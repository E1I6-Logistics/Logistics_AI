"""OMX 완료 → 실제 AI 게이트 → 다음 경로, Pose 지연 완화를 실기체 없이 확인한다."""

import asyncio
from types import SimpleNamespace

import pytest

from backend.app.models.robot import RobotState, NavigationType
from backend.app.ros2 import ros_gateway as G
from backend.app.services import real_navigation as N
from backend.app.services import order_manager as O
from backend.app.services import llm_route_preview as P
from backend.app.services.kev_mapf import kev_dispatcher as K
from backend.app.services.mode_service import mode_manager
from tests.test_reconnect_reset import ctx  # 실제 Gateway/Navigation + 가짜 ROS·시계
from tests.test_real_kev_mapf import graph


@pytest.fixture
def flow(ctx, monkeypatch):
    scene = graph()
    scene['features'].extend([
        {'geometry': {'type': 'Point', 'coordinates': [6, 9]}, 'properties': {'id': '6'}},
        *[{'geometry': {'type': 'MultiLineString'}, 'properties': {
            'id': f'ex{a}{b}', 'startid': a, 'endid': b, 'weight': w}}
          for a, b, w in [('5', '6', 3), ('6', '3', 9), ('3', '0', 6)]],
    ])
    for module in (N, G, K):
        monkeypatch.setattr(module, 'load_route_graph', lambda: scene)
    monkeypatch.setattr(O, 'ros_gateway', ctx.gateway)
    monkeypatch.setattr(O, 'fleet_manager', ctx.fleet)
    monkeypatch.setattr(G, 'ros_gateway', ctx.gateway)
    monkeypatch.setattr(K, '_coordinators', {})
    monkeypatch.setattr(mode_manager, '_mode', 'real')
    ctx.calls = []

    def preview(_graph, start, target, **kwargs):
        ctx.calls.append((str(start), str(target)))
        return {'path': [str(start), str(target)]}

    monkeypatch.setattr(P, 'preview_llm_route', preview)  # 추론만 대체하고 FMS 게이트는 실제 코드를 사용한다.
    ctx.scene = scene
    return ctx


def at_work(ctx, node='5'):
    x, y = N.DistanceAStar(ctx.scene).nodes[node]
    ctx.robot.update_pose(x, y, 0.)
    ctx.robot.current_node = ctx.robot.occupied_node = node
    ctx.robot.goal_node = node  # 도착 전 목적지 표시가 남아 있는 실제 상태.
    row = dict(order_id='job', order_name='test', robot_id='robot1', mode='real', status='IN_PROGRESS',
               phase='WORKING_AT_WORKSTATION' if node == '3' else 'WORKING_AT_PICKUP',
               pickup_nodes=['5', '6'], pickup_index=0 if node == '5' else 1,
               workstation_node='3' if node == '3' else None,
               items={'A': 1, 'B': 0, 'C': 1, 'D': 0}, total_quantity=2,
               operation_mode='ai', driving_mode='llm', selector='kev', model='test')
    O.order_manager.orders['job'] = row
    ctx.robot.assign_order('job', row['items'], 2, ['5', '6'], row['workstation_node'])
    ctx.robot.set_state(RobotState.WAITING)
    ctx.nav.refresh()
    return row


def result(ctx, node, *, success=True, job='job'):
    omx = {'5': 'omx1', '6': 'omx2', '3': 'omx3'}[node]
    O.order_manager.on_mqtt_result(SimpleNamespace(omx_id=omx), {'job_id': job, 'success': success})


