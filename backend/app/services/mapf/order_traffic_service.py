"""일반 시뮬 주문의 새 이동 요청을 기존 예약과 함께 조정한다.

현재 범위: 요청 로봇은 노드에서 정지, 다른 로봇의 주행은 유지. 기존 경로와
동적으로 생성한 우회 경로를 검사하고 선택한다. 이동 중 로봇 강제 취소·양보는 하지 않는다.
"""

import asyncio
from copy import deepcopy
import json
import logging
import math
import os
from threading import RLock
from time import monotonic
from uuid import uuid4

from ...config import BASE_DIR
from ..occupancy import occupied_resource
from ..reservation import edge_key
from ..route_graph import load_route_graph
from .common import fingerprint
from .traffic_route_provider import generate_traffic_route

logger = logging.getLogger(__name__)
LOG_DIR = BASE_DIR / "logs" / "order-traffic"
_selection_log_lock = RLock()
SELECTION_POLICIES = ("KEV", "MIN_EXPECTED_TIME", "KEV_STRATEGY_ORDER")
_simulation_policy_override = None


def _save_selection_record(event):
    """진단 기록 실패가 기존 선택·주행 결과를 바꾸지 않게 한다."""
    try:
        encoded = json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n"
        with _selection_log_lock:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            with (LOG_DIR / "selection-inferences.jsonl").open("a", encoding="utf-8") as output:
                output.write(encoded)
    except (OSError, ValueError, TypeError):
        logger.exception("KEV 요청·응답 진단 기록 실패")


def _select_choice_recorded(selector, state, candidates, instructions, metadata):
    """선택 실행과 기록만 담당. 이 함수는 관제 상태·예약을 변경하지 않는다.

    asyncio 대기가 취소돼도 이미 시작한 모델 호출은 끝날 수 있다. RESPONSE 기록은
    모델 응답을 뜻하며 실제 실행 승인이 아니다. 실행은 호출자의 최신 상태 검사를 따른다.
    """
    identity = dict(request_id=uuid4().hex, **deepcopy(metadata), fms_execution_approved=False)
    planned = dict(model=getattr(selector, "api_model", None), state=deepcopy(state),
                   questions={"order_traffic": dict(type="choice", instructions=instructions,
                                                     criteria=deepcopy(candidates))})
    _save_selection_record(dict(**identity, event="REQUEST", at_monotonic=monotonic(), request=planned))
    previous = getattr(selector, "last_inference", None)
    started = monotonic()
    try:
        result = selector.select_choice(state, candidates, instructions, question_id="order_traffic")
    except Exception as exc:
        _save_selection_record(dict(**identity, event="ERROR", at_monotonic=monotonic(),
                                    elapsed_seconds=monotonic()-started, error=f"{type(exc).__name__}: {exc}"))
        raise
    inference = getattr(selector, "last_inference", None)
    actual = deepcopy(inference) if inference is not previous else None
    _save_selection_record(dict(**identity, event="RESPONSE", at_monotonic=monotonic(),
                                elapsed_seconds=monotonic()-started, result=deepcopy(result),
                                inference=actual, exact_transport_request_saved=actual is not None))
    return result


def enabled(order):
    return (os.getenv("FMS_ORDER_TRAFFIC_ENABLED", "0") == "1"
            and order["mode"] == "simulation"
            and order.get("operation_mode", "ai" if order["driving_mode"] == "llm" else "algorithm") == "ai")


def route_source():
    source = os.getenv("FMS_ORDER_ROUTE_SOURCE", "MODE")
    if source not in ("MODE", "EXISTING_SHORTEST_PATH", "KEV"):
        raise ValueError("FMS_ORDER_ROUTE_SOURCE은 MODE, EXISTING_SHORTEST_PATH, KEV 중 하나여야 합니다.")
    return source


def selection_policy():
    """비교 시험도 같은 후보·예약 검사를 사용한다. 기본 선택은 KEV다."""
    policy = _simulation_policy_override or os.getenv("FMS_TRAFFIC_SELECTION_POLICY", "KEV")
    if policy not in SELECTION_POLICIES:
        raise ValueError("FMS_TRAFFIC_SELECTION_POLICY 설정을 확인하세요: " + ", ".join(SELECTION_POLICIES))
    return policy


