"""[모듈: kev_dispatcher.py - 시뮬레이션 KEV-MAPF 경로 준비·검증 및 배차 실행 게이트]
- 역할:
  1. 주문의 AI 경로를 생성하여 FMS 주행 전 Pending 목록에 등록한다 (prepare_order_route).
  2. 준비된 경로의 실행 직전 상태 무결성을 검증한다 (validate_prepared_order_route).
  3. KEV-MAPF 분석 결과(우회 경로 REROUTE 포함)와 현재 FMS 상태를 대조 검증한다 (review_prepared_mapf_candidate).
  4. Active/Pending 통합 MAPF 평가를 거쳐 FMS `navigate_to_node()`로 최종 주행을 배차한다 (dispatch_simulation_mapf).
"""

from __future__ import annotations

import asyncio
import os
from copy import deepcopy
from time import monotonic
from typing import Any

from ..mode_service import feature_engine, mode_manager
from ..route_graph import load_route_graph
from .kev_coordinator import fms_mapf_coordinator, is_fms_reviewable_advisory
from .kev_graph import Graph
from .kev_traffic import live_traffic


async def prepare_order_route(order: dict, node: Any, current_node: Any, valid: Any = None) -> dict:
    """시뮬레이션 AI 경로를 생성하고 Pending에 등록한다. FMS 주행 요청이나 예약은 수행하지 않는다."""
    from ..llm_route_preview import preview_llm_route
    from ..mock_data import mock_fms

    mode = order["mode"]
    robot_id = order["robot_id"]

    if mode != "simulation" or mode_manager.mode != mode:
        raise ValueError("시뮬레이션 모드에서만 경로를 준비할 수 있습니다.")

    operation_mode = order.get(
        "operation_mode", "ai" if order["driving_mode"] == "llm" else "algorithm"
    )
    if feature_engine("route", operation_mode) != "ai":
        raise ValueError("AI 경로 준비 요청이 아닙니다.")
    if current_node is None:
        raise ValueError("AI 경로의 출발 노드를 확인할 수 없습니다.")
    if valid is not None and not valid():
        raise ValueError("주문 단계가 변경되었습니다.")

    before = mock_fms.robot_snapshot(robot_id, mode)
    if (
        before["goal_node"] is not None
        or before.get("route") is not None
        or before["occupied_node"] != str(current_node)
        or before.get("occupied_edge") is not None
        or before.get("status") not in (None, "IDLE")
    ):
        raise ValueError("로봇이 경로 준비 가능한 정지 상태가 아닙니다.")

    graph = load_route_graph()
    traffic = live_traffic(robot_id, graph=graph)
    try:
        result = await asyncio.to_thread(
            preview_llm_route,
            graph,
            current_node,
            node,
            selector=order["selector"],
            model=order["model"],
            traffic=traffic,
        )
    except (ValueError, RuntimeError) as exc:
        raise ValueError(f"AI 경로 생성 실패(N{current_node} → N{node}): {exc}") from exc

    if mode_manager.mode != mode or load_route_graph() != graph:
        raise ValueError("경로 생성 중 운용 모드 또는 지도가 변경되었습니다.")
    if valid is not None and not valid():
        raise ValueError("경로 생성 중 주문 단계가 변경되었습니다.")

    after = mock_fms.robot_snapshot(robot_id, mode)
    fields = (
        "status",
        "connection_state",
        "stop_requested",
        "occupied_node",
        "occupied_edge",
        "goal_node",
        "navigation_id",
        "route",
    )
    if any(before.get(k) != after.get(k) for k in fields):
        raise ValueError("경로 생성 중 로봇 상태가 변경되었습니다.")

    request = fms_mapf_coordinator.stage_preview(
        order=order, current_node=current_node, goal_node=node, result=result
    )
    return {
        "graph": graph,
        "request": request,
        "result": result,
        "required_path": list(request["result"]["path"]),
    }


