"""주문 이동에 선택된 Algorithm/AI 경로를 기존 주행기로 전달한다."""

import asyncio

from .mode_service import mode_manager, feature_engine
from .route_graph import load_route_graph
"""MAPF 
경로 생성만함 """
async def prepare_order_route(order, node, current_node, valid=None):
    """시뮬레이션 AI 경로를 생성하고 Pending에 등록한다.

    FMS 주행 요청이나 예약은 수행하지 않는다.
    """

    from .mock_data import mock_fms
    from .llm_route_preview import preview_llm_route
    from .fms_mapf_coordinator import fms_mapf_coordinator

    mode = order["mode"]
    robot_id = order["robot_id"]

    # 이번 단계에서는 시뮬레이션 AI 경로만 지원
    if mode != "simulation" or mode_manager.mode != mode:
        raise ValueError("시뮬레이션 모드에서만 경로를 준비할 수 있습니다.")

    operation_mode = order.get(
        "operation_mode",
        "ai" if order["driving_mode"] == "llm" else "algorithm",
    )

    if feature_engine("route", operation_mode) != "ai":
        raise ValueError("AI 경로 준비 요청이 아닙니다.")

    if current_node is None:
        raise ValueError("AI 경로의 출발 노드를 확인할 수 없습니다.")

    if valid is not None and not valid():
        raise ValueError("주문 단계가 변경되었습니다.")

    # 경로 생성 전 FMS 상태 확인
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

    # KEV 경로 생성: FMS는 움직이지 않음
    try:
        result = await asyncio.to_thread(
            preview_llm_route,
            graph,
            current_node,
            node,
            selector=order["selector"],
            model=order["model"],
        )
    except (ValueError, RuntimeError) as exc:
        raise ValueError(
            f"AI 경로 생성 실패(N{current_node} → N{node}): {exc}"
        ) from exc

    # 경로 생성 중 운용 환경 변경 검사
    if mode_manager.mode != mode or load_route_graph() != graph:
        raise ValueError("경로 생성 중 운용 모드 또는 지도가 변경되었습니다.")

    if valid is not None and not valid():
        raise ValueError("경로 생성 중 주문 단계가 변경되었습니다.")

    # 비동기 경로 생성 중 로봇 상태 변경 검사
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

    if any(before.get(key) != after.get(key) for key in fields):
        raise ValueError("경로 생성 중 로봇 상태가 변경되었습니다.")

    # 주행 요청 없이 Pending에 경로만 저장
    request = fms_mapf_coordinator.stage_preview(
        order=order,
        current_node=current_node,
        goal_node=node,
        result=result,
    )

    return {
        "graph": graph,
        "request": request,
        "result": result,
        "required_path": list(request["result"]["path"]),
    }
"""MAPF 
경로 생성 후 유효성 검사 """
def validate_prepared_order_route(
    order,
    node,
    current_node,
    prepared,
    valid=None,
):
    """Prepared 경로의 실행 직전 유효성을 검사한다.

    FMS 주행 요청 및 예약을 수행하지 않는다.
    """

    from .mock_data import mock_fms
    from .fms_mapf_coordinator import fms_mapf_coordinator

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

    if (
        str(request["current_node"]) != str(current_node)
        or str(request["node"]) != str(node)
    ):
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
    order,
    node,
    current_node,
    prepared,
    *,
    valid=None,
    test_only=False,
):
    """Prepared 경로를 재생성 없이 FMS에 전달한다.

    현재는 테스트 전용이다.
    MAPF 실행 게이트 연결 전에는 일반 실행을 허용하지 않는다.
    """

    if not test_only:
        raise RuntimeError(
            "MAPF 실행 게이트가 아직 연결되지 않았습니다."
        )

    if order["mode"] != "simulation":
        raise ValueError("시뮬레이션에서만 사용할 수 있습니다.")

    from .mock_data import mock_fms
    from .fms_mapf_coordinator import fms_mapf_coordinator

    # 1. Prepared 경로 유효성 검사
    path = validate_prepared_order_route(
        order,
        node,
        current_node,
        prepared,
        valid=valid,
    )

    # 2. 기존 FMS에 경로 전달
    #    LLM 경로를 다시 생성하지 않는다.
    response = mock_fms.navigate_to_node(
        order["robot_id"],
        node,
        required_path=path,
    )

    # 3. 성공적으로 전달한 경로를 Active에 등록
    try:
        fms_mapf_coordinator.register_preview(
            order=order,
            current_node=current_node,
            goal_node=node,
            result=prepared["result"],
        )
    finally:
        fms_mapf_coordinator.discard_pending(
            order["robot_id"]
        )

    return response