def set_simulation_policy(policy):
    """호출자가 시뮬·AI/진행 작업 없음을 검사한 뒤 적용하는 프로세스 내 설정."""
    if policy not in SELECTION_POLICIES:
        raise ValueError("지원하지 않는 교통조정 방식입니다.")
    global _simulation_policy_override
    _simulation_policy_override = policy


def _minimum_time_choice(candidates):
    return min(candidates, key=lambda key: (
        candidates[key]["estimated_metrics"]["new_request_completion_seconds"],
        candidates[key]["estimated_metrics"]["total_distance_m"], key,
    ))


def _seconds(name, default):
    value = float(os.getenv(name, str(default)))
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name}은 유한한 양수여야 합니다.")
    return value


def _capture(store, graph):
    with store._lock:
        traffic = store._traffic._planning_copy()
    states = traffic._robots
    # 예약 원본은 사본에 유지하고 모델에는 후보별 예약 대기 예상값을 전달한다.
    now = monotonic()
    context = {
        "robots": [{"robot_id": rid, "status": row["status"],
                    "occupied_resource": list(occupied_resource(row, graph)),
                    "goal_node": row["goal_node"],
                    "remaining_path": (row.get("route") or {}).get("node_ids", [])[
                        (row.get("route") or {}).get("segment_index", 0):],
                    "existing_plan_fixed": True}
                   for rid, row in sorted(states.items())],
        "reservation_count": len(traffic._reservations.snapshot()),
        "protected_resources": {rid: [list(r) for r in sorted(resources)]
                                for rid, resources in traffic._protected_resources.items()},
    }
    return traffic, context, now


def _avoidance_requests(path, traffic, rid, limit, graph):
    """공유 자원을 피하는 요청만 구성한다. 실제 경로 계산은 기존 생성기가 맡는다."""
    shared_nodes, shared_edges = set(), set()
    own_edges = {edge_key(a, b) for a, b in zip(path, path[1:])}
    for other_id, row in traffic._robots.items():
        if other_id == rid:
            continue
        if row["occupied_node"] is not None:
            shared_nodes.add(row["occupied_node"])
        if row["occupied_edge"] is not None:
            # 물리 엣지는 지도에 기록된 방향별 ID에서 해석한다.
            resource = occupied_resource(row, graph)
            shared_edges.add(resource)
        route = row.get("route") or {}
        remaining = route.get("node_ids", [])[route.get("segment_index", 0):]
        shared_nodes.update(remaining)
        shared_edges.update(edge_key(a, b) for a, b in zip(remaining, remaining[1:]))
    shared_nodes.update(r[1] for resources in traffic._protected_resources.values()
                        for r in resources if r[0] == "node")
    shared_edges.update(r for resources in traffic._protected_resources.values()
                        for r in resources if r[0] == "edge")
    nodes = [n for n in path[1:-1] if n in shared_nodes]
    edges = [(a, b) for a, b in zip(path, path[1:]) if edge_key(a, b) in shared_edges & own_edges]
    requests = []
    if nodes:
        requests.append(dict(blocked_nodes=tuple(dict.fromkeys(nodes)), blocked_edges=()))
    for pair in edges:
        request = dict(blocked_nodes=(), blocked_edges=(pair,))
        if request not in requests:
            requests.append(request)
    return requests[:limit]


def _estimate(traffic, rid, path, graph, now):
    route = traffic.preview_new_route(rid, path, now=now, graph=graph)
    if route is None:
        return None
    from ..pathfinding import DistanceAStar

    points = DistanceAStar(graph).nodes
    length = sum(math.dist(points[a], points[b]) for a, b in zip(path, path[1:]))
    finish = route["segment_departures"][-1] + math.dist(points[path[-2]], points[path[-1]]) / traffic.speed_mps
    return dict(new_request_completion_seconds=finish - now, total_distance_m=length,
                reservation_wait_seconds=max(0.0, finish - now - length / traffic.speed_mps))


def _record(order, event):
    event = {"order_id": order.get("order_id"), "robot_id": order["robot_id"],
             "phase": order.get("phase"), **event}
    order["traffic_coordination"] = event
    history = order.setdefault("traffic_coordination_history", [])
    history.append(event)
    del history[:-12]
    logger.info("ORDER_TRAFFIC %s", json.dumps(event, ensure_ascii=False, allow_nan=False))
    return event