def validate_prepared_order_route(
    order: dict, node: Any, current_node: Any, prepared: dict, valid: Any = None
) -> list[str]:
    """Prepared 경로의 실행 직전 유효성을 검사한다."""
    from ..mock_data import mock_fms

    robot_id = str(order["robot_id"])
    mode = order["mode"]

    if mode != "simulation" or mode_manager.mode != mode:
        raise ValueError("운용 모드가 변경되었습니다.")
    if valid is not None and not valid():
        raise ValueError("주문 단계가 변경되었습니다.")
    if load_route_graph() != prepared["graph"]:
        raise ValueError("경로 준비 이후 지도가 변경되었습니다.")

    request = prepared["request"]
    if str(request["order"]["robot_id"]) != robot_id:
        raise ValueError("준비된 경로의 로봇 ID가 다릅니다.")
    if (
        request["order"]["selector"] != order["selector"]
        or request["order"]["model"] != order["model"]
    ):
        raise ValueError("경로 생성 모델 정보가 변경되었습니다.")
    if str(request["current_node"]) != str(current_node) or str(request["node"]) != str(node):
        raise ValueError("준비된 경로의 출발지 또는 목적지가 다릅니다.")

    path = [str(n) for n in prepared["required_path"]]
    if (
        not path
        or path[0] != str(current_node)
        or path[-1] != str(node)
        or path != [str(n) for n in request["result"]["path"]]
        or path != [str(n) for n in prepared["result"]["path"]]
    ):
        raise ValueError("준비된 경로 데이터가 일치하지 않습니다.")

    pending = next(
        (
            item
            for item in fms_mapf_coordinator.pending_snapshot()
            if str(item["order"]["robot_id"]) == robot_id
        ),
        None,
    )
    if pending != request:
        raise ValueError("Pending 경로가 변경되거나 삭제되었습니다.")

    snapshot = mock_fms.robot_snapshot(robot_id, mode)
    if (
        snapshot["status"] != "IDLE"
        or snapshot["occupied_node"] != str(current_node)
        or snapshot.get("occupied_edge") is not None
        or snapshot["goal_node"] is not None
        or snapshot.get("route") is not None
    ):
        raise ValueError("경로 준비 이후 로봇 상태가 변경되었습니다.")

    return path


def dispatch_prepared_order_route(
    order: dict,
    node: Any,
    current_node: Any,
    prepared: dict,
    *,
    valid: Any = None,
    test_only: bool = False,
) -> dict:
    """Prepared 경로를 재생성 없이 FMS에 전달한다 (테스트 전용 헬퍼)."""
    if not test_only:
        raise RuntimeError("MAPF 실행 게이트가 아직 연결되지 않았습니다.")
    if order["mode"] != "simulation":
        raise ValueError("시뮬레이션에서만 사용할 수 있습니다.")

    from ..mock_data import mock_fms

    path = validate_prepared_order_route(order, node, current_node, prepared, valid=valid)
    response = mock_fms.navigate_to_node(order["robot_id"], node, required_path=path)
    try:
        fms_mapf_coordinator.register_preview(
            order=order,
            current_node=current_node,
            goal_node=node,
            result=prepared["result"],
        )
    finally:
        fms_mapf_coordinator.discard_pending(order["robot_id"])
    return response


def review_prepared_mapf_candidate(
    order: dict,
    node: Any,
    current_node: Any,
    prepared: dict,
    advisory: dict,
    *,
    valid: Any = None,
) -> dict:
    """MAPF 실행 후보(우회 경로 REROUTE 포함)와 Prepared 경로를 검증하고 최종 주행 경로를 확정한다."""
    robot_id = str(order["robot_id"])

    def blocked(reason: str) -> dict:
        return {
            "status": "BLOCKED",
            "reason": reason,
            "robot_id": robot_id,
            "required_path": None,
            "fms_execution_approved": False,
        }

    fms_before = fms_mapf_coordinator._simulation_signature()
    pending_before = fms_mapf_coordinator.pending_snapshot()

    policy = fms_mapf_coordinator.classify_mixed_pending_advisory(advisory)
    if policy["status"] != "REVIEW_ONLY":
        return blocked(policy.get("reason", "MAPF_POLICY_NOT_READY"))
    if robot_id not in advisory.get("pending_robot_ids", []):
        return blocked("ROBOT_NOT_PENDING")
    if robot_id not in policy["fms_check_candidates"]:
        return blocked("MAPF_ROBOT_DEFERRED")

    try:
        path = validate_prepared_order_route(
            order, node, current_node, prepared, valid=valid
        )
    except (ValueError, KeyError, TypeError):
        return blocked("PREPARED_ROUTE_INVALID")

    required_paths = advisory.get("required_paths")
    if not isinstance(required_paths, dict):
        return blocked("MAPF_REQUIRED_PATHS_INVALID")

    analyzed_raw = required_paths.get(robot_id)
    if not isinstance(analyzed_raw, list) or not analyzed_raw:
        return blocked("MAPF_REQUIRED_PATHS_INVALID")
    analyzed_path = [str(n) for n in analyzed_raw]

    rerouted_map = policy.get("rerouted") or {}
    if robot_id in rerouted_map:
        if analyzed_path != rerouted_map[robot_id]:
            return blocked("MAPF_REROUTE_PATH_MISMATCH")
        try:
            Graph(prepared["graph"]).check_path(
                [int(n) for n in analyzed_path],
                start=int(current_node),
                goal=int(node),
            )
        except Exception:
            return blocked("MAPF_REROUTE_PATH_INVALID")
        final_path = analyzed_path
    else:
        if analyzed_path != path:
            return blocked("MAPF_PREPARED_PATH_MISMATCH")
        final_path = path

    if (
        fms_before != fms_mapf_coordinator._simulation_signature()
        or pending_before != fms_mapf_coordinator.pending_snapshot()
    ):
        return blocked("STATE_CHANGED_DURING_REVIEW")

    return {
        "status": "REVIEW_ONLY",
        "robot_id": robot_id,
        "required_path": list(final_path),
        "rerouted": robot_id in rerouted_map,
        "next_step": "FMS_RESERVATION_CHECK",
        "fms_execution_approved": False,
    }


