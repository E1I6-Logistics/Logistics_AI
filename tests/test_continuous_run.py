"""Actual navigation/traffic code; fake ROS goals, results, poses and clock only."""
from unittest.mock import Mock
import pytest
from backend.app.models.robot import RobotState, NavigationType


@pytest.fixture
def rig(monkeypatch):
    monkeypatch.setenv('FMS_CONTINUOUS_RUN', '1')
    from tests.test_real_kev_mapf import RealKevTests
    fixture = RealKevTests('runTest')
    fixture._setupAsyncioRunner()
    try:
        fixture.setUp()
        yield fixture
    finally:
        try:
            assert fixture.doCleanups(), '모의 ROS 패치 정리 실패'
        finally:
            fixture._tearDownAsyncioRunner()


def moving(rig, path=None):
    nav = rig.nav
    nav.request('robot1', '3', NavigationType.GOAL, required_path=path or ['0', '3'])
    rig.advance()
    assert 'robot1' in nav._executing
    return rig.fleet.get_robot('robot1')


def test_failed_terminal_action_retries_same_goal_and_then_completes(rig):
    robot = moving(rig)
    rig.nav.on_result('robot1', False, status=6)
    assert robot.state == RobotState.PAUSED
    rig.advance(.1);rig.advance(3);rig.advance(.2)
    assert 'robot1' in rig.nav._executing and len(rig.sent) == 2
    rig.finish_action('robot1');rig.advance()
    assert rig.completed == ['robot1'] and robot.state == RobotState.IDLE
    assert not rig.nav._continuous.pending


def test_pose_recovery_waits_for_terminal_cancel_and_fresh_observation(rig):
    robot = moving(rig)
    rig.now += 10
    rig.nav.tick()
    assert robot.state == RobotState.PAUSED and rig.cancels
    rig.advance(3)
    assert len(rig.sent) == 1  # fresh pose alone never proves old Nav2 terminated
    rig.cancels[-1][1]()
    rig.advance(.1);rig.advance(3);rig.advance(.2)
    assert robot.state in (RobotState.WAITING, RobotState.NAVIGATING)
    assert len(rig.sent) == 2


def test_recovery_preserves_original_ai_path(rig):
    moving(rig, ['0', '7', '3'])
    rig.nav.on_result('robot1', False, status=6)
    request=Mock(wraps=rig.nav.request);rig.nav.request=request
    rig.advance(.1);rig.advance(3);rig.advance(.2)
    assert request.call_args.kwargs['required_path'] == ['0', '7', '3']


@pytest.mark.parametrize('reason',['cancel_timeout','station_exit_failed','omx_job_failed','aruco_alignment_failed'])
def test_unsafe_or_physical_task_failures_are_never_auto_replayed(rig,reason):
    robot=moving(rig)
    rig.nav._pause('robot1', reason)
    rig.advance(3);rig.advance(3)
    assert robot.state == RobotState.PAUSED
    assert 'robot1' not in rig.nav._continuous.pending
    assert len(rig.sent) == 1


def test_emergency_stop_and_long_disconnect_reset_discard_retry(rig):
    robot=moving(rig)
    rig.nav.on_result('robot1', False, status=6)
    robot.set_state(RobotState.EMERGENCY_STOP)
    rig.advance(3)
    assert not rig.nav._continuous.pending and len(rig.sent)==1
    robot.pause('reconnected_with_active_work')
    robot.reconnect_reset_pending=True
    rig.advance(3)
    assert not rig.nav._continuous.pending and len(rig.sent)==1


def test_changed_operator_goal_discards_old_recovery(rig):
    robot=moving(rig)
    rig.nav.on_result('robot1', False, status=6)
    robot.goal_node='4'
    rig.advance(3)
    assert not rig.nav._continuous.pending and len(rig.sent)==1


def test_actual_occupied_destination_still_blocks_motion(rig):
    moving(rig)
    rig.nav.on_result('robot1',False,status=6)
    other=rig.fleet.get_robot('robot2')
    other.x,other.y=rig.nodes['3'];other.pose_received_at=rig.now
    rig.advance(.1);rig.advance(3);rig.advance(.2)
    assert len(rig.sent)==1
    assert rig.fleet.get_robot('robot1').goal_node=='3'


def test_scenario_waits_only_for_same_active_recovery(rig):
    robot=moving(rig)
    rig.nav.on_result('robot1', False, status=6)
    assert rig.nav._continuous.waiting('robot1')
    robot.goal_node='4'
    assert not rig.nav._continuous.waiting('robot1')
    robot.goal_node='3'
    robot.set_state(RobotState.EMERGENCY_STOP)
    assert not rig.nav._continuous.waiting('robot1')


def test_mode_switch_cannot_restart_real_navigation(rig,monkeypatch):
    from backend.app.services.mode_service import mode_manager
    moving(rig)
    rig.nav.on_result('robot1',False,status=6)
    monkeypatch.setattr(mode_manager,'_mode','simulation')
    rig.advance(.1);rig.advance(3);rig.advance(.2)
    assert len(rig.sent)==1