"""
MAPF
주문 경로를 배차한다.
"""
async def dispatch_order_route(order, node, current_node, valid=None):
    mode = order["mode"]
    if mode_manager.mode != mode:
        raise ValueError("운용 모드가 변경되었습니다.")
    
        # 시뮬레이션 AI 주행만 KEV-MAPF 실행 게이트 사용
    import os

    operation_mode = order.get(
        "operation_mode",
        "ai" if order["driving_mode"] == "llm" else "algorithm",
    )

    if (
        mode == "simulation"
        and os.getenv("FMS_SIM_MAPF_ENABLED", "0") == "1"
        and feature_engine("route", operation_mode) == "ai"
    ):
        return await _dispatch_simulation_mapf(
            order, node, current_node, valid=valid
        )

    required_path = None
    operation_mode = order.get("operation_mode", "ai" if order["driving_mode"] == "llm" else "algorithm")
    if feature_engine("route", operation_mode) == "ai":
        if current_node is None:
            raise ValueError("AI 경로의 출발 노드를 확인할 수 없습니다.")
        from .llm_route_preview import preview_llm_route

        graph = load_route_graph()
        try:
            result = await asyncio.to_thread(
                preview_llm_route, graph, current_node, node, selector=order["selector"], model=order["model"]
            )
        except (ValueError, RuntimeError) as exc:
            raise ValueError(f"AI 경로 생성 실패(N{current_node} → N{node}): {exc}") from exc
        required_path = result["path"]
        if mode_manager.mode != mode or load_route_graph() != graph:
            raise ValueError("AI 경로 생성 중 운용 모드 또는 지도가 변경되었습니다.")

    if valid is not None and not valid():
        raise ValueError("주문 단계가 변경되어 경로를 취소했습니다.")
    if mode == "simulation":
        from .mock_data import mock_fms

        snapshot = mock_fms.robot_snapshot(order["robot_id"], mode)
        if snapshot["goal_node"] is not None:
            raise ValueError("경로 생성 중 로봇의 현재 위치가 변경되었습니다.")
        if current_node is None:
            # Algorithm planning already supports an occupied edge as a
            # starting resource. AI paths require a confirmed start node.
            if (
                order["driving_mode"] != "standard"
                or snapshot["occupied_node"] is not None
                or snapshot["occupied_edge"] is None
            ):
                raise ValueError("로봇의 출발 노드를 확인할 수 없습니다.")
        elif snapshot["occupied_node"] != str(current_node):
            raise ValueError("경로 생성 중 로봇의 현재 위치가 변경되었습니다.")
        
        """MAPF 
            조정기 사이에 AI 경로를 등록한다."""
                # AI 경로를 FMS 주행 요청 전에 임시 등록
                # 1. FMS 실행 전에 AI 경로를 Pending에 등록
        if required_path is not None:
            from .fms_mapf_coordinator import fms_mapf_coordinator

            fms_mapf_coordinator.stage_preview(
                order=order,
                current_node=current_node,
                goal_node=node,
                result=result,
            )

        # 2. 기존 FMS에 주행 요청 (단 한 번)
        try:
            response = mock_fms.navigate_to_node(
                order["robot_id"],
                node,
                required_path=required_path,
            )
        except Exception:
            if required_path is not None:
                fms_mapf_coordinator.discard_pending(order["robot_id"])
            raise

        # 3. FMS가 요청을 접수한 경로를 Active에 등록
        if required_path is not None:
            try:
                fms_mapf_coordinator.register_preview(
                    order=order,
                    current_node=current_node,
                    goal_node=node,
                    result=result,
                )
            finally:
                fms_mapf_coordinator.discard_pending(order["robot_id"])

        return response

    from ..ros2.ros_gateway import ros_gateway
    from .fleet_manager import fleet_manager

    robot = fleet_manager.get_robot(order["robot_id"])
    if robot is None or not robot.connected or robot.occupied_node != str(current_node):
        raise ValueError("경로 생성 중 실제 로봇의 현재 위치가 변경되었습니다.")
    return ros_gateway.navigate_to_node(order["robot_id"], node, _order_step=True, required_path=required_path)

