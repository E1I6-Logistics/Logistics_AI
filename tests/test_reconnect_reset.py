"""실기체 없이 장시간 단절의 경계·종료 확인·늦은 결과 격리를 검증한다."""

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend.app.models.robot import RobotState, NavigationType
from backend.app.ros2 import ros_gateway as G
from backend.app.services import reconnect_recovery as R
from backend.app.services import real_navigation as N
from backend.app.services.fleet_manager import FleetManager
from backend.app.services.mqtt_manager import MQTTManager
from backend.app.services.order_manager import order_manager
from backend.app.services.workstation_manager import workstation_manager
from tests.test_real_kev_mapf import graph


@pytest.fixture
def ctx(monkeypatch):
    clock = [100.0]
    for module in (G, R):
        monkeypatch.setattr(module, 'monotonic', lambda: clock[0])
    monkeypatch.setattr('backend.app.models.robot.monotonic', lambda: clock[0])
    monkeypatch.setattr(N, 'load_route_graph', graph)
    monkeypatch.setattr(G, 'load_route_graph', graph)
    monkeypatch.setattr(G, 'ZENOH_MISSING_GRACE_S', 5.)
    fleet = FleetManager()
    for rid, y in [('robot1', 0.), ('robot2', 3.)]:
        robot = fleet.register_robot(rid)
        robot.update_pose(0., y, 0.)
        robot.set_state(RobotState.IDLE)
    monkeypatch.setattr(G, 'fleet_manager', fleet)
    mqtt = MQTTManager()
    monkeypatch.setattr(G, 'mqtt_manager', mqtt)
    gateway = G.RosGateway()
    gateway.navigation._clock = lambda: clock[0]
    node = Mock()
    node.has_active_auxiliary.return_value = False
    cancels, auxiliary = [], []
    node.cancel_follow_waypoints.side_effect = lambda rid, callback: cancels.append(callback)
    node.cancel_auxiliary.side_effect = lambda rid, callback: auxiliary.append(callback)
    gateway.set_ros_node(node)
    gateway.navigation.refresh()
    monkeypatch.setattr(order_manager, 'orders', {})
    monkeypatch.setattr(order_manager, 'returning', {})
    monkeypatch.setattr(order_manager, 'recovering', {})
    monkeypatch.setattr(order_manager, '_return_started', set())
    monkeypatch.setattr(order_manager, '_route_tasks', {})
    monkeypatch.setattr(order_manager, '_robot_dispatcher', Mock())
    monkeypatch.setattr(workstation_manager, '_assigned', {})
    return SimpleNamespace(clock=clock, fleet=fleet, robot=fleet.get_robot('robot1'),
                           gateway=gateway, nav=gateway.navigation, mqtt=mqtt,
                           node=node, cancels=cancels, auxiliary=auxiliary)


def sync(ctx, missing=False):
    ids = ['robot2'] if missing else ['robot1', 'robot2']
    ctx.gateway.sync_connected_robots([dict(name=rid, connected=True) for rid in ids])


def disconnect(ctx, seconds=10.):
    sync(ctx, missing=True)
    ctx.clock[0] += 5
    sync(ctx, missing=True)
    ctx.clock[0] += seconds - 5
    sync(ctx)


def order(ctx, job='old', rid='robot1'):
    order_manager.orders[job] = dict(order_id=job, robot_id=rid, mode='real',
                                    status='IN_PROGRESS', phase='MOVING_TO_PICKUP')
    ctx.fleet.get_robot(rid).order_id = job


def pose(ctx):
    ctx.clock[0] += .1
    ctx.robot.update_pose(0., 0., 0.)


@pytest.mark.parametrize('seconds,reset', [(9.999, False), (10., True), (10.001, True)])
def test_observed_disconnect_boundary_and_short_drop_preservation(ctx, seconds, reset):
    order(ctx)
    disconnect(ctx, seconds)
    assert ctx.robot.reconnect_reset_pending == reset
    assert (order_manager.orders['old']['status'] == 'FAILED') == reset
    assert ctx.robot.order_id == (None if reset else 'old')
    sync(ctx)  # repeated ONLINE polling must not reset twice
    assert len(ctx.cancels) == int(reset)


def test_reservations_wait_for_terminal_and_new_pose_only_target_is_reset(ctx):
    ctx.nav.request('robot1', '3', NavigationType.GOAL, required_path=['0', '7', '3'])
    ctx.nav._send('robot1', '3', 0, N.DistanceAStar(graph()))
    order(ctx)
    order(ctx, 'other', 'robot2')
    before = ctx.nav._traffic._reservations.snapshot()
    assert before
    disconnect(ctx)
    assert ctx.nav._traffic._reservations.snapshot() == before
    assert not order_manager.is_current('old', 'robot1', 'MOVING_TO_PICKUP')
    pose(ctx)
    ctx.gateway._reconnect_recovery.tick()
    assert ctx.robot.reconnect_reset_pending  # fresh pose alone is not stop confirmation
    ctx.cancels[-1]()
    ctx.robot.pose_received_at = 100.  # pre-reconnect pose cannot clear the route
    ctx.gateway._reconnect_recovery.tick()
    assert ctx.nav._traffic._reservations.snapshot() == before
    pose(ctx)
    ctx.gateway._reconnect_recovery.tick()
    assert ctx.robot.state == RobotState.IDLE
    assert not ctx.nav._traffic._reservations.snapshot()
    assert ctx.robot.current_node == '0'
    assert ctx.robot.route is ctx.robot.goal_node is ctx.robot.navigation_type is None
    assert order_manager.orders['other']['status'] == 'IN_PROGRESS'
    assert ctx.fleet.get_robot('robot2').order_id == 'other'
    assert ctx.node.send_follow_waypoints_goal.call_count == 1  # old goal never replayed