@pytest.mark.parametrize('node,target,phase', [
    ('5', '6', 'MOVING_TO_PICKUP'),
    ('6', '3', 'WAITING_WORKSTATION'),
    ('3', '0', 'RETURNING'),
])
def test_omx_success_passes_real_ai_prepare_validate_and_execution(flow, node, target, phase):
    ctx = flow
    row = at_work(ctx, node)

    async def scenario():
        # Before fix: legal OMX waiting blocks AI before inference.
        with pytest.raises(ValueError, match='경로 준비 가능한 정지 상태'):
            await K.prepare_order_route(row, target, node)
        assert not ctx.calls
        result(ctx, node)
        assert row['phase'] == phase
        assert ctx.robot.state == RobotState.WAITING
        assert O.order_manager._finish_omx_transition(row)
        assert ctx.robot.state == RobotState.IDLE
        assert ctx.robot.goal_node is None
        assert '_omx_completed_node' not in row
        prepared = await K.prepare_order_route(row, target, node)
        path = K.validate_prepared_order_route(row, target, node, prepared)
        ctx.nav.validate_pending(['robot1'])
        ctx.nav.navigate_to_node('robot1', target, required_path=path)
        ctx.clock[0] += .01  # 예약한 출발 시각을 지난 다음 제어 주기
        ctx.nav.tick()
        assert ctx.calls == [(node, target)]
        assert ctx.node.send_follow_waypoints_goal.call_count == 1, (ctx.nav._robots, ctx.nav._sync_errors)
        assert ctx.robot.goal_node == target
        assert ctx.robot.state == RobotState.NAVIGATING

    asyncio.run(scenario())


@pytest.mark.parametrize('blocker', ['auxiliary', 'mqtt', 'cancel', 'pose'])
def test_terminal_success_waits_for_readiness_instead_of_failing_next_stage(flow, blocker):
    ctx = flow
    row = at_work(ctx)
    result(ctx, '5')
    if blocker == 'auxiliary':
        ctx.node.has_active_auxiliary.return_value = True
    elif blocker == 'mqtt':
        ctx.mqtt._pending_jobs.add(('omx1', 'job'))
    elif blocker == 'cancel':
        ctx.nav._stopping['robot1'] = None
    else:
        ctx.clock[0] += N.REAL_POSE_TIMEOUT_S + .1
    assert not O.order_manager._finish_omx_transition(row)
    asyncio.run(O.order_manager._tick_real(row))
    assert not O.order_manager._route_tasks
    assert row['status'] == 'IN_PROGRESS'
    assert ctx.robot.state == RobotState.WAITING
    assert not ctx.calls
    if blocker == 'cancel':
        assert O.order_manager.cancel_order('job', return_to_station=False)
        assert '_omx_completed_node' not in row  # 취소 뒤 복귀가 완료 전환 표식에 막히지 않는다.
        return
    ctx.node.has_active_auxiliary.return_value = False
    ctx.mqtt._pending_jobs.clear()
    ctx.nav._stopping.clear()
    ctx.robot.update_pose(6., 6., 0.)
    assert O.order_manager._finish_omx_transition(row)
    assert ctx.robot.state == RobotState.IDLE


@pytest.mark.parametrize('success,job', [(False, 'job'), (True, 'another-job')])
def test_failed_or_unrelated_omx_result_cannot_mark_robot_ready(flow, success, job):
    row = at_work(flow)
    result(flow, '5', success=success, job=job)
    assert '_omx_completed_node' not in row
    assert flow.robot.state != RobotState.IDLE
    assert not flow.calls


def test_active_pose_delay_below_five_seconds_is_allowed_but_long_loss_is_protected(flow):
    ctx = flow
    assert N.REAL_POSE_TIMEOUT_S == 5.0
    ctx.robot.goal_node = None
    ctx.robot.set_state(RobotState.IDLE)
    ctx.nav.request('robot1', '3', NavigationType.GOAL, required_path=['0', '3'])
    ctx.clock[0] += .01
    ctx.robot.update_pose(0., 0., 0.)
    ctx.nav.tick()
    assert ctx.robot.state == RobotState.NAVIGATING
    ctx.clock[0] += 4.9
    ctx.fleet.get_robot('robot2').update_pose(0., 3., 0.)
    ctx.nav.tick()
    assert not ctx.cancels
    assert ctx.robot.state == RobotState.NAVIGATING
    ctx.clock[0] += .2
    ctx.fleet.get_robot('robot2').update_pose(0., 3., 0.)
    ctx.nav.tick()
    assert ctx.cancels
    assert ctx.robot.state == RobotState.PAUSED
    assert ctx.nav._traffic._protected_resources['robot1']
    assert ctx.nav._traffic._reservations.snapshot()
