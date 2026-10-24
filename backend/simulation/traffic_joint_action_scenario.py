"""기존 시나리오 실행 버튼용 교통조정 테스트. simulation 모드에서만 호출한다."""

import asyncio
from copy import deepcopy
import json
import math
from pathlib import Path
from time import monotonic

from backend.app.services.traffic_joint_actions import TrafficSituation
from backend.app.services.traffic_situation_adapter import build_traffic_situation_input
from backend.app.services.traffic_metrics import TrafficRunMetrics


GENERATED_ROUTE_SOURCES = ("EXISTING_SHORTEST_PATH", "KEV")


def traffic_selector(options):
    selector = options.get("traffic_selector", "kev")
    if selector not in ("kev", "fixed", "search", "guided"):
        raise ValueError("교통조정 선택기는 kev, fixed, search 또는 guided여야 합니다.")
    if (selector == "fixed") != (options.get("fixed_plan") is not None):
        raise ValueError("고정 계획과 fixed 선택기를 함께 지정하세요.")
    if (selector in ("search", "guided")) != (options.get("recovery_search") is not None):
        raise ValueError("자동 탐색 한도와 search/guided 선택기를 함께 지정하세요.")
    strategy = options.get("recovery_strategy", "BREADTH_FIRST")
    if (selector == "guided") != (strategy in ("KEV_GUIDED", "KEV_DEPENDENCY")):
        raise ValueError("KEV 전략 제안은 guided 선택기와 KEV 탐색 방식을 함께 지정하세요.")
    return selector


def route_source_choices(options):
    """경로를 생성하는 시험만 출처를 선택한다. 공급 경로 fixture는 고정한다."""
    return list(GENERATED_ROUTE_SOURCES) if options.get("route_source") in (
        *GENERATED_ROUTE_SOURCES, "OPERATION_MODE",
    ) else []


def resolve_route_source(options, operation_mode):
    source = options.get("route_source", "SUPPLIED_PATHS")
    if source == "OPERATION_MODE":  # 이전 시험 설정과의 호환성
        source = "KEV" if operation_mode == "ai" else "EXISTING_SHORTEST_PATH"
    if source not in (*GENERATED_ROUTE_SOURCES, "SUPPLIED_PATHS"):
        raise ValueError(f"알 수 없는 테스트 경로 출처: {source}")
    if traffic_selector(options) == "fixed" and source != "SUPPLIED_PATHS":
        raise ValueError("고정 계획 시험은 시험용 고정 경로만 사용합니다.")
    return source


async def run_joint_action_scenario(
    manager, scenario, graph, started, timeout, *, selector=None, poll_interval=0.1
):
    """실패·중단도 미완료 기록을 남긴다. 실제 로봇/일반 주문에는 연결하지 않는다."""
    from backend.app.config import BASE_DIR

    context = {}
    error = None
    try:
        await _run_joint_action_scenario(
            manager, scenario, graph, started, timeout, context=context,
            selector=selector, poll_interval=poll_interval,
        )
    except (Exception, asyncio.CancelledError) as exc:
        error = str(exc) or "시험 중단"
        raise
    finally:
        if "recorder" in context:
            manager._status["coordination"]["actual_metrics"] = context["recorder"].report(
                monotonic(), outcome="INTERRUPTED" if error else context["outcome"],
            )
        report = manager.status()
        if "payload" in context:
            report["traffic_input"] = context["payload"]
            report["traffic_preview"] = context["preview"]
        if error:
            report.update(state="failed", message=error)
        output = Path(BASE_DIR) / "logs" / "traffic-joint-actions"
        output.mkdir(parents=True, exist_ok=True)
        run_id = "".join(c for c in str(report.get("run_id", "unknown")) if c.isalnum() or c in "-_")
        for filename in ("ui-last-result.json", f"run-{run_id}.json"):
            (output / filename).write_text(
                json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8"
            )


