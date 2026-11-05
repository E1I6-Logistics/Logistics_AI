"""실제 동작 없이 취소·종료 상태 유실·재접속 후 재요청을 검증한다."""
from concurrent.futures import Future
from types import SimpleNamespace as NS
from unittest.mock import Mock
from uuid import uuid4

import pytest

from backend.app.services.pallet_replenishment import PalletReplenishment, ReplenishmentError
from backend.app.services.waf_telemetry import WafTelemetry


@pytest.fixture
def rig(tmp_path):
    clock = [100.]
    telemetry = WafTelemetry(clock=lambda: clock[0])
    service = PalletReplenishment(tmp_path / 'job.json', clock=lambda: clock[0], telemetry=telemetry)
    sent, result, cancel = Future(), Future(), Future()
    client = Mock(server_is_ready=lambda: True, action_name='/robot4/pallet_replace')
    client.send_goal_async.return_value = sent
    handle = Mock(accepted=True)
    handle.get_result_async.return_value = result
    handle.cancel_goal_async.return_value = cancel
    service.attach(client, NS)
    request_id = str(uuid4())
    service.start('A', request_id)
    sent.set_result(handle)
    return NS(clock=clock, telemetry=telemetry, service=service, client=client,
              handle=handle, sent=sent, result=result, cancel=cancel, id=request_id)


def status(telemetry, goal_id, state, source='/robot4/pallet_replace'):
    row = NS(status=state, goal_info=NS(goal_id=NS(uuid=list(bytes.fromhex(goal_id)))))
    telemetry.observe_action_status(NS(status_list=[row]), source)


def next_job(rig):
    rig.client.send_goal_async.return_value = Future()
    return rig.service.start('B', str(uuid4()))


def test_cancelled_status_recovers_lost_result_and_allows_next_job(rig):
    goal_id = rig.service.job['ros_goal_id']
    status(rig.telemetry, goal_id, 2)
    rig.service.cancel(rig.id)
    rig.cancel.set_result(NS(goals_canceling=[1]))
    assert rig.service.snapshot()['busy']
    status(rig.telemetry, goal_id, 5)
    row = rig.service.snapshot()
    assert row['job']['state'] == 'CANCELLED' and not row['busy']
    assert not row['can_cancel'] and not row['job']['cancel_requested']
    # 별명 경로에서 늦게 도착한 동일 UUID의 실행 상태로 다시 잠그지 않는다.
    status(rig.telemetry, goal_id, 2, '/pallet_replace')
    assert not rig.service.snapshot()['busy']
    assert next_job(rig)['job']['state'] == 'SENDING'
    assert rig.client.send_goal_async.call_count == 2
    # 이전 취소 결과가 늦게 도착해도 새 요청을 변경하지 않는다.
    rig.result.set_result(NS(status=5, result=NS(success=False, message='Cancelled')))
    assert rig.service.snapshot()['job']['item'] == 'B'
    assert rig.service.pending_goal


@pytest.mark.parametrize('failure', ['rejected', 'response_error', 'send_error', 'timeout', 'result_timeout'])
def test_cancel_failure_can_retry_but_never_unlocks_running_goal(rig, failure):
    if failure == 'send_error':
        rig.handle.cancel_goal_async.side_effect = RuntimeError('link lost')
    rig.service.cancel(rig.id)
    if failure == 'rejected':
        rig.cancel.set_result(NS(goals_canceling=[]))
    elif failure == 'response_error':
        rig.cancel.set_exception(RuntimeError('link lost'))
    elif failure == 'result_timeout':
        rig.cancel.set_result(NS(goals_canceling=[1]))
    if failure in ('timeout', 'result_timeout'):
        rig.clock[0] += 16
        rig.service.tick()
    row = rig.service.snapshot()
    assert row['busy'] and row['can_cancel'] and not row['job']['cancel_requested']
    with pytest.raises(ReplenishmentError):
        next_job(rig)
    rig.handle.cancel_goal_async.side_effect = None
    rig.handle.cancel_goal_async.return_value = Future()
    rig.service.cancel(rig.id)
    assert rig.handle.cancel_goal_async.call_count == 2


def test_delayed_old_cancel_and_accept_cannot_change_a_new_job(rig):
    rig.service.cancel(rig.id)
    rig.result.set_result(NS(status=5, result=NS(success=False, message='Cancelled')))
    row = next_job(rig)
    rig.cancel.set_exception(RuntimeError('old response'))
    rig.service.accepted(rig.id, rig.sent)
    assert rig.service.snapshot()['job']['request_id'] == row['job']['request_id']
    assert rig.service.pending_goal and rig.service.goal_handle is None


def test_already_terminated_cancel_reply_unlocks_without_claiming_success(rig):
    status(rig.telemetry, rig.service.job['ros_goal_id'], 2)
    rig.service.cancel(rig.id)
    rig.cancel.set_result(NS(return_code=3, goals_canceling=[]))
    row = rig.service.snapshot()
    assert row['job']['state'] == 'ENDED' and not row['busy']
    assert next_job(rig)['job']['item'] == 'B'


def test_restart_recovers_exact_goal_uuid_and_does_not_send_a_new_goal(rig):
    rig.service.cancel(rig.id)
    recovered = PalletReplenishment(rig.service.path, telemetry=rig.telemetry)
    recovered.attach(rig.client, NS)
    assert recovered.snapshot()['can_cancel']
    rig.client.cancel_goal_async.return_value = Future()
    recovered.cancel(rig.id)
    rig.client.cancel_goal_async.assert_called_once_with(
        rig.service.job['ros_goal_id'], '/robot4/pallet_replace')
    rig.client.send_goal_async.assert_called_once()
    status(rig.telemetry, rig.service.job['ros_goal_id'], 5)
    assert recovered.snapshot()['job']['state'] == 'CANCELLED'
    assert not recovered.snapshot()['busy']


