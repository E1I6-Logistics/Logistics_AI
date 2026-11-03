"""실제 로봇 전송 없이 WAF 추가 기능의 액션 수명주기와 HTTP 계약을 검증한다."""
from concurrent.futures import Future, ThreadPoolExecutor
import json
from types import SimpleNamespace as NS
from unittest.mock import Mock
from uuid import uuid4
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from backend.app.services.pallet_replenishment import PalletReplenishment, ReplenishmentError
from backend.app.routers import replenishment as api


@pytest.fixture
def rig(tmp_path):
    service = PalletReplenishment(tmp_path/'job.json', clock=lambda:100.)
    sent, result, cancel = Future(), Future(), Future()
    client = Mock(server_is_ready=lambda:True)
    client.send_goal_async.return_value = sent
    handle = Mock(accepted=True)
    handle.get_result_async.return_value = result
    handle.cancel_goal_async.return_value = cancel
    service.attach(client, NS)
    return NS(service=service, client=client, handle=handle, sent=sent, result=result, cancel=cancel, id=str(uuid4()))


@pytest.mark.parametrize('item,destination', [('A',1),('B',2),('C',3),('D',4)])
def test_goal_mapping_feedback_and_result_leave_stock_external(rig, item, destination):
    rig.service.start(item, rig.id)
    assert rig.client.send_goal_async.call_args.args[0].destination == destination
    rig.sent.set_result(rig.handle)
    callback = rig.client.send_goal_async.call_args.kwargs['feedback_callback']
    callback(NS(feedback=NS(stage='NAV_TO_PICK_2')))
    assert rig.service.snapshot()['job']['stage'] == 'NAV_TO_PICK_2'
    assert rig.service.snapshot()['busy']
    rig.result.set_result(NS(status=4, result=NS(success=True, message='Done')))
    row = rig.service.snapshot()
    assert row['job']['state'] == 'SUCCEEDED' and not row['busy']
    assert 'stock' not in row and 'robots' not in row


def test_simultaneous_duplicate_is_exactly_one_send_and_other_item_is_busy(rig):
    with ThreadPoolExecutor(2) as executor:
        list(executor.map(lambda _:rig.service.start('A', rig.id), range(2)))
    rig.client.send_goal_async.assert_called_once()
    with pytest.raises(ReplenishmentError) as error:
        rig.service.start('B', str(uuid4()))
    assert error.value.status == 409
    with pytest.raises(ReplenishmentError):
        rig.service.start('B', rig.id)


def test_absent_server_and_rejected_goal_do_not_report_success(rig):
    rig.client.server_is_ready = lambda:False
    with pytest.raises(ReplenishmentError) as error:
        rig.service.start('A', rig.id)
    assert error.value.status == 503
    rig.client.send_goal_async.assert_not_called()
    rig.client.server_is_ready = lambda:True
    rig.service.start('A', rig.id)
    rig.sent.set_result(NS(accepted=False))
    assert rig.service.snapshot()['job']['state'] == 'REJECTED'
    assert not rig.service.snapshot()['busy']


def test_cancel_during_accept_wait_keeps_busy_until_terminal_result(rig):
    rig.service.start('A', rig.id)
    rig.service.cancel(rig.id)
    rig.service.cancel(rig.id)
    rig.handle.cancel_goal_async.assert_not_called()
    rig.sent.set_result(rig.handle)
    rig.handle.cancel_goal_async.assert_called_once()
    rig.cancel.set_result(NS(goals_canceling=[1]))
    assert rig.service.snapshot()['busy']
    rig.result.set_result(NS(status=5, result=NS(success=False, message='Cancelled')))
    assert rig.service.snapshot()['job']['state'] == 'CANCELLED'
    assert not rig.service.snapshot()['busy']


def test_timeout_restart_or_result_failure_never_replays_physical_goal(rig):
    rig.service.start('C', rig.id)
    rig.service.clock = lambda:120.
    rig.service.tick()
    assert rig.service.snapshot()['job']['state'] == 'UNKNOWN'
    reloaded = PalletReplenishment(rig.service.path)
    reloaded.attach(rig.client, NS)
    with pytest.raises(ReplenishmentError):
        reloaded.start('D', str(uuid4()))
    rig.client.send_goal_async.assert_called_once()
    # 지연 응답은 같은 작업으로 회복하고 실제 Result까지 기다린다.
    rig.sent.set_result(rig.handle)
    assert rig.service.snapshot()['job']['state'] == 'RUNNING'
    rig.result.set_exception(RuntimeError('connection lost'))
    assert rig.service.snapshot()['job']['state'] == 'UNKNOWN'
    assert rig.service.snapshot()['busy']


@pytest.mark.parametrize('status,success,expected', [(4,False,'FAILED'),(6,True,'FAILED'),(0,True,'UNKNOWN')])
def test_action_status_and_success_must_both_confirm_success(rig,status,success,expected):
    rig.service.start('D', rig.id); rig.sent.set_result(rig.handle)
    rig.result.set_result(NS(status=status,result=NS(success=success,message='result')))
    assert rig.service.snapshot()['job']['state'] == expected


def test_unwritable_journal_prevents_send_and_corrupt_journal_blocks_replay(rig, tmp_path):
    rig.service.path = tmp_path  # 파일 대신 디렉터리인 잘못된 저장 대상
    with pytest.raises(ReplenishmentError):rig.service.start('A', rig.id)
    rig.client.send_goal_async.assert_not_called()
    bad = tmp_path/'broken.json';bad.write_text('{bad')
    recovered = PalletReplenishment(bad);recovered.attach(rig.client,NS)
    assert recovered.snapshot()['busy']
    with pytest.raises(ReplenishmentError):recovered.start('B', rig.id)


def test_api_confirmation_mode_validation_and_cancel(rig, monkeypatch):
    monkeypatch.setattr(api,'replenishment',rig.service)
    mode = NS(mode='simulation');monkeypatch.setattr(api,'mode_manager',mode)
    app = FastAPI();app.include_router(api.router)
    with TestClient(app) as client:
        assert client.get('/api/replenishment').status_code == 200
        body=dict(item='A',request_id=rig.id,confirm=True)
        assert client.post('/api/replenishment',json=body).status_code == 409
        mode.mode = 'real'
        assert client.post('/api/replenishment',json={**body,'confirm':False}).status_code == 400
        assert client.post('/api/replenishment',json={**body,'item':'E'}).status_code == 422
        assert client.post('/api/replenishment',json=body).status_code == 200
        assert client.post('/api/replenishment',json=body).status_code == 200
        rig.client.send_goal_async.assert_called_once()
        mode.mode = 'simulation'
        assert client.post('/api/replenishment/cancel',json=dict(request_id=rig.id,confirm=True)).status_code == 200
