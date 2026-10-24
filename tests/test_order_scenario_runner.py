"""실기체 등록 차단과 기존 주문 API를 사용하는 시나리오의 등록 일정을 검증."""

from copy import deepcopy
import io
import json
import sys

import pytest

from tests import order_scenarios as runner


def response(value):
    return io.BytesIO(json.dumps(value).encode())


def test_real_mode_is_rejected_before_any_order_post(monkeypatch):
    methods = []

    def request(req, **kwargs):
        methods.append(req.get_method())
        if req.full_url.endswith('/api/mode'):
            return response(dict(mode='real', operation_mode='algorithm'))
        if req.full_url.endswith('/api/scenarios/status'):
            return response(dict(state='idle'))
        return response(dict(orders=[]))

    monkeypatch.setattr(runner, 'urlopen', request)
    monkeypatch.setattr(sys, 'argv', ['order_scenarios.py', 'bulk_six', '--run'])
    assert runner.main() == 1
    assert 'POST' not in methods


@pytest.mark.parametrize('row', [
    dict(status='PENDING'), dict(status='IN_PROGRESS'),
    dict(status='COMPLETED', phase='RETURNING'),
    dict(status='FAILED', recovery_status='BLOCKED'),
])
def test_existing_work_blocks_a_new_test_without_altering_orders(row):
    before = deepcopy(row)
    with pytest.raises(ValueError, match='기존 주문'):
        runner.ensure_available(
            dict(mode='simulation', operation_mode='algorithm'),
            dict(state='idle'), dict(orders=[row]), 'algorithm',
        )
    assert row == before


def test_default_preview_does_not_contact_server(monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError('미리보기에서는 서버에 연결하지 않아야 합니다.')

    monkeypatch.setattr(runner, 'urlopen', unexpected)
    monkeypatch.setattr(sys, 'argv', ['order_scenarios.py', 'bulk_six'])
    assert runner.main() == 0


def test_continuous_ai_orders_use_existing_order_api_at_configured_times(monkeypatch):
    clock, submitted, orders = [0.0], [], []

    def request(req, **kwargs):
        if req.full_url.endswith('/api/mode'):
            return response(dict(mode='simulation', operation_mode='ai'))
        if req.full_url.endswith('/api/scenarios/status'):
            return response(dict(state='idle'))
        assert req.full_url.endswith('/api/orders')
        if req.get_method() == 'POST':
            payload = json.loads(req.data)
            submitted.append((clock[0], payload))
            row = dict(payload, order_id=str(len(submitted)), robot_id='robot1', status='COMPLETED', phase='COMPLETED')
            orders.append(row)
            return response(row)
        return response(dict(orders=orders))

    monkeypatch.setattr(runner, 'urlopen', request)
    monkeypatch.setattr(runner.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(runner.time, 'sleep', lambda seconds: clock.__setitem__(0, clock[0]+seconds))
    monkeypatch.setattr(sys, 'argv', ['order_scenarios.py', 'continuous_six', '--operation', 'ai', '--run'])
    assert runner.main() == 0
    assert [at for at, _ in submitted] == [0, 0, 0, 10, 10, 20]
    for _, payload in submitted:
        assert payload['operation_mode'] == 'ai'
        assert (payload['selector'], payload['model']) == ('kev', 'kev-08b')
        assert payload['total_quantity'] == sum(payload['items'].values())
    assert len({p['order_name'] for _, p in submitted}) == 6
