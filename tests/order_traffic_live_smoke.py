"""현재 관제를 변경하지 않는 일반 주문 시험. 별도 가상 로봇 + 실제 KEV 서버.

실제 실행: PYTHONNOUSERSITE=1 ~/venv/robot/bin/python tests/order_traffic_live_smoke.py
시험용 이동 속도 5m/s. 운영 속도·완료 시간 성능을 검증하는 시험이 아니다.
"""

import asyncio
import argparse
from contextlib import ExitStack
import json
from pathlib import Path
import sys
from time import monotonic
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app.services import mock_data, mode_service, order_manager, order_route_dispatcher
from backend.app.services import simulation_order_runner, workstation_manager, scenario_service
from backend.app.services import order_traffic_service
from backend.app.services.occupancy import occupied_resource
from backend.app.services.reservation import ReservationTable
from backend.app.services.route_graph import load_route_graph
from backend.app.services.traffic_route_provider import generate_traffic_route


async def run(same_pickup=False):
    graph = load_route_graph()
    store = mock_data.MockFmsStore(ReservationTable())
    orders = order_manager.OrderManager()
    runner = simulation_order_runner.SimulationOrderRunner()
    runner.work_seconds = .5 if same_pickup else .05
    stations = workstation_manager.WorkstationManager()
    mode = SimpleNamespace(mode="simulation", operation_mode="ai")
    scenario = scenario_service.ScenarioManager()
    started = monotonic()
    output = Path(__file__).resolve().parents[1] / (
        "logs/order-traffic/same-pickup-live-smoke.json" if same_pickup else "logs/order-traffic/live-smoke.json")
    report = dict(state="running", model="kev-08b", test_speed_mps=5,
                  note="별도 가상 로봇. 실제 관제 주문은 수정하지 않음.")
    with ExitStack() as stack:
        original_record = order_traffic_service._record

        def record(order, event):
            if same_pickup and order["robot_id"] == "robot2" and order["phase"] == "MOVING_TO_PICKUP" and event["state"] == "APPLIED":
                first = next(iter(orders.orders.values()))
                report["pickup_handoff"] = dict(first_status=first["status"], first_phase=first["phase"],
                                                first_node=store._robots["robot1"]["occupied_node"],
                                                second_started_before_first_completed=first["status"] == "IN_PROGRESS")
            return original_record(order, event)

        stack.enter_context(patch.object(order_traffic_service, "_record", record))
        stack.enter_context(patch.dict("os.environ", {"FMS_ORDER_TRAFFIC_ENABLED": "1",
                            "FMS_ORDER_ROUTE_SOURCE": "EXISTING_SHORTEST_PATH"}))
        bindings = [(mock_data, "mock_fms", store), (mode_service, "mode_manager", mode),
                    (order_route_dispatcher, "mode_manager", mode),
                    (order_manager, "order_manager", orders), (order_manager, "mode_manager", mode),
                    (order_manager, "scenario_manager", scenario),
                    (order_manager, "workstation_manager", stations),
                    (simulation_order_runner, "simulation_order_runner", runner),
                    (simulation_order_runner, "mode_manager", mode), (simulation_order_runner, "mock_fms", store),
                    (simulation_order_runner, "workstation_manager", stations),
                    (workstation_manager, "workstation_manager", stations),
                    (mock_data, "SIMULATION_SPEED_MPS", 5.0)]
        for module, key, value in bindings:
            stack.enter_context(patch.object(module, key, value))
        store._traffic.speed_mps = 5.0
        original = store.advance_mock_robot
        stack.enter_context(patch.object(store, "advance_mock_robot",
                            lambda *a, **kw: original(*a, speed_mps=5.0, **kw)))
        try:
            # 기존 KEV 경로 생성기에도 회피 조건이 실제 적용되는지 별도 확인한다.
            try:
                reroute = await asyncio.to_thread(generate_traffic_route, graph, "9", "11", source="KEV",
                                                 blocked_nodes=["7"], selector="kev", model="kev-08b")
                assert "7" not in reroute["path"]
                report["kev_route_generation"] = dict(state="completed", **reroute)
            except (ValueError, RuntimeError) as exc:
                # 경로 생성 모델의 실패를 숨기거나 A* 성공으로 대체하지 않는다.
                report["kev_route_generation"] = dict(state="failed", error=str(exc))
            second_items = dict(A=0, B=3, C=0, D=0) if same_pickup else dict(A=0, B=0, C=1, D=0)
            for items in [dict(A=1, B=0, C=0, D=0), second_items]:
                orders.create_order(items, sum(items.values()), selector="kev", model="kev-08b")
            previous, overlap_checks = monotonic(), 0
            while monotonic() - started < 40:
                await orders.tick()
                now = monotonic()
                store.advance_simulation(now-previous, now)
                previous = now
                resources = [occupied_resource(r, graph) for r in store._robots.values()]
                assert len(resources) == len(set(resources)), "중복 점유"
                overlap_checks += 1
                rows = orders.list_orders()
                if any(r["status"] == "FAILED" or r.get("return_error") for r in rows):
                    raise ValueError(json.dumps(rows, ensure_ascii=False))
                if all(r["status"] == r["phase"] == "COMPLETED" for r in rows):
                    if same_pickup:
                        assert report["pickup_handoff"]["second_started_before_first_completed"]
                        assert report["pickup_handoff"]["first_node"] != "5"
                    report.update(state="completed", overlap_checks=overlap_checks)
                    break
                await asyncio.sleep(.01)
            if report["state"] != "completed":
                raise ValueError("40초 시험 한도 초과")
        except Exception as exc:
            report.update(state="failed", error=str(exc))
        finally:
            await runner.shutdown()
            report.update(elapsed_seconds=monotonic()-started, orders=orders.list_orders())
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n")
    print(json.dumps({k:v for k,v in report.items() if k != "orders"}, ensure_ascii=False))
    print(f"결과: {output}")
    return report["state"] == "completed"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--same-pickup", action="store_true", help="A 1개와 B 3개의 같은 픽업대 인계 확인")
    raise SystemExit(0 if asyncio.run(run(parser.parse_args().same_pickup)) else 1)