def test_omx_and_auxiliary_must_finish_without_old_next_step(ctx):
    order(ctx)
    ctx.mqtt._pending_jobs.add(('omx1', 'old'))
    old_callback = Mock()
    ctx.gateway._waiting_omx['robot1'] = ('old', old_callback, 'new_goal_request')
    ctx.node.has_active_auxiliary.return_value = True
    disconnect(ctx)
    assert not ctx.cancels  # cancel auxiliary first
    ctx.node.has_active_auxiliary.return_value = False
    ctx.auxiliary[-1]()
    ctx.cancels[-1]()
    pose(ctx)
    ctx.gateway._reconnect_recovery.tick()
    assert ctx.robot.reconnect_reset_pending
    assert ('omx1', 'old') in ctx.mqtt._canceled_jobs
    ctx.gateway._on_omx_terminal('robot1', 'old')
    old_callback.assert_not_called()
    ctx.mqtt._pending_jobs.clear()  # OMX terminal received
    ctx.gateway._reconnect_recovery.tick()
    assert ctx.robot.state == RobotState.IDLE
    old_callback.assert_not_called()


def test_timeout_retry_and_new_work_blocked_during_reset(ctx):
    disconnect(ctx)
    pose(ctx)
    with pytest.raises(ValueError, match='장시간 단절'):
        ctx.gateway.navigate_to_node('robot1', '3')
    with pytest.raises(ValueError, match='장시간 단절'):
        ctx.gateway.cmd_vel('robot1', .1, 0.)
    with pytest.raises(ValueError, match='장시간 단절'):
        ctx.nav.request('robot1', '3', NavigationType.GOAL)
    ctx.clock[0] += 6
    ctx.nav.tick()  # cancel timeout must keep old protection, not imply success
    ctx.gateway._reconnect_recovery.tick()
    assert len(ctx.cancels) == 2
    assert ctx.robot.reconnect_reset_pending
    ctx.cancels[-1]()
    pose(ctx)
    ctx.gateway._reconnect_recovery.tick()
    assert ctx.robot.state == RobotState.IDLE


def test_estop_and_invalid_pose_are_not_silently_released(ctx):
    ctx.robot.set_state(RobotState.EMERGENCY_STOP)
    disconnect(ctx)
    ctx.cancels[-1]()
    pose(ctx)
    ctx.robot.x = 1000.
    ctx.gateway._reconnect_recovery.tick()
    assert ctx.robot.reconnect_reset_pending
    with pytest.raises(ValueError, match='장시간 단절'):
        ctx.gateway.emergency_release('robot1')
    pose(ctx)
    ctx.gateway._reconnect_recovery.tick()
    assert not ctx.robot.reconnect_reset_pending
    assert ctx.robot.state == RobotState.EMERGENCY_STOP


def test_second_disconnect_does_not_accept_stale_completion_or_old_pose(ctx):
    disconnect(ctx)
    previous = ctx.cancels[-1]
    disconnect(ctx, 6.)
    previous()  # old completion cannot approve a new recovery record
    assert not ctx.gateway._reconnect_recovery.pending['robot1']['stopped']
    ctx.clock[0] += 6
    ctx.nav.tick()
    ctx.gateway._reconnect_recovery.tick()
    ctx.cancels[-1]()
    pose(ctx)
    ctx.gateway._reconnect_recovery.tick()
    assert ctx.robot.state == RobotState.IDLE


def test_old_async_return_is_cancelled_before_a_new_order_can_start(ctx):
    async def scenario():
        order(ctx)
        order_manager.returning['robot1'] = 'old'
        cleaned = []

        async def old_route():
            try:
                await asyncio.Future()
            finally:
                cleaned.append(True)
                order_manager._route_tasks.pop('return:robot1', None)

        task = asyncio.create_task(old_route())
        order_manager._route_tasks['return:robot1'] = task
        await asyncio.sleep(0)
        disconnect(ctx)
        ctx.cancels[-1]()
        pose(ctx)
        ctx.gateway._reconnect_recovery.tick()
        assert ctx.robot.reconnect_reset_pending  # finally has not run yet
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert task.cancelled() and cleaned
        ctx.gateway._reconnect_recovery.tick()
        assert not ctx.robot.reconnect_reset_pending
        assert not order_manager.returning

    asyncio.run(scenario())


def test_real_reconnect_does_not_cancel_simulation_return_with_same_robot_id(ctx):
    order(ctx, 'sim')
    order_manager.orders['sim']['mode'] = 'simulation'
    ctx.robot.clear_order()  # 실제 Robot과 시뮬레이션 로봇은 별도 객체다.
    order_manager.returning['robot1'] = 'sim'
    order_manager._return_started.add('robot1')
    task = Mock()
    task.done.return_value = False
    order_manager._route_tasks['return:robot1'] = task
    disconnect(ctx)
    ctx.cancels[-1]()
    pose(ctx)
    ctx.gateway._reconnect_recovery.tick()
    assert ctx.robot.state == RobotState.IDLE
    assert order_manager.returning['robot1'] == 'sim'
    assert 'robot1' in order_manager._return_started
    assert order_manager.orders['sim']['status'] == 'IN_PROGRESS'
    task.get_loop.assert_not_called()
