"""코드 탐색·KEV 전략 탐색·jhlee 전략을 같은 정지 장면에서 비교한다.

기본 실행은 코드 준비 검사만 한다. --run만 실제 KEV를 호출하며 자동 대체 응답을
사용하지 않는다. 기존 관제·주문·ROS에는 명령하지 않고 결과 폴더를 덮어쓰지 않는다.
"""

import argparse
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime
import hashlib
import json
from pathlib import Path
from time import monotonic
from zoneinfo import ZoneInfo

from backend.app.config import BASE_DIR
from backend.app.services.route_service import plan_route
from backend.app.services.traffic_joint_actions import TrafficDecision, fingerprint
from backend.app.services.traffic_metrics import estimate_plan
from backend.app.services.traffic_situation_adapter import build_traffic_situation_input
from backend.simulation.traffic_joint_actions import JointActionSimulation
from .traffic_strategy_jhlee import load_jhlee, run_jhlee
from .traffic_strategy_search import ComparisonLimits, PlanningBudget, PlanningScene, search_strategies

CATALOG = Path(BASE_DIR) / "tests/traffic_strategy_cases.json"
METHODS = ("CODE_SEARCH", "KEV_STRATEGY_SEARCH", "JHLEE_STRATEGY")
COMPARISONS = dict(three_way=METHODS,
                   repeat_vs_rank=("CODE_SEARCH", "KEV_STRATEGY_SEARCH", "KEV_PROBABILITY_ORDER"))
LABELS = dict(CODE_SEARCH="코드 탐색", KEV_STRATEGY_SEARCH="KEV 전략 탐색", JHLEE_STRATEGY="jhlee 전략",
              KEV_PROBABILITY_ORDER="KEV 확률 순서 재사용")
PROFILES = dict(equal_calls="동일 호출 상한 비교", equal_time="동일 시간 상한 비교")


def profile_limits(catalog, profile):
    if profile not in PROFILES:
        raise ValueError("알 수 없는 비교 조건입니다.")
    limits = ComparisonLimits(**catalog["limits"])
    return replace(limits, max_model_calls=None) if profile == "equal_time" else limits


def build_input(spec, graph):
    requests, states, metadata = [], {}, {}
    for index, (rid, pair) in enumerate(spec["robots"].items()):
        start, goal = pair
        result = dict(path=plan_route(start, goal, graph)["node_ids"])
        requests.append(dict(order=dict(robot_id=rid, selector="kev", model="kev-08b"),
                             current_node=start, node=goal, result=result))
        states[rid] = dict(status="IDLE", occupied_node=start, occupied_edge=None,
                           navigation_id=None, route=None, goal_node=None,
                           connected=True, fresh=True, active=False)
        metadata[rid] = dict(request_order=index, waiting_seconds=0)
    return build_traffic_situation_input(
        graph=graph, route_requests=requests, robot_states=states, request_metadata=metadata,
        reservations=[], protected_resources={}, wait_allowed_nodes=[pair[0] for pair in spec["robots"].values()],
        snapshot_id=f"strategy-comparison-{spec['id']}", config={},
    )


def compare_one(payload, method, source, limits, *, selector=None):
    if method not in LABELS:
        raise ValueError("알 수 없는 비교 방식입니다.")
    if method != "CODE_SEARCH" and selector is None:
        raise ValueError("모델 방식에는 실제 선택기 또는 명시적인 시험 대역이 필요합니다.")
    original_hash = fingerprint(payload)
    scene = PlanningScene(payload)
    budget = PlanningBudget(limits)
    if method == "JHLEE_STRATEGY":
        plan, report = run_jhlee(scene, budget, source, selector)
    else:
        plan, report = search_strategies(scene, budget, source.Graph, source.conflicts,
                                       selector=selector if method != "CODE_SEARCH" else None,
                                       probability_order=method == "KEV_PROBABILITY_ORDER")
    estimate, execution = None, None
    if plan is not None:
        estimate = estimate_plan(plan, scene.requests, scene.planner.nodes)
        scene.plans = {plan.id: plan}
        scene.estimates = {plan.id: estimate}
        # 실제 실행 직전 검사는 탐색 한도 밖의 공통 실행 검사로 별도 수행한다.
        decision = TrafficDecision(scene.snapshot_hash, plan.id, method, {})
        execution = JointActionSimulation(scene).execute(decision)
    if fingerprint(payload) != original_hash:
        raise RuntimeError("비교 방식이 공통 입력을 변경했습니다.")
    return dict(
        method=method, label=LABELS[method], input_sha256=original_hash,
        model_source="NONE" if method == "CODE_SEARCH" else (
            "KEV" if getattr(selector, "name", None) == "kev" else "TEST_DOUBLE"),
        objective="MIN_FOUND_MEAN_MOVEMENT_COMPLETION_WITHIN_BUDGET",
        scope="STOPPED_FLEET_SERIAL_PLAN_NO_EXISTING_RESERVATIONS_NO_YIELD",
        initial_route_generator="COMMON_ASTAR_OUTSIDE_STRATEGY_BUDGET",
        reroute_generator="COMMON_ASTAR_INSIDE_STRATEGY_BUDGET",
        live_fms_commands_sent=False, real_robot_commands_sent=False,
        search=report, selected_plan=None if plan is None else plan.to_dict(),
        estimated_metrics=estimate, execution=execution,
        solved=execution is not None and execution["status"] == "COMPLETED",
        planning_plus_estimated_mean_seconds=None if estimate is None else
            report["planning_seconds"] + estimate["mean_goal_completion_seconds"],
    )


