"""일반 시뮬 주문용 전략 순서 제안. 다른 로봇의 경로·예약은 수정하지 않는다.

정지 배치의 전체 로봇 DFS를 그대로 호출하지 않는다. 새 요청 하나에 대해
KEEP/회피 전략을 먼저 질문하고 확률 순서로 필요한 경로를 생성·검사한다.
"""

import asyncio
from copy import deepcopy
from dataclasses import asdict
import json
import math
import os
from time import monotonic

from . import order_traffic_service as common
from ..pathfinding import DistanceAStar
from .common import fingerprint
from .traffic_route_provider import generate_traffic_route

INSTRUCTIONS = (
    "Choose ONE next route strategy to TEST for the new stopped robot. "
    "The returned choice probabilities will order the remaining strategies ONCE for this snapshot. "
    "KEEP_PATH uses the supplied base path; REROUTE excludes the specified resources using the existing planner. "
    "Reroute paths have NOT been generated yet and strategies have NOT passed reservation checks. "
    "Other robots keep their existing plans and reservations; parked robots do not move by themselves. "
    "Use the graph, current occupancy, remaining paths and recent failures. Prefer strategies likely to "
    "produce short completion time for the new request. Code generates and checks paths in that order, "
    "then chooses the smallest estimated completion time among feasible paths tested within the budget. "
    "No movement is approved by your answer. Do not invent routes or assume option order is quality."
)


def probability_order(result, candidates):
    probabilities = result.get("probabilities") if isinstance(result, dict) else None
    if not isinstance(result, dict) or not isinstance(result.get("choice"), str) or result["choice"] not in candidates:
        raise ValueError("KEV 전략 응답의 선택지가 올바르지 않습니다.")
    if not isinstance(probabilities, dict) or set(probabilities) != set(candidates):
        raise ValueError("KEV 전략 응답의 후보 확률이 누락되거나 다릅니다.")
    values = list(probabilities.values())
    if (not all(not isinstance(p, bool) and isinstance(p, (int, float)) and math.isfinite(p)
                and 0 <= p <= 1 for p in values) or abs(sum(values) - 1.0) > .01):
        raise ValueError("KEV 전략 응답의 후보 확률 범위가 올바르지 않습니다.")
    return sorted(candidates, key=lambda key: -probabilities[key])


def snapshot_key(traffic, graph):
    reservations = []
    for row in traffic._reservations.snapshot():
        record = asdict(row)
        if math.isinf(record["end"]):
            record["end"] = None
        reservations.append(record)
    return fingerprint(dict(graph=graph, robots=traffic._robots, reservations=reservations,
        protected={rid: sorted(resources) for rid, resources in traffic._protected_resources.items()}))


