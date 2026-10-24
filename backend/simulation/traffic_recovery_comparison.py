"""같은 허용 동작·한도로 BFS, 코드 DFS, KEV 제안 DFS를 격리 비교한다.

--run만 실제 KEV 서버에 요청한다. 일반 주문·실행 중인 관제 로봇에는 명령하지 않는다.
"""

import argparse
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
from time import monotonic

from backend.app.config import BASE_DIR
from backend.app.services.route_graph import load_route_graph
from backend.app.services.scenario_service import load_config
from backend.app.services.traffic_joint_actions import TrafficSituation, fingerprint
from backend.app.services.traffic_situation_adapter import build_traffic_situation_input
from backend.simulation.traffic_joint_actions import JointActionSimulation


def comparison_cases():
    scenario = next(s for s in load_config()["scenarios"] if s["name"] == "search_two_robot_yield")
    options = scenario["traffic_joint_action"]
    states, requests, metadata = {}, [], {}
    for index, spec in enumerate(scenario["robots"], 1):
        rid, start, goal = spec["id"], spec["start"], spec["goal"]
        states[rid] = dict(status="IDLE", occupied_node=start, occupied_edge=None,
                           navigation_id=None, route=None, goal_node=None, connected=True, fresh=True, active=False)
        if goal is not None:
            requests.append(dict(order=dict(robot_id=rid, selector="kev", model="kev-08b"),
                                 current_node=start, node=goal, result=dict(path=options["paths"][rid])))
            metadata[rid] = dict(request_order=index, waiting_seconds=0)
    base = build_traffic_situation_input(
        graph=load_route_graph(), route_requests=requests, robot_states=states,
        reservations=[], protected_resources={}, wait_allowed_nodes=options["wait_allowed_nodes"],
        snapshot_id="recovery-comparison", request_metadata=metadata,
        config=dict(yield_paths=options["yield_paths"], recovery_search=dict(max_checks=128, max_steps=8, max_seconds=10.0)),
    )
    reverse = deepcopy(base)
    for index, request in enumerate(reversed(reverse["route_requests"])):
        request["request_order"] = index
    missing = deepcopy(base)
    del missing["config"]["yield_paths"]["robot3"]
    return {"two_yields": base, "reverse_request_order": reverse, "missing_second_yield": missing}


def run_comparison(cases, *, selector_factory=None, strategies=("BREADTH_FIRST", "DEPTH_FIRST", "KEV_GUIDED"),
                   guidance_options=None, branch_policy="DISCARD", guidance_budget_policy="STOP"):
    if selector_factory is None:
        from backend.app.services.route_selector.kev_selector import KevSelector

        selector_factory = lambda: KevSelector(model="kev-08b", timeout_seconds=2.0)
    rows = []
    for name, base in cases.items():
        common_hash = fingerprint(base)
        for strategy in strategies:
            payload = deepcopy(base)
            payload["config"].update(recovery_strategy=strategy, recovery_guidance={"max_calls": 8, "request_seconds": 2.0, **(guidance_options or {})})
            if strategy in ("DEPENDENCY_DFS", "KEV_DEPENDENCY"):
                payload["config"]["recovery_branch_policy"] = branch_policy
                if strategy == "KEV_DEPENDENCY":
                    payload["config"]["recovery_guidance_budget_policy"] = guidance_budget_policy
            started = monotonic()
            scene = TrafficSituation(payload, recovery_selector=selector_factory() if strategy in ("KEV_GUIDED", "KEV_DEPENDENCY") else None)
            planning_seconds = monotonic() - started
            result = JointActionSimulation(scene).execute(scene.decide())
            row = dict(case=name, strategy=strategy, common_input_sha256=common_hash,
                       planning_seconds=planning_seconds, search=scene.search_report, result=result)
            rows.append(row)
            print(name, strategy, scene.search_report["status"],
                  f"checks={scene.search_report['checks']}", f"calls={scene.search_report['model_calls']}",
                  f"planning={planning_seconds:.3f}s", result["status"], flush=True)
    return dict(execution_scope="ISOLATED_SIMULATION", live_fms_commands_sent=False,
                objective="FIND_A_VALID_PLAN_NOT_THROUGHPUT_OPTIMIZATION", inputs=deepcopy(cases), runs=rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    cases = comparison_cases()
    if not args.run:
        print(json.dumps({name: dict(input_sha256=fingerprint(case), limits=case["config"]["recovery_search"])
                          for name, case in cases.items()}, ensure_ascii=False, indent=2))
        return
    output = args.output or Path(BASE_DIR) / "logs/traffic-recovery" / datetime.now().strftime("%Y%m%d-%H%M%S")
    output.mkdir(parents=True, exist_ok=True)
    report = run_comparison(cases)
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    lines = ["# 복구 탐색 비교", "", "격리된 가상 이동기 시험. 일반 관제·실기체 명령 없음.", "",
             "| 상황 | 방식 | 탐색 결과 | 예약 검사 | 모델 호출 | 전체 계획 시간 |", "| --- | --- | --- | ---: | ---: | ---: |"]
    for row in report["runs"]:
        r = row["search"]
        lines.append(f"| {row['case']} | {row['strategy']} | {r['status']} | {r['checks']} | {r['model_calls']} | {row['planning_seconds']:.4f}초 |")
    lines.extend(["", "BFS와 DFS는 탐색 방식이 다르다. 제안 효과는 같은 DFS의 코드 순서와 KEV 순서를 비교한다.",
                  "실행 시간 지표는 별도 가상 시계이며, 위 계획 시간은 모델 응답을 포함한 실제 경과 시간이다."])
    (output / "summary.md").write_text("\n".join(lines) + "\n")
    print("REPORT", str(output.resolve()), flush=True)


if __name__ == "__main__":
    main()
