"""Stress scenario controls and persistent evidence without running a live fleet."""
import asyncio
import json
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import unquote
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from backend.app.services.order_stress_test import OrderStressTest, STRESS_NAME, MAX_OPEN_ORDERS
from backend.app.services.scenario_service import ScenarioManager


class OrderStressTests(unittest.TestCase):
    def test_random_quantities_bounded_and_seed_reproducible(self):
        with TemporaryDirectory() as directory:
            a = OrderStressTest('a'*32, Path(directory), seed=123)
            b = OrderStressTest('b'*32, Path(directory), seed=123)
            draws = [a.next_items() for _ in range(1000)]
            self.assertEqual(draws, [b.next_items() for _ in range(1000)])
            self.assertTrue(all(set(items) == set('ABCD') and 1 <= sum(items.values()) <= 5 for items in draws))
            self.assertEqual({sum(items.values()) for items in draws}, {1, 2, 3, 4, 5})

    def test_record_filename_uses_korean_start_time_without_overwriting(self):
        started = datetime(2026, 10, 26, 8, 30, 0, 123456, tzinfo=timezone.utc)
        with TemporaryDirectory() as directory, patch('backend.app.services.order_stress_test.datetime') as clock:
            clock.now.return_value = started
            clock.fromisoformat.side_effect = datetime.fromisoformat
            a = OrderStressTest('a'*32, Path(directory), seed=1)
            before = a.record_path.read_bytes()
            b = OrderStressTest('b'*32, Path(directory), seed=1)
            self.assertEqual(a.record_path.name, '자동배차테스트[2026-10-26 17-30-00].json')
            self.assertEqual(b.record_path.name, '자동배차테스트[2026-10-26 17-30-00.123456].json')
            self.assertEqual(a.record_path.read_bytes(), before)
            with self.assertRaises(FileExistsError):
                OrderStressTest('c'*32, Path(directory), seed=1)

    def test_every_three_orders_cover_both_single_pickups_and_combined(self):
        from backend.app.services.order_manager import OrderManager
        with TemporaryDirectory() as directory:
            recorder = OrderStressTest('a'*32, Path(directory), seed=42)
            for _ in range(100):
                items = [recorder.next_items() for _ in range(3)]
                self.assertEqual({tuple(OrderManager.get_pickup_nodes(row)) for row in items},
                                 {('5',), ('6',), ('5', '6')})
                self.assertTrue(all(1 <= sum(row.values()) <= 5 for row in items))

    def test_small_record_contains_totals_and_failure_location_only(self):
        with TemporaryDirectory() as directory:
            recorder = OrderStressTest('a'*32, Path(directory), seed=3)
            row = dict(order_id='1', status='IN_PROGRESS', phase='PICKUP', robot_id='robot1')
            recorder.created(row)
            robots = {'robot1': dict(robot_id='robot1', status='NAVIGATING', x=1., y=2., occupied_node='6')}
            for _ in range(100):
                summary, _ = recorder.sample([row], robots)
            self.assertEqual(summary['created'], 1)
            self.assertEqual(list(Path(directory).iterdir()), [recorder.record_path])
            self.assertLess(recorder.record_path.stat().st_size, 1000)
            failed = dict(row, status='FAILED', phase='FAILED', failed_phase='MOVING_TO_PICKUP',
                          failed_at='2026-10-26T00:00:00Z', error='route unavailable',
                          failure_robot_state=dict(occupied_node='5', occupied_edge=None, goal_node='6'))
            recorder.sample([failed], robots)
            summary, _ = recorder.sample([failed], robots)
            self.assertEqual(summary['failed'], 1)
            self.assertEqual(len(summary['failures']), 1)
            failure = summary['failures'][0]
            self.assertEqual((failure['node'], failure['destination'], failure['phase']), ('5', '6', 'MOVING_TO_PICKUP'))
            self.assertEqual(failure['reason'], 'route unavailable')
            recorder.finish('stopped', 'operator stop')
            before = recorder.record_path.read_bytes()
            recorder.sample([dict(row, status='COMPLETED', phase='COMPLETED')], robots)
            self.assertEqual(recorder.record_path.read_bytes(), before)
            saved = json.loads(before)
            self.assertEqual((saved['created'], saved['completed'], saved['failed']), (1, 0, 1))
            self.assertNotIn('robots', saved)
            self.assertNotIn('x', saved['failures'][0])

    def test_only_current_stress_run_can_create_while_manual_commands_blocked(self):
        manager = ScenarioManager()
        manager._status.update(state='running', name=STRESS_NAME, run_id='run', robot_ids=['robot1'])
        self.assertTrue(manager.order_workload)
        manager.ensure_order_available('run')
        for token in (None, 'stale'):
            with self.assertRaises(ValueError):
                manager.ensure_order_available(token)
        with self.assertRaises(ValueError):
            manager.ensure_available()
        manager.interrupt()
        with self.assertRaises(ValueError):
            manager.ensure_order_available('run')
        manager._status['state'] = 'completed'
        manager.ensure_order_available()

    def test_start_requires_simulation_ai_and_idle_fleet(self):
        manager = ScenarioManager()
        for mode, operation in [('real', 'ai'), ('simulation', 'algorithm')]:
            with patch('backend.app.services.scenario_service.mode_manager') as modes:
                modes.mode, modes.operation_mode = mode, operation
                with self.assertRaises(ValueError):
                    manager._start_order_stress({'description': 'test'})
        with patch('backend.app.services.scenario_service.mode_manager') as modes, patch('backend.app.services.scenario_service.robot_states', return_value={'robot1': dict(connected=True, fresh=True, status='NAVIGATING', active=True)}):
            modes.mode, modes.operation_mode = 'simulation', 'ai'
            with self.assertRaises(ValueError):
                manager._start_order_stress({'description': 'test'})

    def test_loop_refills_to_limit_and_stop_leaves_orders_untouched(self):
        asyncio.run(self._exercise_loop())

    async def _exercise_loop(self):
        from backend.app.services.order_manager import order_manager
        manager = ScenarioManager()
        manager._status.update(state='running', name=STRESS_NAME, run_id='a'*32, robot_ids=[])
        orders = []
        states = {'robot1': dict(robot_id='robot1', status='IDLE', x=0., y=0., active=False)}
        def create(items, quantity, **kwargs):
            self.assertLessEqual(quantity, 5)
            self.assertEqual(kwargs['scenario_run_id'], 'a'*32)
            row = dict(order_id=str(len(orders)), items=items, total_quantity=quantity, status='PENDING')
            orders.append(row)
            return row
        polls = 0
        async def sleep(_):
            nonlocal polls
            polls += 1
            if polls == 1:
                orders[0].update(status='COMPLETED', phase='COMPLETED')
            elif polls == 2:
                manager.interrupt()
        with TemporaryDirectory() as directory, patch.object(order_manager, 'list_orders', side_effect=lambda: orders), patch.object(order_manager, 'create_order', side_effect=create), patch('backend.app.services.scenario_service.robot_states', return_value=states), patch('backend.app.services.scenario_service.load_route_graph', return_value={}), patch('backend.app.services.scenario_service.mode_manager') as modes, patch('backend.app.services.scenario_service.asyncio.sleep', side_effect=sleep):
            modes.mode, modes.operation_mode = 'simulation', 'ai'
            recorder = OrderStressTest('a'*32, Path(directory), seed=4)
            await manager._run_order_stress(recorder, {})
            self.assertEqual(len(orders), MAX_OPEN_ORDERS+1)
            self.assertEqual(sum(o['status'] == 'PENDING' for o in orders), MAX_OPEN_ORDERS)
            self.assertEqual(manager.status()['state'], 'completed')
            self.assertEqual(recorder.summary['state'], 'stopped')
            self.assertEqual(recorder.summary['open_orders'], MAX_OPEN_ORDERS)