async def _run_joint_action_scenario(
    manager, scenario, graph, started, timeout, *, context, selector, poll_interval
):
    # 기존 관제의 Mock singleton을 사용하되 ROS 실행 경로에서는 로드하지 않는다.
    from backend.app.config import BASE_DIR
    from backend.app.services.mock_data import mock_fms
    from backend.app.services.mode_service import mode_manager
    from backend.app.services.occupancy import occupied_resource
    from backend.app.services.route_graph import load_route_graph
    from backend.app.services.scenario_service import robot_states

    options = scenario["traffic_joint_action"]
    operation_mode = mode_manager.operation_mode
    limit = float(options.get("timeout_seconds", timeout))
    if not math.isfinite(limit) or limit <= 0:
        raise ValueError("교통조정 테스트 제한 시간이 올바르지 않습니다.")
    output = Path(BASE_DIR) / "logs" / "traffic-joint-actions"
    output.mkdir(parents=True, exist_ok=True)
    requested_at = monotonic()

    def guard():
        if "recorder" in context:
            manager._status["coordination"]["actual_metrics"] = context["recorder"].report(monotonic())
        if manager._status["state"] != "running":
            raise ValueError(manager._status["message"] or "시나리오 중단")
        if (
            mode_manager.mode != "simulation" or mode_manager.operation_mode != operation_mode
            or load_route_graph() != graph
        ):
            raise ValueError("실행 중 운용 모드 또는 그래프가 변경되었습니다.")
        if monotonic() - started >= limit:
            raise ValueError(f"{limit:g}초 내 미완료 (교착 확정 아님)")
        states = robot_states("simulation")
        rows = []
        for spec in scenario["robots"]:
            rid = spec["id"]
            row = states.get(rid)
            if row is None or not row["connected"] or not row["fresh"]:
                raise ValueError(f"{rid}: 연결 또는 최신 위치 확인 실패")
            if row["status"] in ("PAUSED", "EMERGENCY_STOP", "OFFLINE"):
                raise ValueError(f"{rid}: {row['status']}")
            rows.append(dict(
                robot_id=rid, status=row["status"],
                current_node=row["current_node"], goal=spec["goal"],
            ))
        manager._status.update(robots=rows, elapsed=round(monotonic() - started, 2))
        return states

    guard()
    with mock_fms._lock:
        before = deepcopy(mock_fms._robots)
        captured_states = robot_states("simulation")
        reservations = mock_fms._traffic._reservations.snapshot()
        protected = deepcopy(mock_fms._traffic._protected_resources)
        if set(before) != {spec["id"] for spec in scenario["robots"]}:
            raise ValueError("테스트 입력은 전체 가상 로봇을 포함해야 합니다.")
    source = resolve_route_source(options, operation_mode)
    requests, metadata, route_results = [], {}, {}
    for index, spec in enumerate(scenario["robots"], 1):
        if spec["goal"] is None:
            continue  # 요청 없는 로봇의 정적 점유는 어댑터가 전체 FMS 상태에서 포함한다.
        rid = spec["id"]
        current, goal = before[rid]["occupied_node"], spec["goal"]
        if source == "KEV":
            from backend.app.services.llm_route_preview import preview_llm_route

            manager._status["message"] = f"KEV 경로 생성 중: {rid} N{current} → N{goal}"
            result = await asyncio.to_thread(
                preview_llm_route, graph, current, goal,
                selector="kev", model=options.get("model", "kev-08b"),
            )
            guard()
            path = result["path"]
            route_results[rid] = result
        elif source == "EXISTING_SHORTEST_PATH":
            from backend.app.services.route_service import plan_route

            path = plan_route(current, goal, graph)["node_ids"]
        else:
            path = options["paths"][rid]
        requests.append(dict(
            order={"robot_id": rid, "selector": "kev", "model": options.get("model", "kev-08b")},
            current_node=current, node=goal, result={"path": path},
        ))
        # 시나리오 요청을 동시에 만든 실험값이다. 실제 주문의 요청 시각을 뜻하지 않는다.
        metadata[rid] = dict(request_order=index, waiting_seconds=0)
    payload = build_traffic_situation_input(
        graph=graph, snapshot_id=manager._status["run_id"], mode=mode_manager.mode,
        route_requests=requests, robot_states=captured_states,
        reservations=reservations, protected_resources=protected,
        request_metadata=metadata, model=options.get("model", "kev-08b"),
        wait_allowed_nodes=options["wait_allowed_nodes"],
        config=dict(
            blocked_nodes=options.get("blocked_nodes", []),
            blocked_edges=options.get("blocked_edges", []),
            reroute_paths=options.get("reroute_paths", {}),
            yield_paths=options.get("yield_paths", {}),
            **({"fixed_plan": options["fixed_plan"]} if "fixed_plan" in options else {}),
            **({"recovery_search": options["recovery_search"]} if "recovery_search" in options else {}),
            recovery_strategy=options.get("recovery_strategy", "BREADTH_FIRST"),
            **({"recovery_motion_estimates": options["recovery_motion_estimates"]}
               if "recovery_motion_estimates" in options else {}),
            **({"recovery_guidance": options["recovery_guidance"]} if "recovery_guidance" in options else {}),
            recovery_branch_policy=options.get("recovery_branch_policy", "DISCARD"),
            recovery_guidance_budget_policy=options.get("recovery_guidance_budget_policy", "STOP"),
        ),
    )
    with mock_fms._lock:
        if before != mock_fms._robots or mock_fms._traffic._reservations.snapshot() or mock_fms._traffic._protected_resources:
            raise ValueError("경로 생성 중 가상 로봇 또는 예약이 변경되었습니다.")
    selection_method = traffic_selector(options)
    payload.update(route_source=source, traffic_selector=selection_method, operation_mode=operation_mode, request_metadata_source="SCENARIO_ORDER_ZERO_WAIT")
    if selection_method in ("search", "guided"):
        if selection_method == "guided" and selector is None:
            from backend.app.services.route_selector.kev_selector import KevSelector
            from backend.app.services.traffic_recovery_guidance import GuidanceLimits

            guidance_limits = GuidanceLimits.parse(options.get("recovery_guidance", {}))
            selector = KevSelector(model=options.get("model", "kev-08b"), timeout_seconds=guidance_limits.request_seconds)
        manager._status["message"] = (
            "KEV가 다음 검사 동작을 제안하고 코드가 검사하는 중" if selection_method == "guided"
            else "코드가 양보·통과·복귀 순서를 찾는 중 · KEV 호출 없음"
        )
        scene = await asyncio.to_thread(
            TrafficSituation, payload, recovery_selector=selector,
            recovery_cancelled=lambda: manager._status["state"] != "running" or mode_manager.mode != "simulation" or mode_manager.operation_mode != operation_mode,
        )
        guard()
    else:
        scene = TrafficSituation(payload)
    context.update(payload=payload, preview=scene.preview())
    for name, value in [("ui-last-input", payload), ("ui-last-preview", scene.preview())]:
        (output / f"{name}.json").write_text(
            json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    if selector is None and len(scene.plans) > 1:
        from backend.app.services.route_selector.kev_selector import KevSelector

        selector = KevSelector(model=options.get("model", "kev-08b"))
    manager._status["message"] = (
        "KEV 전략 탐색 완료 · 완성 계획 재검사 중" if selection_method == "guided" else
        "자동 탐색 완료 · KEV 호출 없음" if selection_method == "search" else
        "고정 계획 검사 완료 · KEV 호출 없음" if selection_method == "fixed"
        else f"교통조정안 확인 중: {', '.join(scene.plans)}"
    )
    selection_started = monotonic()
    decision = await asyncio.to_thread(scene.decide, selector)
    selection_seconds = monotonic() - selection_started
    if selection_method == "guided":
        selection_seconds = sum(call["elapsed_seconds"] for call in scene.search_report["guidance_calls"])
    guard()
    plan = scene.checked_plan(decision)
    label = options.get("candidate_labels", {}).get(plan.id)
    selection_text = (
        f"KEV 전략 제안 ({scene.search_report['model_calls']}회) + 코드 검사" if selection_method == "guided" else
        "코드 자동 탐색 (KEV 호출 없음)" if selection_method == "search" else
        "고정 계획 (KEV 호출 없음)" if selection_method == "fixed"
        else f"{decision.selection_source}: {plan.id}" + (f" ({label})" if label else "")
    )
    if scene.search_report and scene.search_report.get("code_after_model_budget_decisions", 0):
        selection_text = (f"KEV 추천 {scene.search_report['model_calls']}회 후 코드 보완 "
                          f"{scene.search_report['code_after_model_budget_decisions']}회")
    with mock_fms._lock:
        if (
            before != mock_fms._robots or mock_fms._traffic._reservations.snapshot()
            or mock_fms._traffic._protected_resources
        ):
            raise ValueError("계획 확인 중 가상 로봇 또는 예약이 변경되었습니다.")
    manager._status["coordination"] = dict(
        decision=decision.to_dict(), selected_joint_action=plan.to_dict(), events=[],
        input_source=payload["input_source"], route_source=source, traffic_selector=selection_method,
        joint_action_choice_requests=int(len(scene.plans) > 1),
        recovery_guidance_requests=scene.search_report.get("model_calls", 0) if scene.search_report else 0,
        candidate_ids=list(scene.plans),
        selected_description=label,
        candidate_descriptions=options.get("candidate_labels", {}),
        candidate_estimates=scene.estimates,
        measurement_note="예상은 거리÷속도와 순차 실행 기준. 실제 단계 시간에는 예약 정지·위치 확인 지연이 포함됩니다.",
        route_generation=route_results,
        route_generation_calls=sum(result["calls"] for result in route_results.values()),
        recovery_search=scene.search_report,
    )
    events = manager._status["coordination"]["events"]
    recorder = TrafficRunMetrics(
        scene.requests, scene.planner.nodes, requested_at=requested_at,
        clock_kind="WALL_MONOTONIC",
    )
    recorder.begin_execution(monotonic(), selection_seconds=selection_seconds)
    context.update(recorder=recorder, outcome="RUNNING")
    if not plan.steps:
        progress = any(candidate.steps for candidate in scene.plans.values())
        outcome = "HELD" if progress else "UNRESOLVED"
        context["outcome"] = outcome
        manager._status.update(
            state="completed",
            message=f"{decision.selection_source}: HOLD_ALL · 전원 진입 대기 ({outcome})",
        )
        if selection_method in ("search", "guided"):
            manager._status["message"] = (
                f"{'KEV 전략 탐색' if selection_method == 'guided' else '코드 자동 탐색'} · 이번 한도와 허용 경로에서 해결 순서를 찾지 못함 "
                f"({scene.search_report['status']}) · 이동하지 않고 대기"
            )
    else:
        for index, step in enumerate(plan.steps, 1):
            guard()
            # 다음 단계는 이전 단계 도착 확인 후 시작한다.
            with mock_fms._lock:
                guard()
                current = mock_fms._robots[step.robot_id]
                if (
                    current["status"] != "IDLE" or current["route"] is not None
                    or current["goal_node"] is not None or current["occupied_edge"] is not None
                    or current["occupied_node"] != step.path[0]
                ):
                    raise ValueError("실행 직전 로봇 정지/출발 노드가 변경되었습니다.")
                recorder.start_step(index, step, monotonic())
                events.append(dict(event="STEP_STARTED", robot_id=step.robot_id, action=step.action,
                                   at_seconds=monotonic() - requested_at))
                mock_fms.navigate_to_node(
                    step.robot_id, step.path[-1], required_path=step.path,
                )
            guard()  # 실행 요청 중 들어온 중단 사유를 진행 문구로 덮어쓰지 않는다.
            manager._status["message"] = (
                f"{selection_text} · {index}/{len(plan.steps)} "
                f"{step.robot_id} {step.action} → N{step.path[-1]} · 나머지 진입 대기"
            )
            if selection_method in ("fixed", "search", "guided"):
                robot_label = "R" + step.robot_id.removeprefix("robot")
                action_label = "양보 이동" if step.action == "YIELD" else (
                    "원래 자리로 복귀" if scene.requests[step.robot_id]["current_node"] == scene.requests[step.robot_id]["node"]
                    else "통과"
                )
                manager._status["message"] = (
                    f"{selection_text} · {index}/{len(plan.steps)} · "
                    f"{robot_label} {action_label}: N{step.path[0]} → N{step.path[-1]} · 다른 로봇 대기"
                )
            while True:
                states = guard()
                with mock_fms._lock:
                    resources = [occupied_resource(r, graph) for r in mock_fms._robots.values()]
                    if len(resources) != len(set(resources)):
                        raise ValueError("노드/물리 엣지 동시 점유 위반")
                row = states[step.robot_id]
                if (
                    row["status"] == "IDLE" and not row["active"]
                    and row["route"] is None and row["occupied_node"] == step.path[-1]
                ):
                    break
                await asyncio.sleep(poll_interval)
            recorder.complete_step(step, monotonic())
            events.append(dict(
                event="STEP_COMPLETED", robot_id=step.robot_id, node=step.path[-1],
                at_seconds=monotonic() - requested_at,
            ))
        states = guard()
        if any(states[rid]["occupied_node"] != r["node"] for rid, r in scene.requests.items()):
            raise ValueError("원래 목적지 복원에 실패했습니다.")
        manager._status.update(
            state="completed",
            message=f"{selection_text} · 모든 단계 완료 · 점유 위반 없음",
        )
        context["outcome"] = "COMPLETED"