def _save(event):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    # 외부 입력을 파일명으로 사용하지 않는다.
    with (LOG_DIR / "decisions.jsonl").open("a", encoding="utf-8") as output:
        output.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n")


async def coordinate_order_route(order, goal, start, path, *, graph, source, valid=None,
                                 store=None, selector=None):
    from ..mock_data import mock_fms
    from ..mode_service import mode_manager
    from ..route_selector.kev_selector import KevSelector

    store = mock_fms if store is None else store
    rid, start, goal = order["robot_id"], str(start), str(goal)
    path = tuple(map(str, path))
    if path[0] != start or path[-1] != goal:
        raise ValueError("교통조정에 전달된 경로의 출발·목적지가 다릅니다.")
    policy = selection_policy()
    coordination_started = monotonic()
    deadline = monotonic() + _seconds("FMS_TRAFFIC_WAIT_SECONDS", 60)
    selection_timeout = _seconds("FMS_TRAFFIC_SELECTION_SECONDS", 10)
    limit = int(os.getenv("FMS_TRAFFIC_REROUTE_LIMIT", "2"))
    if not 0 <= limit <= 8:
        raise ValueError("우회 요청 한도는 0~8이어야 합니다.")
    cache, rejected, attempts, calls = {}, {}, 0, 0
    with store._lock:
        initial_navigation_id = store._robots[rid]["navigation_id"]

    def guard():
        if (mode_manager.mode != "simulation" or mode_manager.operation_mode != "ai"
                or load_route_graph() != graph or (valid and not valid())):
            raise ValueError("교통조정 중 주문 단계·지도·운용 모드가 변경되었습니다.")
        with store._lock:
            row = store._robots[rid]
            if (row["status"] != "IDLE" or row["occupied_node"] != start
                    or row["occupied_edge"] is not None or row["route"] is not None
                    or row["goal_node"] is not None or row["connection_state"] != "ONLINE"
                    or row["navigation_id"] != initial_navigation_id):
                raise ValueError("교통조정 중 출발 로봇 상태가 변경되었습니다.")

    if policy == "KEV_STRATEGY_ORDER" and start != goal:
        from .order_traffic_strategy import coordinate_strategy_route

        return await coordinate_strategy_route(
            order, goal, start, path, graph=graph, source=source, store=store, selector=selector,
            guard=guard, deadline=deadline, selection_timeout=selection_timeout, limit=limit,
            coordination_started=coordination_started,
        )

    try:
        while monotonic() < deadline:
            guard()
            traffic, context, now = _capture(store, graph)
            if start == goal:
                with store._lock:
                    guard()
                    if store._traffic.commit_new_route(rid, path, now=monotonic(), graph=graph):
                        return dict(success=True, status="SUCCESS", route=None)
            paths = {"KEEP_PATH": path}
            for index, constraints in enumerate(_avoidance_requests(path, traffic, rid, limit, graph), 1):
                key = fingerprint(constraints)
                if key not in cache:
                    if len(cache) >= limit:
                        continue
                    try:
                        result = await asyncio.wait_for(asyncio.to_thread(
                            generate_traffic_route, graph, start, goal, source=source,
                            selector=order.get("selector") or "kev", model=order.get("model") or "kev-08b",
                            **constraints,
                        ), timeout=min(selection_timeout, max(.001, deadline - monotonic())))
                        guard()
                        cache[key] = result
                    except (ValueError, RuntimeError, TimeoutError) as exc:
                        cache[key] = {"error": str(exc), "constraints": constraints}
                result = cache[key]
                if "error" in result:
                    rejected[f"REROUTE_{index}"] = result
                    continue
                alternate = tuple(map(str, result["path"]))
                if alternate not in paths.values():
                    paths[f"REROUTE_{index}"] = alternate
            guard()
            # 경로 생성 중 움직인 다른 로봇까지 최신 상태로 다시 검사한다.
            traffic, context, now = _capture(store, graph)
            candidates = {}
            for key, candidate in paths.items():
                estimate = _estimate(traffic, rid, candidate, graph, now)
                if estimate is not None:
                    candidates[key] = dict(path=list(candidate), action="PROCEED" if key == "KEEP_PATH" else "REROUTE",
                                           estimated_metrics=estimate)
            if not candidates:
                if attempts == 0:
                    _record(order, dict(state="WAITING_FOR_SAFE_ROUTE", goal_node=goal, candidates={},
                                        note="준비된 경로를 예약할 수 없어 현재 노드에서 재검사를 기다립니다."))
                attempts += 1
                await asyncio.sleep(.25)
                continue
            result, selected_by = {}, "SINGLE_OPTION"
            selection_started = monotonic()
            if len(candidates) == 1:
                choice = next(iter(candidates))
            elif policy == "MIN_EXPECTED_TIME":
                choice = _minimum_time_choice(candidates)
                selected_by = "MIN_EXPECTED_TIME"
            else:
                selector = selector or KevSelector(model=os.getenv("FMS_TRAFFIC_MODEL", "kev-08b"),
                                                   timeout_seconds=selection_timeout)
                state = dict(schema_version="order-traffic/0.1", new_request=dict(robot_id=rid,
                             current_node=start, goal_node=goal), **context,
                             objective="MIN_NEW_REQUEST_COMPLETION_WITH_EXISTING_PLANS_FIXED")
                _record(order, dict(state="SELECTING", goal_node=goal, candidates=candidates))
                calls += 1
                result = await asyncio.wait_for(asyncio.to_thread(
                    _select_choice_recorded, selector, state,
                    {key: json.dumps(value, ensure_ascii=False) for key, value in candidates.items()},
                    "Choose the candidate with the smallest estimated_metrics.new_request_completion_seconds. "
                    "Other robots keep their existing plans and reservations. Estimates include reservation waits "
                    "but not inference or turning. Do not change another robot's plan. Tie: smaller total_distance_m.",
                    dict(order_id=order.get("order_id"), robot_id=rid, phase=order.get("phase"),
                         goal_node=goal, call=calls),
                ), timeout=min(selection_timeout, max(.001, deadline - monotonic())))
                choice = result.get("choice")
                selected_by = "KEV" if getattr(selector, "name", None) == "kev" else "TEST_DOUBLE"
            selection_seconds = monotonic() - selection_started
            if choice not in candidates:
                raise ValueError(f"등록되지 않은 교통조정안: {choice}")
            guard()
            with store._lock:
                guard()
                # 추론 시작 시점의 예약을 복사해 덮지 않는다. 최신 예약표에 새 요청만 추가한다.
                committed = store._traffic.commit_new_route(rid, candidates[choice]["path"],
                                                           now=monotonic(), graph=graph)
                route = deepcopy(store._robots[rid]["route"])
            if not committed:
                attempts += 1
                await asyncio.sleep(.1)
                continue
            event = _record(order, dict(state="APPLIED", goal_node=goal, choice=choice,
                            selected_by=selected_by, kev_calls=calls, path=candidates[choice]["path"],
                            selection_policy=policy, selection_seconds=selection_seconds,
                            coordination_seconds=monotonic() - coordination_started,
                            route_source=source, candidates=candidates, rejected_reroutes=rejected,
                            reroute_generation=list(cache.values()), existing_plans_preserved=True,
                            revalidated_before_apply=True, retries=attempts,
                            note="새 이동 요청 하나의 경로 조정. 다른 로봇의 통과 순서를 변경하지 않습니다."))
            try:
                await asyncio.to_thread(_save, event)
            except OSError:
                logger.exception("교통조정은 적용했지만 파일 기록에 실패했습니다.")
            return dict(success=True, status="WAITING" if route and route["departure_at"] > monotonic() else "SUCCESS",
                        route=route, traffic_coordination=event)
        raise ValueError("교통조정 대기 한도를 초과했습니다. 준비된 경로로 예약할 수 없습니다.")
    except asyncio.CancelledError:
        _record(order, dict(state="CANCELLED", goal_node=goal, kev_calls=calls))
        raise
    except TimeoutError as exc:
        _record(order, dict(state="FAILED", goal_node=goal, kev_calls=calls, error="KEV 선택 응답 시간 초과"))
        raise ValueError("KEV 교통조정 응답 시간 한도를 초과했습니다.") from exc
    except Exception as exc:
        _record(order, dict(state="FAILED", goal_node=goal, kev_calls=calls, error=str(exc)))
        raise