def test_unrelated_status_empty_array_and_cancel_ack_do_not_mean_stopped(rig):
    rig.service.cancel(rig.id)
    rig.cancel.set_result(NS(goals_canceling=[1]))
    status(rig.telemetry, uuid4().hex, 5)
    rig.telemetry.observe_action_status(NS(status_list=[]), '/robot4/pallet_replace')
    assert rig.service.snapshot()['busy']
    assert not rig.service.snapshot()['can_clear']
    with pytest.raises(ReplenishmentError):
        rig.service.clear_stopped(rig.id)


def test_legacy_orphan_clear_requires_operator_and_preserves_other_active_goals(rig):
    del rig.service.job['ros_goal_id']
    rig.service.cancel(rig.id)
    rig.service.close()
    recovered = PalletReplenishment(rig.service.path, telemetry=rig.telemetry)
    recovered.attach(rig.client, NS)
    assert recovered.snapshot()['busy'] and recovered.snapshot()['can_clear']
    with pytest.raises(ReplenishmentError):
        recovered.clear_stopped(str(uuid4()))
    foreign_id = uuid4().hex
    status(rig.telemetry, foreign_id, 2)
    assert not recovered.snapshot()['can_clear']
    with pytest.raises(ReplenishmentError):
        recovered.clear_stopped(rig.id)
    status(rig.telemetry, foreign_id, 5)
    assert recovered.clear_stopped(rig.id)['job']['state'] == 'CANCELLED'
    assert not recovered.snapshot()['busy']
    rig.client.send_goal_async.assert_called_once()  # 기록 정리만으로 다시 실행하지 않는다.


@pytest.mark.parametrize('terminal,expected', [(4, 'ENDED'), (6, 'FAILED')])
def test_terminal_status_without_result_does_not_invent_success(rig, terminal, expected):
    status(rig.telemetry, rig.service.job['ros_goal_id'], terminal)
    row = rig.service.snapshot()
    assert row['job']['state'] == expected and not row['busy']
    if terminal == 4:
        rig.result.set_result(NS(status=4, result=NS(success=False, message='Not replaced')))
        assert rig.service.snapshot()['job']['state'] == 'FAILED'


def test_pending_accept_can_retry_cancellation_by_persisted_uuid(rig):
    rig.result.set_result(NS(status=5, result=NS(success=False, message='old')))
    row = next_job(rig)
    request_id = row['job']['request_id']
    rig.service.cancel(request_id)
    rig.clock[0] += 16
    rig.service.tick()
    assert rig.service.snapshot()['can_cancel']
    rig.client.cancel_goal_async.return_value = Future()
    rig.service.cancel(request_id)
    rig.client.cancel_goal_async.assert_called_once_with(row['job']['ros_goal_id'], '/robot4/pallet_replace')


def test_clear_stopped_api_requires_separate_confirmation_and_allows_a_new_request(rig, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.app.routers import replenishment as api
    rig.service.close()
    recovered = PalletReplenishment(rig.service.path, telemetry=rig.telemetry)
    recovered.attach(rig.client, NS)
    monkeypatch.setattr(api, 'replenishment', recovered)
    monkeypatch.setattr(api, 'mode_manager', NS(mode='real'))
    app = FastAPI()
    app.include_router(api.router)
    with TestClient(app) as client:
        body = dict(request_id=rig.id, confirm=True, confirm_stopped=False)
        assert client.post('/api/replenishment/clear-stopped', json=body).status_code == 400
        assert client.post('/api/replenishment/clear-stopped', json={**body, 'confirm_stopped': True}).status_code == 200
        rig.client.send_goal_async.return_value = Future()
        response = client.post('/api/replenishment', json=dict(item='B', request_id=str(uuid4()), confirm=True))
        assert response.status_code == 200 and response.json()['job']['item'] == 'B'


def test_transport_sends_and_cancels_only_the_recorded_ros_uuid(monkeypatch):
    import sys
    from backend.app.ros2.waf_client import WafActionTransport
    uuid_module = NS(UUID=lambda **kwargs: NS(**kwargs))
    cancel_module = NS(CancelGoal=NS(Request=lambda: NS(goal_info=NS(goal_id=NS(uuid=[])))))
    monkeypatch.setitem(sys.modules, 'unique_identifier_msgs.msg', uuid_module)
    monkeypatch.setitem(sys.modules, 'action_msgs.srv', cancel_module)
    client, node = Mock(), Mock()
    node.create_client.return_value.service_is_ready.return_value = True
    transport = WafActionTransport([('/robot4/pallet_replace', client)], node)
    goal_id = uuid4().hex
    transport.send_goal_async(NS(destination=1), goal_id=goal_id)
    assert bytes(client.send_goal_async.call_args.kwargs['goal_uuid'].uuid).hex() == goal_id
    transport.cancel_goal_async(goal_id, '/robot4/pallet_replace')
    request = node.create_client.return_value.call_async.call_args.args[0]
    assert bytes(request.goal_info.goal_id.uuid).hex() == goal_id
    node.create_client.assert_called_once_with(cancel_module.CancelGoal, '/robot4/pallet_replace/_action/cancel_goal')