async def coordinate_strategy_route(order, goal, start, path, *, graph, source, store, selector,
                                    guard, deadline, selection_timeout, limit, coordination_started):
    from ..route_selector.kev_selector import KevSelector

    rid = order["robot_id"]
    planner = DistanceAStar(graph)
    graph_input = dict(nodes={node: list(xy) for node, xy in planner.nodes.items()},
                       directed_edges=[dict(start=a, end=b, distance_m=d)
                                       for a, edges in planner.edges.items() for b, d in edges])
    calls, attempts, batches = 0, 0, []
    recent_checks = []
    route_cache, rejected = {}, {}
    ranked_key, ranked_ids = None, None
    def ensure_current():
        guard()
        if monotonic() >= deadline:
            raise ValueError("교통조정 전체 대기 한도를 초과했습니다.")

    try:
        call_limit = int(os.getenv("FMS_TRAFFIC_STRATEGY_CALL_LIMIT", "8"))
        if not 1 <= call_limit <= 32:
            raise ValueError("전략 제안 호출 상한은 1~32여야 합니다.")
        while monotonic() < deadline:
            ensure_current()
            traffic, context, now = common._capture(store, graph)
            strategies = {"KEEP_PATH": dict(action="KEEP", blocked_nodes=(), blocked_edges=())}
            for index, constraints in enumerate(common._avoidance_requests(path, traffic, rid, limit, graph), 1):
                strategies[f"REROUTE_{index}"] = dict(action="REROUTE", **constraints)
            key = fingerprint(dict(snapshot=snapshot_key(traffic, graph), strategies=strategies))
            cycle_deadline = min(deadline, monotonic() + selection_timeout)
            reused = key == ranked_key and ranked_ids is not None
            cycle_started = monotonic()
            if not reused:
                if len(strategies) == 1:
                    ranked_ids = list(strategies)
                else:
                    if calls >= call_limit:
                        raise ValueError("KEV 전략 제안 호출 상한을 초과했습니다.")
                    selector = selector or KevSelector(model=os.getenv("FMS_TRAFFIC_MODEL", "kev-08b"),
                                                       timeout_seconds=selection_timeout)
                    state = dict(schema_version="order-traffic-strategy/0.1", planning_only=True,
                                 new_request=dict(robot_id=rid, current_node=start, goal_node=goal, base_path=list(path)),
                                 route_graph=graph_input, **context,
                                 objective="MIN_NEW_REQUEST_COMPLETION_WITH_EXISTING_PLANS_FIXED",
                                 recent_failures=list(rejected.values())[-8:], recent_checks=recent_checks[-8:])
                    common._record(order, dict(state="SELECTING", goal_node=goal,
                                              selection_policy="KEV_STRATEGY_ORDER", strategies=strategies))
                    calls += 1
                    result = await asyncio.wait_for(asyncio.to_thread(
                        common._select_choice_recorded, selector, state,
                        {name: json.dumps(spec, ensure_ascii=False) for name, spec in strategies.items()},
                        INSTRUCTIONS, dict(order_id=order.get("order_id"), robot_id=rid, phase=order.get("phase"),
                                           goal_node=goal, call=calls, role="ROUTE_STRATEGY_ORDER"),
                    ), timeout=max(.001, cycle_deadline - monotonic()))
                    ensure_current()
                    ranked_ids = probability_order(result, strategies)
                ranked_key = key
            batch = dict(snapshot_key=key, strategy_order=list(ranked_ids), reused=reused,
                         call=calls if len(strategies) > 1 else None, attempts=[])
            batches.append(batch)
            paths = {}
            for name in ranked_ids:
                ensure_current()
                if monotonic() >= cycle_deadline:
                    break
                spec = strategies[name]
                if name == "KEEP_PATH":
                    candidate_path = path
                else:
                    constraints = {k: spec[k] for k in ("blocked_nodes", "blocked_edges")}
                    route_key = fingerprint(constraints)
                    if route_key not in route_cache:
                        if len(route_cache) >= limit:
                            batch["attempts"].append(dict(strategy=name, result="ROUTE_CALL_LIMIT"))
                            continue
                        try:
                            value = await asyncio.wait_for(asyncio.to_thread(
                                generate_traffic_route, graph, start, goal, source=source,
                                selector=order.get("selector") or "kev", model=order.get("model") or "kev-08b",
                                **constraints,
                            ), timeout=max(.001, cycle_deadline - monotonic()))
                            ensure_current()
                            route_cache[route_key] = value
                        except TimeoutError:
                            batch["attempts"].append(dict(strategy=name, result="ROUTE_TIMEOUT"))
                            break
                        except (ValueError, RuntimeError) as exc:
                            ensure_current()
                            route_cache[route_key] = dict(error=str(exc), constraints=constraints)
                    value = route_cache[route_key]
                    if "error" in value:
                        rejected[route_key] = dict(strategy=name, result="ROUTE_REJECTED", **value)
                        batch["attempts"].append(deepcopy(rejected[route_key]))
                        continue
                    candidate_path = tuple(map(str, value["path"]))
                paths[name] = tuple(candidate_path)
                # 경로 생성 중 움직인 다른 로봇도 최신 사본에서 확인한다.
                latest, _, latest_now = common._capture(store, graph)
                estimate = common._estimate(latest, rid, candidate_path, graph, latest_now)
                batch["attempts"].append(dict(strategy=name, path=list(candidate_path),
                                               result="VALID" if estimate is not None else "NO_FEASIBLE_RESERVATION"))
            ensure_current()
            traffic, _, now = common._capture(store, graph)
            candidates = {}
            for name, candidate_path in paths.items():
                estimate = common._estimate(traffic, rid, candidate_path, graph, now)
                if estimate is not None:
                    candidates[name] = dict(path=list(candidate_path), action="PROCEED" if name == "KEEP_PATH" else "REROUTE",
                                            estimated_metrics=estimate)
            if candidates:
                choice = common._minimum_time_choice(candidates)
                ensure_current()
                with store._lock:
                    ensure_current()
                    committed = store._traffic.commit_new_route(rid, candidates[choice]["path"], now=monotonic(), graph=graph)
                    route = deepcopy(store._robots[rid]["route"])
                if committed:
                    event = common._record(order, dict(state="APPLIED", goal_node=goal, choice=choice,
                        selected_by="KEV_STRATEGY_ORDER_CODE_MIN_TIME" if calls else "SINGLE_OPTION",
                        selection_policy="KEV_STRATEGY_ORDER", kev_calls=calls, path=candidates[choice]["path"],
                        selection_seconds=monotonic()-cycle_started, coordination_seconds=monotonic()-coordination_started,
                        route_source=source, candidates=candidates, rejected_reroutes=rejected,
                        reroute_generation=list(route_cache.values()), strategy_batches=batches,
                        strategy_call_limit=call_limit, existing_plans_preserved=True, revalidated_before_apply=True,
                        retries=attempts, note="KEV는 새 요청의 전략 순서를 제안하고 코드는 검사한 경로 중 예상 시간이 짧은 경로를 선택합니다."))
                    try:
                        await asyncio.to_thread(common._save, event)
                    except OSError:
                        common.logger.exception("전략 조정 기록 저장 실패")
                    return dict(success=True, status="WAITING" if route and route["departure_at"] > monotonic() else "SUCCESS",
                                route=route, traffic_coordination=event)
            recent_checks.extend(deepcopy(batch["attempts"]))
            del recent_checks[:-16]
            attempts += 1
            if attempts == 1:
                common._record(order, dict(state="WAITING_FOR_SAFE_ROUTE", goal_node=goal,
                                          selection_policy="KEV_STRATEGY_ORDER", kev_calls=calls))
            await asyncio.sleep(.25)
        raise ValueError("교통조정 대기 한도를 초과했습니다. 전략으로 예약할 수 있는 경로가 없습니다.")
    except asyncio.CancelledError:
        common._record(order, dict(state="CANCELLED", goal_node=goal, kev_calls=calls, strategy_batches=batches))
        raise
    except TimeoutError as exc:
        common._record(order, dict(state="FAILED", goal_node=goal, kev_calls=calls,
                                  strategy_batches=batches, error="KEV 전략 응답 시간 초과"))
        raise ValueError("KEV 전략 응답 시간 한도를 초과했습니다.") from exc
    except Exception as exc:
        common._record(order, dict(state="FAILED", goal_node=goal, kev_calls=calls,
                                  strategy_batches=batches, error=str(exc)))
        raise