"""
MAPF
    
"""
def review_prepared_mapf_candidate(
    order,
    node,
    current_node,
    prepared,
    advisory,
    *,
    valid=None,
):
    """MAPF 실행 후보와 Prepared 경로를 검증한다.

    FMS 예약 및 실제 주행 명령은 수행하지 않는다.
    """

    from .fms_mapf_coordinator import fms_mapf_coordinator

    robot_id = str(order["robot_id"])

    def blocked(reason):
        return {
            "status": "BLOCKED",
            "reason": reason,
            "robot_id": robot_id,
            "required_path": None,
            "fms_execution_approved": False,
        }

    # 1. 검증 전 FMS와 Pending 상태 저장
    fms_before = fms_mapf_coordinator._simulation_signature()
    pending_before = fms_mapf_coordinator.pending_snapshot()

    # 2. KEV-MAPF 실행 정책 재검증
    policy = fms_mapf_coordinator.classify_mixed_pending_advisory(
        advisory
    )

    if policy["status"] != "REVIEW_ONLY":
        return blocked(
            policy.get("reason", "MAPF_POLICY_NOT_READY")
        )

    # 3. Pending 로봇인지 확인
    if robot_id not in advisory.get("pending_robot_ids", []):
        return blocked("ROBOT_NOT_PENDING")

    # 4. WAIT / HOLD / REROUTE 대상이면 실행 후보에서 제외
    if robot_id not in policy["fms_check_candidates"]:
        return blocked("MAPF_ROBOT_DEFERRED")

    # 5. Prepared 경로 유효성 검사
    try:
        path = validate_prepared_order_route(
            order,
            node,
            current_node,
            prepared,
            valid=valid,
        )
    except (ValueError, KeyError, TypeError):
        return blocked("PREPARED_ROUTE_INVALID")

    # 6. KEV-MAPF 분석 당시 경로와 비교
    required_paths = advisory.get("required_paths")

    if not isinstance(required_paths, dict):
        return blocked("MAPF_REQUIRED_PATHS_INVALID")

    analyzed_path = required_paths.get(robot_id)

    if (
        not isinstance(analyzed_path, list)
        or [str(n) for n in analyzed_path] != path
    ):
        return blocked("MAPF_PREPARED_PATH_MISMATCH")

    # 7. 검증 중 상태가 변경됐으면 결과 폐기
    if (
        fms_before != fms_mapf_coordinator._simulation_signature()
        or pending_before != fms_mapf_coordinator.pending_snapshot()
    ):
        return blocked("STATE_CHANGED_DURING_REVIEW")

    return {
        "status": "REVIEW_ONLY",
        "robot_id": robot_id,
        "required_path": list(path),
        "next_step": "FMS_RESERVATION_CHECK",
        "fms_execution_approved": False,
    }
