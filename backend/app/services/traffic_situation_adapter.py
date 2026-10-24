"""경로 생성 DTO + 같은 시점의 FMS 전체 상태 → 정지 상태 TrafficSituation.

jhlee의 order/current_node/node/result.path 형식을 받는다. 이동·예약 상태를
IDLE/빈 예약으로 치환하지 않는다. 상태 수집 잠금과 실행 전 재검사는 호출자 책임이다.
"""

from copy import deepcopy

from .traffic_joint_actions import SCHEMA_VERSION, node_id


class TrafficInputDeferred(ValueError):
    """현재 입력은 정지 상태 실험 범위 밖이므로 KEV/실행 전에 보류한다."""

    def __init__(self, reasons):
        self.reasons = list(reasons)
        super().__init__("교통조정 입력 보류: " + "; ".join(self.reasons))


def build_traffic_situation_input(
    *, graph, route_requests, robot_states, reservations, protected_resources,
    wait_allowed_nodes, snapshot_id, request_metadata, mode="simulation",
    model="kev-08b", config=None,
):
    """순서·대기 시간은 호출자가 제공한다. 요청이 없는 로봇도 정적 점유로 포함한다.

    robot_states는 전체 fleet의 ID→상태 매핑이며 connected/fresh/active와
    occupied_node/status/occupied_edge/navigation_id/route/goal_node를 포함한다.
    request_metadata는 요청 로봇별 request_order/waiting_seconds다. jhlee DTO에
    없는 값을 실제 우선순위인 것처럼 추측하지 않는다.
    """
    reasons = []
    if reservations is None or protected_resources is None:
        reasons.append("MISSING_RESOURCE_STATE")
    if mode != "simulation":
        reasons.append("SIMULATION_ONLY")
    if reservations:
        reasons.append("ACTIVE_RESERVATIONS")
    if protected_resources:
        reasons.append("PROTECTED_RESOURCES")
    if not robot_states:
        reasons.append("MISSING_FLEET_STATE")
    required = {
        "status", "occupied_node", "occupied_edge", "navigation_id", "route",
        "goal_node", "connected", "fresh", "active",
    }
    occupied = {}
    for rid, state in robot_states.items():
        if not required <= state.keys():
            reasons.append(f"{rid}: MISSING_STATE_FIELDS")
            continue
        if state["connected"] is not True or state["fresh"] is not True:
            reasons.append(f"{rid}: DISCONNECTED_OR_STALE")
        if (
            state["status"] != "IDLE" or state["active"] is not False
            or any(state[key] is not None for key in (
                "occupied_edge", "navigation_id", "route", "goal_node"
            ))
        ):
            reasons.append(f"{rid}: ACTIVE_OR_MID_EDGE")
        if state.get("order_id") is not None:
            reasons.append(f"{rid}: ACTIVE_ORDER")
        try:
            current = node_id(state["occupied_node"])
        except ValueError:
            reasons.append(f"{rid}: UNKNOWN_NODE")
            continue
        if current in occupied:
            reasons.append(f"{rid}: DUPLICATE_NODE_OCCUPANCY")
        occupied[current] = rid
    requests = {}
    for raw in route_requests:
        rid = raw["order"]["robot_id"]
        if rid in requests:
            reasons.append(f"{rid}: DUPLICATE_REQUEST")
        requests[rid] = raw
        if rid not in robot_states:
            reasons.append(f"{rid}: MISSING_ROBOT_STATE")
            continue
        state = robot_states[rid]
        if state.get("occupied_node") is None or node_id(raw["current_node"]) != str(state["occupied_node"]):
            reasons.append(f"{rid}: STALE_ROUTE_START")
        if "status" in raw and raw["status"] != state.get("status"):
            reasons.append(f"{rid}: CONTRADICTORY_STATUS")
        if rid not in request_metadata or not {"request_order", "waiting_seconds"} <= request_metadata[rid].keys():
            reasons.append(f"{rid}: MISSING_REQUEST_METADATA")
    if reasons:
        raise TrafficInputDeferred(reasons)

    joined = []
    for rid, state in robot_states.items():
        current = node_id(state["occupied_node"])
        raw = requests.get(rid)
        request = deepcopy(raw) if raw is not None else dict(
            order={"robot_id": rid, "selector": "kev", "model": model},
            current_node=current, node=current, result={"path": [current]},
        )
        request.update({key: deepcopy(state[key]) for key in (
            "status", "occupied_edge", "navigation_id", "route", "goal_node"
        )})
        request.update(
            current_node=current, node=node_id(request["node"]),
            request_order=request_metadata[rid]["request_order"] if raw is not None else 0,
            waiting_seconds=request_metadata[rid]["waiting_seconds"] if raw is not None else 0,
        )
        joined.append(request)
    # 실제 점유·예약 필드는 추가 설정으로 덮어쓰지 않는다.
    settings = deepcopy(config or {})
    settings.update(
        wait_allowed_nodes=list(wait_allowed_nodes),
        zone_occupancy={f"node:{node}": rid for node, rid in occupied.items()},
        reservations=deepcopy(list(reservations)),
    )
    return dict(
        schema_version=SCHEMA_VERSION, snapshot_id=snapshot_id, mode=mode,
        graph=deepcopy(graph), route_requests=joined, config=settings,
        input_source="ROUTE_RESULTS_AND_FMS_SNAPSHOT",
    )
