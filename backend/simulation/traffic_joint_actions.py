"""격리된 3대 가상 로봇으로 교통조정안을 선택하고 기존 FMS 이동기를 실행한다.

실제 백엔드 singleton, 주문, ROS/Nav2에는 명령하지 않는다.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
from time import monotonic
from unittest.mock import patch

from backend.app.services.mock_data import MockFmsStore
from backend.app.services.occupancy import occupied_resource
from backend.app.services.reservation import ReservationTable
from backend.app.services.traffic_joint_actions import TrafficDecision, TrafficSituation
from backend.app.services.traffic_metrics import TrafficRunMetrics


class TestSelector:
    name = "test"

    def __init__(self, choice=None):
        self.choice = choice
        self.calls = 0

    def select_choice(self, state, candidates, instructions, **kwargs):
        self.calls += 1
        return {"choice": self.choice or next(key for key in candidates if key != "HOLD_ALL")}


class JointActionSimulation:
    def __init__(self, situation: TrafficSituation):
        self.situation = situation
        nodes = situation.planner.nodes

        def get_node(value):
            x, y = nodes[str(value)]
            return {"id": str(value), "x": x, "y": y}

        with patch("backend.app.services.mock_data.get_node", side_effect=get_node), patch(
            "backend.app.services.mock_data.monotonic", return_value=0.0
        ):
            self.store = MockFmsStore(ReservationTable())
        with self.store._lock:
            self.store._robots.clear()
            self.store._robots.update(situation.robot_states())
        self._initial = deepcopy(self.store._robots)
        self._used = False

    def execute(self, decision: TrafficDecision, *, max_step_seconds=300.0, event_sink=None) -> dict:
        plan = self.situation.checked_plan(decision)
        traffic = self.store._traffic
        # 이 API는 격리 시뮬레이션만 받는다. 이미 승인된 예약·주행을 교체하지 않는다.
        with self.store._lock:
            if (
                self._used
                or self.store._robots != self._initial
                or traffic._reservations.snapshot()
                or traffic._protected_resources
                or traffic.has_active_requests()
            ):
                raise ValueError("STALE_SIMULATION: 실행 상태 또는 예약이 바뀌었습니다.")
            self._used = True
        events = []

        def emit(event):
            events.append(event)
            if event_sink is not None:
                event_sink(deepcopy(event))

        now = 1.0
        metrics = TrafficRunMetrics(
            self.situation.requests, self.situation.planner.nodes,
            requested_at=now, clock_kind="VIRTUAL_SIMULATION",
        )
        metrics.begin_execution(now, selection_seconds=None)
        capacity_checks = 0
        for index, step in enumerate(plan.steps):
            with self.store._lock:
                traffic.request_navigation_after_stop(
                    step.robot_id, step.path[-1], now=now,
                    graph=self.situation.graph, required_path=step.path,
                )
            metrics.start_step(index + 1, step, now)
            emit({
                "event": "STEP_STARTED", "step": index, "robot_id": step.robot_id,
                "action": step.action, "path": list(step.path),
            })
            deadline = now + max_step_seconds
            while self.store._robots[step.robot_id]["goal_node"] is not None:
                now += 0.05
                if now > deadline:
                    # Mock은 이 잠금 안에서 즉시 정지할 수 있다. 실제 실행기로 재사용 금지.
                    with self.store._lock:
                        for rid in self.store._robots:
                            traffic.cancel_navigation_after_stop(rid)
                    raise RuntimeError(f"시뮬레이션 단계 시간 초과: {step.robot_id}")
                with self.store._lock:
                    traffic.retry_waiting(now, self.situation.graph, self.situation.planner)
                    for rid in self.store._robots:
                        self.store.advance_mock_robot(
                            rid, 0.05, now=now, graph=self.situation.graph,
                            nodes=self.situation.planner.nodes,
                        )
                    occupied = [
                        occupied_resource(robot, self.situation.graph)
                        for robot in self.store._robots.values()
                    ]
                    if len(set(occupied)) != len(occupied):
                        raise RuntimeError("노드/물리 엣지 동시 점유 위반")
                    capacity_checks += 1
            if self.store._robots[step.robot_id]["occupied_node"] != step.path[-1]:
                raise RuntimeError("단계 목적지에 도착하지 않았습니다.")
            metrics.complete_step(step, now)
            emit({
                "event": "STEP_COMPLETED", "step": index, "robot_id": step.robot_id,
                "node": step.path[-1],
            })
            if index + 1 < len(plan.steps):
                now += 0.5
        completed = all(
            self.store._robots[rid]["occupied_node"] == request["node"]
            for rid, request in self.situation.requests.items()
        )
        progress_plans = any(p.steps for p in self.situation.plans.values())
        outcome = "COMPLETED" if completed else ("HELD" if progress_plans else "UNRESOLVED")
        return {
            "status": outcome,
            "execution_scope": "ISOLATED_SIMULATION",
            "real_robot_commands_sent": False,
            "decision": decision.to_dict(),
            "selected_joint_action": plan.to_dict(),
            "steps_completed": len(plan.steps),
            "capacity_checks": capacity_checks,
            "capacity_violations": 0,
            "events": events,
            "candidate_estimates": self.situation.estimates,
            "actual_metrics": metrics.report(now, outcome=outcome),
            "final_nodes": {rid: r["occupied_node"] for rid, r in self.store._robots.items()},
        }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input", type=Path,
        default=Path(__file__).resolve().parents[2] / "tests" / "traffic_situation_cases.json",
    )
    parser.add_argument("--case", default="all")
    parser.add_argument("--selector", choices=("test", "kev"), default="test")
    parser.add_argument("--choice", help="test selector에서 선택할 조정안 ID")
    parser.add_argument("--model", default="kev-08b")
    parser.add_argument("--host", default="http://127.0.0.1:8011")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--verbose", action="store_true", help="입력 요약·선택·이동 단계를 출력")
    args = parser.parse_args(argv)
    if args.selector == "kev" and args.choice:
        parser.error("--choice는 --selector test에서만 사용할 수 있습니다.")
    fixtures = json.loads(args.input.read_text(encoding="utf-8"))
    cases = {
        name: {**case, "graph": deepcopy(fixtures["graph"])}
        for name, case in fixtures["cases"].items()
    }
    if args.case != "all":
        cases = {args.case: cases[args.case]}
    if args.selector == "kev":
        from backend.app.services.route_selector.kev_selector import KevSelector

        selector = KevSelector(model=args.model, host=args.host)
    else:
        selector = TestSelector(args.choice)
    reports = {}
    for name, payload in cases.items():
        scene = TrafficSituation(payload)
        simulation = JointActionSimulation(scene)
        if args.verbose:
            print(f"\n[INPUT] {name}: snapshot={payload['snapshot_id']}", flush=True)
            for rid, request in scene.requests.items():
                print(f"  {rid}: {list(request['path'])}", flush=True)
            print(f"[JOINT ACTIONS] {', '.join(scene.plans)}", flush=True)
            print(f"[SELECT] {args.selector}에 교통조정안 선택 요청", flush=True)
        decision_started = monotonic()
        decision = scene.decide(selector)
        decision_wall_seconds = monotonic() - decision_started
        if args.verbose:
            print(f"[DECISION] {decision.selection_source}: {decision.joint_action_id}", flush=True)

        def show_event(event):
            print(f"[FMS] {json.dumps(event, ensure_ascii=False)}", flush=True)

        reports[name] = {
            **simulation.execute(decision, event_sink=show_event if args.verbose else None),
            "preview": scene.preview(),
            "decision_wall_seconds": decision_wall_seconds,
        }
        report = reports[name]
        print(
            f"{name}: {report['status']} / {decision.selection_source} / "
            f"{decision.joint_action_id} / steps={report['steps_completed']} / "
            f"capacity_violations={report['capacity_violations']}"
        )
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(reports, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