def summary(report):
    lines = ["# 교통 전략 비교", "", report["status"], "",
             "비교 구성: " + report.get("comparison", "three_way"), "",
             PROFILES.get(report.get("profile"), "최초 동일 호출 상한 비교"), "",
             "기존 관제·실기체 명령 없음. 가상 이동 시간과 실제 판단 시간을 구분한다.",
             "KEV 경로 생성은 사용하지 않으며, 세 방식 모두 공통 A*를 사용한다.", "",
             "| 상황 | 방식 | 가상 실행 | 중단·종료 이유 | 검사 | 모델 호출 | 판단 시간 | 예상 평균 도착 |",
             "| --- | --- | --- | --- | ---: | ---: | ---: | ---: |"]
    for row in report["runs"]:
        search, estimate = row["search"], row["estimated_metrics"]
        average = "미해결" if estimate is None else f"{estimate['mean_goal_completion_seconds']:.3f}초"
        lines.append(f"| {row['title']} | {row['label']} | {'완료' if row['solved'] else '출발 안 함'} | "
                     f"{search['stop_reason']} | {search['checks']} | {search['model_calls']} | "
                     f"{search['planning_seconds']:.3f}초 | {average} |")
    lines.extend(["", "완료하지 못한 상황을 평균 시간 0초로 처리하지 않는다.",
                  "예상 평균은 이동 요청이 있는 로봇만 포함하며 정차 로봇은 제외한다.",
                  "모델 성능은 실제 KEV 결과에서만 판단한다. 준비 검사·대체 응답은 성능 증거가 아니다.",
                  "1회씩의 사전 시험이며 일반 주문 처리량·동시 주행·통계적 우열을 입증하지 않는다."])
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="세 방식의 실제 모델 비교. 기본은 코드 준비 검사")
    parser.add_argument("--catalog", type=Path, default=CATALOG)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--host", default="http://127.0.0.1:8011")
    parser.add_argument("--profile", choices=tuple(PROFILES), default="equal_calls",
                        help="equal_time은 총 모델 호출 수만 제한하지 않고 공통 시간·검사·우회 한도를 유지")
    parser.add_argument("--comparison", choices=tuple(COMPARISONS), default="three_way",
                        help="repeat_vs_rank는 코드·매번 질문·같은 상태의 확률 순서 재사용을 비교")
    args = parser.parse_args(argv)
    catalog = json.loads(args.catalog.read_text(encoding="utf-8"))
    if catalog.get("schema_version") != "traffic-strategy-comparison/0.1":
        parser.error("비교 상황 파일의 버전이 다릅니다.")
    args.output.mkdir(parents=True, exist_ok=False)
    source = load_jhlee(source_output=args.output / "source")
    graph = json.loads((Path(BASE_DIR) / catalog["map"]).read_text(encoding="utf-8"))
    limits = profile_limits(catalog, args.profile)
    inputs = {spec["id"]: build_input(spec, graph) for spec in catalog["cases"]}
    report = dict(
        status="RUNNING", created_at=datetime.now(ZoneInfo("Asia/Seoul")).isoformat(),
        profile=args.profile, effective_limits=asdict(limits),
        comparison=args.comparison, methods=list(COMPARISONS[args.comparison]),
        scope="ISOLATED_SIMULATION", catalog=catalog, inputs=inputs, source=source.provenance,
        local_source_sha256={str(path.relative_to(BASE_DIR)): hashlib.sha256(path.read_bytes()).hexdigest()
                             for path in (Path(__file__), Path(__file__).with_name("traffic_strategy_search.py"),
                                          Path(__file__).with_name("traffic_strategy_jhlee.py"),
                                          Path(BASE_DIR) / "backend/app/services/route_service.py",
                                          Path(BASE_DIR) / "backend/app/services/traffic_joint_actions.py",
                                          Path(BASE_DIR) / "backend/app/services/traffic_manager.py")},
        model_runtime=None, warmup=None, runs=[], live_fms_commands_sent=False,
        real_robot_commands_sent=False, actual_model_comparison=args.run,
    )
    # 실행 당시 파일을 함께 보관해 이후 코드 수정과 시험 결과를 구분한다.
    for relative, digest in report["local_source_sha256"].items():
        raw = (Path(BASE_DIR) / relative).read_bytes()
        if hashlib.sha256(raw).hexdigest() != digest:
            raise RuntimeError("시험 준비 중 소스가 변경됐습니다: " + relative)
        target = args.output / "local_source" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)

    def save():
        (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        (args.output / "summary.md").write_text(summary(report), encoding="utf-8")

    save()
    if args.run:
        from backend.app.services.route_selector.kev_selector import KevSelector

        try:
            selector = KevSelector(model="kev-08b", host=args.host, timeout_seconds=limits.request_seconds)
            report["model_runtime"] = selector._ensure_runtime()
            # 한 번의 공통 준비 요청을 개별 방식의 탐색 비용과 구분한다.
            warmup_state = dict(purpose="COMMON_WARMUP_NOT_A_TRAFFIC_TEST")
            warmup_criteria = dict(READY="The endpoint is ready.", NOT_READY="The endpoint is not ready.")
            warmup_started = monotonic()
            selector.timeout_seconds = 20.0
            try:
                result = selector.select_choice(warmup_state, warmup_criteria,
                                                "Select READY for this warmup request.", question_id="strategy_warmup")
            finally:
                selector.timeout_seconds = limits.request_seconds
            report["warmup"] = dict(state=warmup_state, criteria=warmup_criteria, response=result,
                                    elapsed_seconds=monotonic() - warmup_started,
                                    inference=deepcopy(selector.last_inference))
        except Exception as exc:
            report.update(status="MODEL_UNAVAILABLE_NO_COMPARISON", model_error=f"{type(exc).__name__}: {exc}")
            save()
            print("KEV 서버에 연결하지 못해 모델 비교를 실행하지 않았습니다.", flush=True)
            return 2
    else:
        selector = None
    for index, spec in enumerate(catalog["cases"]):
        chosen_methods = COMPARISONS[args.comparison]
        methods = chosen_methods[index % len(chosen_methods):] + chosen_methods[:index % len(chosen_methods)] if args.run else ("CODE_SEARCH",)
        for method in methods:
            started = monotonic()
            try:
                row = compare_one(inputs[spec["id"]], method, source, limits, selector=selector)
            except Exception as exc:
                report.update(status="COMPARISON_TOOL_ERROR", failed_case=spec["id"], failed_method=method,
                              tool_error=f"{type(exc).__name__}: {exc}")
                save()
                raise
            row.update(case=spec["id"], title=spec["title"], expected=spec["expected"],
                       isolated_run_wall_seconds=monotonic() - started)
            report["runs"].append(row)
            save()
            print(spec["title"], LABELS[method], "완료" if row["solved"] else "출발 안 함", flush=True)
    if args.run:
        report["status"] = "ONE_PASS_REAL_MODEL_COMPARISON_COMPLETED_NOT_THROUGHPUT_EVIDENCE"
    else:
        passed = all(row["solved"] == (row["expected"] == "SOLVABLE") for row in report["runs"])
        report["status"] = "CODE_PREPARATION_PASSED_AWAITING_REAL_KEV" if passed else "CODE_PREPARATION_FAILED"
    save()
    print("결과:", args.output.resolve(), flush=True)
    return int(report["status"] == "CODE_PREPARATION_FAILED")


if __name__ == "__main__":
    raise SystemExit(main())