"""
MAPF
"""
async def _dispatch_simulation_mapf(order, node, current_node, valid=None):
    """KEV-MAPF 판단 후 FMS에 배차하는 시뮬레이션 실행 게이트."""

    import os
    from copy import deepcopy
    from time import monotonic

    from .mock_data import mock_fms
    from .fms_mapf_coordinator import fms_mapf_coordinator as c
    from .fms_mapf_coordinator import is_fms_reviewable_advisory

    rid = str(order["robot_id"])

    # 이전 경로가 중간에서 종료됐고 정지가 확인된 경우 기록 정리
    with c._lock, mock_fms._lock:
        state = mock_fms.robot_snapshot(rid, "simulation")
        if (
            state["status"] == "IDLE"
            and state["goal_node"] is None
            and state["route"] is None
        ):
            c.remove(rid)

    # 1. KEV 경로 생성 및 Pending 등록
    prepared = await prepare_order_route(
        order, node, current_node, valid=valid
    )

    deadline = monotonic() + float(
        os.getenv("FMS_SIM_MAPF_MAX_WAIT", "180")
    )
    last_state = None

    try:
        # 다른 주문의 경로가 등록될 기회를 잠시 제공
        await asyncio.sleep(0.5)

        while True:
            if valid is not None and not valid():
                raise ValueError("주문 단계가 변경되었습니다.")

            if mode_manager.mode != "simulation":
                raise ValueError("운용 모드가 변경되었습니다.")

            if monotonic() > deadline:
                raise ValueError("KEV-MAPF 배차 대기시간을 초과했습니다.")

            # 2. 완료된 Active 경로 정리
            retired = c.retire_completed_simulation_routes()

            if retired["status"] != "RECONCILED":
                await asyncio.sleep(0.2)
                continue

            # FMS 상태가 바뀌었을 때만 재분석
            current_state = (
                c._simulation_signature(),
                c.pending_snapshot(),
                c.snapshot(),
            )

            if current_state == last_state:
                await asyncio.sleep(0.2)
                continue

            last_state = deepcopy(current_state)

            # 3. Active + Pending 통합
            mixed = c.prepare_mixed_simulation_requests()

            if mixed["issues"]:
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
                    await asyncio.sleep(0.2)
                    continue

                # 시뮬레이션에서는 Pending 출발 노드를
                # 대기 가능 위치 후보로 사용
                wait_nodes = [
                    int(req["current_node"])
                    for req in mixed["requests"]
                    if req["order"]["robot_id"]
                    in mixed["pending_robot_ids"]
                ]

                # 4. 실제 KEV-MAPF 분석
                advisory = await asyncio.to_thread(
                    c.evaluate_mixed_simulation_advisory,
                    prepared["graph"],
                    config={
                    "wait_allowed_nodes": wait_nodes,
                    "max_rounds": 20,
                    "max_reroutes": 0,

                    # KEV 우선순위: choice 또는 score
                    "priority_mode": os.getenv(
                        "FMS_MAPF_PRIORITY_MODE", "score"
                    ),

                    # WAIT 가능해도 HOLD를 선택할 수 있는지 설정
                    "allow_proactive_hold": (
                        os.getenv("FMS_MAPF_ALLOW_HOLD", "0") == "1"
                    ),
                },
                )
                print(
                    f"[MAPF RESULT] robot={rid} "
                    f"status={advisory.get('status')} "
                    f"reason={advisory.get('reason')} "
                    f"decisions={[(d.get('action'), d.get('reason')) for d in advisory.get('decisions', [])]} "
                    f"warnings={advisory.get('warnings')}",
                    flush=True,
                )

                if not is_fms_reviewable_advisory(advisory):
                    continue

                policy = c.classify_mixed_pending_advisory(advisory)
                # MAPF 분석 결과가 검토 단계에 진입할 수 있는지 확인
                reviewable = is_fms_reviewable_advisory(advisory)

                print(
                    f"[MAPF GATE] robot={rid} "
                    f"status={advisory.get('status')} "
                    f"reviewable={reviewable} "
                    f"warnings={advisory.get('warnings')}",
                    flush=True,
                )

                if not reviewable:
                    continue

                policy = c.classify_mixed_pending_advisory(advisory)

                print(
                    f"[MAPF POLICY] robot={rid} "
                    f"status={policy.get('status')} "
                    f"reason={policy.get('reason')} "
                    f"candidates={policy.get('fms_check_candidates')} "
                    f"deferred={policy.get('deferred')}",
                    flush=True,
                )

                if (
                    policy["status"] != "REVIEW_ONLY"
                    or rid not in policy["fms_check_candidates"]
                ):
                    continue

            # 5. Coordinator와 FMS의 잠금을 유지한 채 최종 재검사
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
                        continue

                    path = validate_prepared_order_route(
                        order, node, current_node,
                        prepared, valid=valid,
                    )

                else:
                    reviewed = review_prepared_mapf_candidate(
                        order,
                        node,
                        current_node,
                        prepared,
                        advisory,
                        valid=valid,
                    )

                    if reviewed["status"] != "REVIEW_ONLY":
                        continue

                    path = reviewed["required_path"]

                # 6. 기존 TrafficManager에 주행 요청
                # MAPF가 아니라 FMS가 예약 가능 여부를 결정
                response = mock_fms.navigate_to_node(
                    rid,
                    node,
                    required_path=path,
                )

                try:
                    c.register_preview(
                        order=order,
                        current_node=current_node,
                        goal_node=node,
                        result={"path": path},
                    )
                except Exception:
                    # 시뮬레이션 요청이 접수됐지만 기록에 실패했다면 정지
                    mock_fms.stop_robot(rid)
                    c.remove(rid)
                    raise

                c.discard_pending(rid)

                print(
                    f"[FMS MAPF DISPATCH] robot={rid} "
                    f"path={path} status={response['status']}",
                    flush=True,
                )

                return response

    finally:
        c.discard_pending(rid)