async def dispatch_simulation_mapf(
    order: dict, node: Any, current_node: Any, valid: Any = None
) -> dict:
    """KEV-MAPF 판단(WAIT / REROUTE / HOLD) 후 FMS에 배차하는 시뮬레이션 실행 게이트."""
    from ..mock_data import mock_fms

    c = fms_mapf_coordinator
    rid = str(order["robot_id"])

    with c._lock, mock_fms._lock:
        state = mock_fms.robot_snapshot(rid, "simulation")
        if (
            state["status"] == "IDLE"
            and state["goal_node"] is None
            and state["route"] is None
        ):
            c.remove(rid)

    prepared = await prepare_order_route(order, node, current_node, valid=valid)
    deadline = monotonic() + float(os.getenv("FMS_SIM_MAPF_MAX_WAIT", "180"))
    max_reroutes = int(os.getenv("FMS_MAPF_MAX_REROUTES", "3"))
    last_state = None

    try:
        await asyncio.sleep(0.5)

        while True:
            if valid is not None and not valid():
                raise ValueError("주문 단계가 변경되었습니다.")
            if mode_manager.mode != "simulation":
                raise ValueError("운용 모드가 변경되었습니다.")
            if monotonic() > deadline:
                raise ValueError("KEV-MAPF 배차 대기시간을 초과했습니다.")

            retired = c.retire_completed_simulation_routes()
            if retired["status"] != "RECONCILED":
                await asyncio.sleep(0.2)
                continue

            current_state = c._state_triplet()
            if current_state == last_state:
                await asyncio.sleep(0.2)
                continue

            mixed = c.prepare_mixed_simulation_requests()
            if mixed["issues"]:
                last_state = None
                await asyncio.sleep(0.2)
                continue

            single = (
                len(mixed["requests"]) == 1
                and mixed["pending_robot_ids"] == [rid]
                and not mixed["active_robot_ids"]
            )
            advisory = None

            if not single:
                if mixed["status"] != "READY":
                    last_state = None
                    await asyncio.sleep(0.2)
                    continue

                wait_nodes = [
                    int(req["current_node"])
                    for req in mixed["requests"]
                    if req["order"]["robot_id"] in mixed["pending_robot_ids"]
                ]
                advisory = await asyncio.to_thread(
                    c.evaluate_mixed_simulation_advisory,
                    prepared["graph"],
                    config={
                        "wait_allowed_nodes": wait_nodes,
                        "max_rounds": 20,
                        "max_reroutes": max_reroutes,
                        "priority_mode": os.getenv("FMS_MAPF_PRIORITY_MODE", "score"),
                        "allow_proactive_hold": os.getenv("FMS_MAPF_ALLOW_HOLD", "0") == "1",
                    },
                )
                if not is_fms_reviewable_advisory(advisory):
                    last_state = None
                    await asyncio.sleep(0.2)
                    continue

                policy = c.classify_mixed_pending_advisory(advisory)
                if policy["status"] != "REVIEW_ONLY":
                    last_state = None
                    await asyncio.sleep(0.2)
                    continue
                if rid not in policy["fms_check_candidates"]:
                    last_state = deepcopy(current_state)
                    continue

            with c._lock, mock_fms._lock:
                if valid is not None and not valid():
                    raise ValueError("주문 단계가 변경되었습니다.")

                if single:
                    latest = c.prepare_mixed_simulation_requests()
                    if (
                        latest["issues"]
                        or len(latest["requests"]) != 1
                        or latest["pending_robot_ids"] != [rid]
                        or latest["active_robot_ids"]
                    ):
                        last_state = None
                        continue
                    path = validate_prepared_order_route(
                        order, node, current_node, prepared, valid=valid
                    )
                else:
                    reviewed = review_prepared_mapf_candidate(
                        order, node, current_node, prepared, advisory, valid=valid
                    )
                    if reviewed["status"] != "REVIEW_ONLY":
                        last_state = None
                        continue
                    path = reviewed["required_path"]

                response = mock_fms.navigate_to_node(rid, node, required_path=path)
                try:
                    c.register_preview(
                        order=order,
                        current_node=current_node,
                        goal_node=node,
                        result={"path": path},
                    )
                except Exception:
                    mock_fms.stop_robot(rid)
                    c.remove(rid)
                    raise

                c.discard_pending(rid)
                return response
    finally:
        c.discard_pending(rid)
