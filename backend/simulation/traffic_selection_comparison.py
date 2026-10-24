"""같은 입력·후보·격리 이동기로 요청 순서/계산 최솟값/KEV 선택을 비교한다."""

import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime
import json
import math
from pathlib import Path
from statistics import mean
from time import monotonic

from backend.app.services.traffic_joint_actions import (
    CHOICE_INSTRUCTIONS, SELECTION_OBJECTIVE, TrafficSituation,
)
from backend.app.services.traffic_situation_adapter import build_traffic_situation_input
from backend.app.services.pathfinding import DistanceAStar
from backend.app.services.route_graph import load_route_graph
from backend.simulation.traffic_joint_actions import JointActionSimulation

STRATEGIES = ("request_order", "min_mean_completion", "kev")
LABELS = {
    "request_order": "요청 순서", "min_mean_completion": "계산상 빠른 방법", "kev": "KEV",
}


def load_cases():
    """기존 지도 시험을 재사용한다. 관제의 상태나 주문을 읽거나 바꾸지 않는다."""
    from backend.app.config import BASE_DIR

    config = json.loads((Path(BASE_DIR) / "tests/traffic_scenarios.json").read_text())
    graph = load_route_graph()
    nodes = DistanceAStar(graph).nodes
    cases = {}
    names = {
        "kev_joint_wait_or_reroute", "kev_joint_yield_or_reroute",
        "kev_joint_proceed_wait", "kev_joint_hold",
    }
    for scenario in config["scenarios"]:
        if scenario["name"] not in names:
            continue
        options = scenario["traffic_joint_action"]
        states, requests, metadata = {}, [], {}
        for index, spec in enumerate(scenario["robots"], 1):
            rid, start = spec["id"], spec["start"]
            x, y = nodes[start]
            states[rid] = dict(
                status="IDLE", occupied_node=start, occupied_edge=None,
                navigation_id=None, route=None, goal_node=None,
                connected=True, fresh=True, active=False, x=x, y=y,
            )
            if spec["goal"] is not None:
                requests.append(dict(
                    order=dict(robot_id=rid, selector="kev", model=options.get("model", "kev-08b")),
                    current_node=start, node=spec["goal"], result=dict(path=options["paths"][rid]),
                ))
                metadata[rid] = dict(request_order=index, waiting_seconds=0)
        payload = build_traffic_situation_input(
            graph=graph, snapshot_id=f"comparison-{scenario['name']}",
            route_requests=requests, robot_states=states, reservations=[], protected_resources={},
            wait_allowed_nodes=options["wait_allowed_nodes"], request_metadata=metadata,
            config={key: options[key] for key in (
                "blocked_nodes", "blocked_edges", "reroute_paths", "yield_paths"
            ) if key in options},
        )
        cases[scenario["name"]] = dict(description=scenario["description"], payload=payload)
        if scenario["name"] == "kev_joint_proceed_wait":
            reverse = deepcopy(payload)
            reverse["snapshot_id"] += "-reverse-request-order"
            for r in reverse["route_requests"]:
                r["request_order"] = 4 - r["request_order"]
            cases[scenario["name"] + "_reverse_requests"] = dict(
                description="3대 통과 · 요청 순서를 반대로", payload=reverse,
            )
    # 같은 작은 교차로에서 긴 이동과 짧은 이동의 순서를 비교한다.
    fixtures = json.loads((Path(BASE_DIR) / "tests/traffic_situation_cases.json").read_text())
    payload = {**deepcopy(fixtures["cases"]["three_robot_node"]), "graph": fixtures["graph"]}
    cases["three_robot_different_distances"] = dict(
        description="3대 통과 · 이동 거리가 서로 다름", payload=payload,
    )
    return cases


class PresentedSelector:
    """후보를 빼거나 정답을 알려주지 않고, 표시 순서만 바꾼다."""

    def __init__(self, selector, reverse):
        self.selector, self.reverse = selector, reverse
        self.name = selector.name

    def select_choice(self, state, candidates, instructions, **kwargs):
        shown = dict(reversed(list(candidates.items()))) if self.reverse else candidates
        return self.selector.select_choice(state, shown, instructions, **kwargs)


def run_trial(case, strategy, *, selector=None, repeat=1, reverse=False):
    started = monotonic()
    scene = TrafficSituation(deepcopy(case["payload"]))
    prepared = monotonic()
    row = dict(
        strategy=strategy, repeat=repeat, candidate_presentation="REVERSED" if reverse else "ORIGINAL",
        snapshot_hash=scene.snapshot_hash, candidate_ids=list(scene.plans),
        candidate_estimates=scene.estimates, rejected_candidates=scene.rejected,
        scene_build_wall_seconds=prepared - started,
    )
    selection_started = monotonic()
    prior_trace = getattr(selector, "last_inference", None)
    try:
        decision = scene.decide(
            PresentedSelector(selector, reverse) if strategy == "kev" and selector is not None else None,
            strategy=strategy,
        )
        row["selection_wall_seconds"] = monotonic() - selection_started
        row["decision"] = decision.to_dict()
        row["kev_requests"] = int(decision.selection_source == "KEV")
        result = JointActionSimulation(scene).execute(decision)
        actual = result["actual_metrics"]
        selected = scene.estimates[decision.joint_action_id]
        values = [e["mean_goal_completion_seconds"] for e in scene.estimates.values()
                  if e["mean_goal_completion_seconds"] is not None]
        best = min(values) if values else None
        chosen_mean = selected["mean_goal_completion_seconds"]
        row.update(
            status=result["status"], selected_id=decision.joint_action_id,
            selected_estimated_mean_seconds=chosen_mean,
            best_offered_estimated_mean_seconds=best,
            matched_best_offered_mean=None if best is None or chosen_mean is None else math.isclose(best, chosen_mean, abs_tol=1e-9),
            excess_estimated_mean_seconds=None if best is None or chosen_mean is None else chosen_mean - best,
            actual_metrics=actual, capacity_checks=result["capacity_checks"],
            capacity_violations=result["capacity_violations"], final_nodes=result["final_nodes"],
        )
    except Exception as exc:
        row.update(status="FAILED", error=f"{type(exc).__name__}: {exc}",
                   selection_wall_seconds=row.get("selection_wall_seconds", monotonic() - selection_started))
    if strategy == "kev" and selector is not None:
        # 재사용한 선택기의 이전 응답을 실패한 요청의 증거로 붙이지 않는다.
        trace = getattr(selector, "last_inference", None)
        if trace is not None and trace is not prior_trace:
            row["inference"] = deepcopy(trace)
    return row