if __name__ == '__main__':
    unittest.main()


def test_stress_http_controls_persistent_history_and_download(tmp_path, monkeypatch):
    import httpx
    from fastapi import FastAPI
    from backend.app.routers import scenarios
    from backend.app.services.order_stress_test import unfinished_order
    manager = ScenarioManager()
    monkeypatch.setattr(scenarios, 'scenario_manager', manager)
    monkeypatch.setattr(scenarios, 'RECORD_DIR', tmp_path)
    recorder = OrderStressTest('a'*32, tmp_path, seed=12)
    returning = dict(order_id='1', status='COMPLETED', phase='RETURNING_TO_STATION')
    recorder.created(returning)
    summary, _ = recorder.sample([returning], {})
    assert unfinished_order(returning) and summary['open_orders'] == 1 and summary['completed'] == 0
    application = FastAPI()
    application.include_router(scenarios.router)
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url='http://test') as client:
            listing = (await client.get('/api/scenarios')).json()
            assert STRESS_NAME in {row['name'] for row in listing}
            assert (await client.get('/api/scenarios/status')).json()['state'] == 'idle'
            assert (await client.get('/api/scenarios/records')).json()[0]['state'] == 'interrupted'
            download = await client.get('/api/scenarios/records/'+'a'*32)
            assert download.status_code == 200
            assert download.json()['seed'] == 12
            assert unquote(download.headers['content-disposition'].split("UTF-8''", 1)[1]) == recorder.record_path.name
            assert download.json()['file_name'] == recorder.record_path.name
            assert not list(tmp_path.glob('download-*'))
            # Existing UUID filenames remain accessible through the same run URL.
            legacy = tmp_path / ('b'*32+'.json')
            legacy.write_text(json.dumps(dict(summary, run_id='b'*32)))
            old_download = await client.get('/api/scenarios/records/'+'b'*32)
            assert old_download.status_code == 200
            assert old_download.json()['file_name'].startswith('자동배차테스트[')
            legacy.unlink()
            assert (await client.get('/api/scenarios/records/invalid')).status_code == 404
            assert (await client.get('/api/scenarios/records/'+'f'*32)).status_code == 404
            manager._status.update(name=STRESS_NAME, run_id='a'*32, state='running', robot_ids=[])
            assert (await client.post('/api/scenarios/reset', json={'run_id': 'a'*32})).status_code == 409
            async def finish():
                await asyncio.sleep(0)
                recorder.finish('stopped', 'operator stop')
                manager._status['state'] = 'completed'
            manager._task = asyncio.create_task(finish())
            assert (await client.post('/api/scenarios/stop')).json()['state'] == 'completed'
            assert (await client.post('/api/scenarios/reset', json={'run_id': 'b'*32})).status_code == 409
            assert (await client.post('/api/scenarios/reset', json={'run_id': 'a'*32})).json()['state'] == 'idle'
            assert recorder.record_path.is_file()
            assert (await client.get('/api/scenarios/records')).json()[0]['state'] == 'stopped'

    asyncio.run(exercise())


def test_stress_uses_normal_order_validation_and_order_tick(monkeypatch):
    from tests.test_order_flow import _reset
    from backend.app.services.order_manager import order_manager
    from backend.app.services.scenario_service import scenario_manager
    from backend.app.services.simulation_order_runner import simulation_order_runner
    _reset(monkeypatch)
    monkeypatch.setattr(scenario_manager, '_status', dict(state='running', name=STRESS_NAME, run_id='run', robot_ids=[]))
    with unittest.TestCase().assertRaises(ValueError):
        order_manager.create_order(dict(A=1, B=0, C=0, D=0), 1)
    # The normal order service still applies the 5-item limit to the internal run.
    with unittest.TestCase().assertRaises(ValueError):
        order_manager.create_order(dict(A=6, B=0, C=0, D=0), 6, scenario_run_id='run')
    order = order_manager.create_order(dict(A=1, B=0, C=0, D=0), 1, scenario_run_id='run')
    async def exercise():
        await order_manager.tick()
        assert order_manager.get_order(order['order_id'])['robot_id'] == 'robot1'
        await asyncio.gather(*list(simulation_order_runner.tasks.values()))
        assert order_manager.get_order(order['order_id'])['status'] == 'COMPLETED'
    asyncio.run(exercise())