def summarize(rows):
    completed = [r for r in rows if r["status"] == "COMPLETED"]
    evaluable = [r for r in rows if r.get("matched_best_offered_mean") is not None]
    means = [r["actual_metrics"]["mean_goal_completion_seconds"] for r in completed
             if r["actual_metrics"]["mean_goal_completion_seconds"] is not None]
    return dict(
        trials=len(rows), outcomes=dict(Counter(r["status"] for r in rows)),
        evaluated_choices=len(evaluable), matched_best_offered_mean=sum(r["matched_best_offered_mean"] for r in evaluable),
        mean_virtual_goal_completion_seconds=mean(means) if means else None,
        mean_selection_wall_seconds=mean(r["selection_wall_seconds"] for r in rows) if rows else None,
    )


def write_reports(report, output):
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
    lines = [
        "# 교통조정 선택 방식 비교", "",
        "같은 입력·후보·검사·가상 이동기를 사용했다. 완료 시간은 **가상 시계**, 선택 지연은 **실제 경과 시간**이다.",
        "두 시간을 합쳐 실측 완료 시간으로 표시하지 않는다. 최솟값 일치는 제공된 후보의 예상 평균 기준이다.", "",
        "| 상황 | 선택 방식 | 완료/전체 | 예상 최솟값 일치/평가 | 관측 평균 도착(가상 초) | 선택 지연(실제 초) |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for case in report["cases"].values():
        for strategy in STRATEGIES:
            rows = [r for r in case["trials"] if r["strategy"] == strategy]
            if not rows:
                continue
            s = summarize(rows)
            value = s["mean_virtual_goal_completion_seconds"]
            average = "미완료" if value is None else f"{value:.3f}"
            lines.append(f"| {case['description']} | {LABELS[strategy]} | {s['outcomes'].get('COMPLETED', 0)}/{s['trials']} | {s['matched_best_offered_mean']}/{s['evaluated_choices']} | {average} | {s['mean_selection_wall_seconds']:.4f} |")
    lines.extend(["", "전원 대기는 미완료로 남기며 0초 완료로 평균에 넣지 않는다.",
                  "장기 대기 방지·처리량·이동 중 상태 대응은 이 단일 상황 시험으로 검증하지 않았다.",
                  "선택한 후보 ID, 모든 예상 값, 입력, 제외 이유, 실제 KEV 요청·응답은 같은 이름의 JSON에 있다."])
    output.with_suffix(".md").write_text("\n".join(lines) + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--case", action="append", help="반복 지정 가능. 생략 시 전체 6상황")
    parser.add_argument("--without-kev", action="store_true", help="모델 호출 없이 두 일반 코드 방식만 시험")
    parser.add_argument("--model", default="kev-08b")
    parser.add_argument("--host", default="http://127.0.0.1:8011")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.repeats < 1:
        parser.error("repeats는 1 이상이어야 합니다.")
    cases = load_cases()
    if args.case:
        unknown = set(args.case) - cases.keys()
        if unknown:
            parser.error(f"알 수 없는 시험: {sorted(unknown)}")
        cases = {key: cases[key] for key in args.case}
    output = args.output or Path("logs/traffic-joint-actions") / f"selection-comparison-{datetime.now():%Y%m%d-%H%M%S}.json"
    strategies = STRATEGIES[:2] if args.without_kev else STRATEGIES
    report = dict(
        state="RUNNING", objective=SELECTION_OBJECTIVE, instructions=CHOICE_INSTRUCTIONS,
        execution_scope="ISOLATED_SIMULATION", route_source="SUPPLIED_PATHS",
        repeat_count=args.repeats, tie_breaks=["max_turn_wait_seconds", "total_distance_m", "request_order"],
        starvation_prevention="NOT_IMPLEMENTED_TIE_BREAK_ONLY", cases={},
    )
    for name, case in cases.items():
        entry = report["cases"][name] = {**deepcopy(case), "trials": []}
        for repeat in range(1, args.repeats + 1):
            for strategy in strategies:
                selector = None
                if strategy == "kev":
                    from backend.app.services.route_selector.kev_selector import KevSelector
                    selector = KevSelector(model=args.model, host=args.host, timeout_seconds=20)
                row = run_trial(case, strategy, selector=selector, repeat=repeat, reverse=repeat % 2 == 0)
                entry["trials"].append(row)
                write_reports(report, output)
                print(f"{name} / {repeat} / {strategy}: {row['status']} / {row.get('selected_id', row.get('error'))}", flush=True)
    failed = any(r["status"] == "FAILED" for c in report["cases"].values() for r in c["trials"])
    report["state"] = "FAILED" if failed else "COMPLETED"
    write_reports(report, output)
    print(f"결과: {output.with_suffix('.md')}")
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
