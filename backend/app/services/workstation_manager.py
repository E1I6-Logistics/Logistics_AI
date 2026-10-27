"""N3/N4 컨베이어의 점유·예약과 KEV 기반 분산 선택을 함께 관리한다."""

import os
from threading import RLock
from time import monotonic
from collections import deque

from ..config import REAL_POSE_TIMEOUT_S
from .kev_mapf.kev_workstation_dispatcher import kev_workstation_dispatcher
from .route_service import plan_route


class WorkstationManager:
    # WorkstationManager의 초기 상태를 설정한다.
    def __init__(self):
        self._lock = RLock()
        self._assigned = {}  # node -> order_id
        self._assignment_history = deque()  # (시각, 작업대 노드)

    def _available_nodes(self, order_id, mode):
        occupied, unknown = self._occupied(mode)
        if unknown:
            return []
        return [
            node for node in ("3", "4")
            if node not in occupied and self._assigned.get(node) in (None, order_id)
        ]

    def _record_assignment(self, node):
        now = monotonic()
        window = float(os.getenv("KEV_WORKSTATION_WORKLOAD_WINDOW_SECONDS", "600"))
        while self._assignment_history and self._assignment_history[0][0] < now - window:
            self._assignment_history.popleft()
        self._assignment_history.append((now, node))

    def _reserve(self, order_id, node):
        self._assigned[node] = order_id
        self._record_assignment(node)
        return node

    # 작업대가 현재 점유 중인지 확인한다.
    def _occupied(self, mode):
        if mode == "simulation":
            from .mock_data import mock_fms

            rows = mock_fms.robot_snapshots(mode)
            return {str(row["occupied_node"]) for row in rows if row.get("occupied_node") is not None}, False

        from .fleet_manager import fleet_manager

        rows = fleet_manager.get_all_robots()
        occupied = {str(robot.occupied_node) for robot in rows if robot.occupied_node is not None}
        # 연결된 로봇의 현재 점유를 모르면 빈 작업대를 확정할 수 없다.
        unknown = any(
            robot.connected
            and (
                (robot.occupied_node is None and robot.occupied_edge is None)
                or robot.pose_received_at is None
                or monotonic() - robot.pose_received_at > REAL_POSE_TIMEOUT_S
            )
            for robot in rows
        )
        return occupied, unknown

    # 관련을 선택한다.
    def choose(self, order_id, mode, operation_mode="algorithm"):
        with self._lock:
            nodes = self._available_nodes(order_id, mode)
            return self._reserve(order_id, nodes[0]) if nodes else None

    async def choose_with_kev(self, order_id, mode, operation_mode, robot_id, current_node):
        """AI 모드에서 비어 있는 N3/N4 중 한 곳을 KEV가 고르게 한다."""
        if operation_mode != "ai" or os.getenv("KEV_WORKSTATION_ENABLED", "1") != "1":
            return self.choose(order_id, mode, operation_mode)
        with self._lock:
            nodes = self._available_nodes(order_id, mode)
            if len(nodes) <= 1:
                return self._reserve(order_id, nodes[0]) if nodes else None
            if current_node is None:
                return None
            counts = {node: sum(1 for _, assigned_node in self._assignment_history if assigned_node == node) for node in nodes}
            distances = {node: float(plan_route(str(current_node), node)["cost"]) for node in nodes}
            state = {
                "order": {"order_id": order_id, "robot_id": robot_id, "current_node": str(current_node)},
                "workstations": [
                    {"node": node, "route_distance_m": distances[node], "recent_assignment_count": counts[node]}
                    for node in nodes
                ],
            }
            choices = {
                node: f"node=N{node}, route_distance_m={distances[node]:.3f}, recent_assignment_count={counts[node]}"
                for node in nodes
            }

        try:
            chosen = await kev_workstation_dispatcher.select(state, choices, f"workstation_{order_id}")
        except Exception:
            # KEV 장애 시에도 N3 고정 대신 최근 작업량과 거리로 균형 있게 대체한다.
            chosen = min(nodes, key=lambda node: (counts[node], distances[node], node))

        with self._lock:
            available = self._available_nodes(order_id, mode)
            if chosen in available:
                return self._reserve(order_id, chosen)
            if not available:
                return None
            return self._reserve(order_id, min(available, key=lambda node: (counts.get(node, 0), distances.get(node, float("inf")), node)))

    # 관련을 해제한다.
    def release(self, order_id):
        with self._lock:
            for node, assigned_order in list(self._assigned.items()):
                if assigned_order == order_id:
                    del self._assigned[node]

    # 현재 상태를 반환한다.
    def status(self, mode):
        with self._lock:
            occupied, unknown = self._occupied(mode)
            result = {}
            for node in ("3", "4"):
                if node in occupied:
                    state = "OCCUPIED"
                elif unknown:
                    state = "UNKNOWN"
                elif node in self._assigned:
                    state = "ASSIGNED"
                else:
                    state = "FREE"
                result[node] = {"status": state, "order_id": self._assigned.get(node)}
            return result

    def pickup_status(self, mode, orders):
        """Read pickup occupancy and reservations without changing traffic."""
        from .reservation import reservation_tables, node_key

        if mode == "simulation":
            from .mock_data import mock_fms

            locations = {r["robot_id"]: r["occupied_node"] for r in mock_fms.robot_snapshots(mode)}
        else:
            from .fleet_manager import fleet_manager

            locations = {r.robot_id: r.occupied_node for r in fleet_manager.get_all_robots()}
        _, unknown = self._occupied(mode)
        reservations = reservation_tables[mode].snapshot()
        now = monotonic()
        result = {}
        for node in ("5", "6"):
            robot_ids = [robot_id for robot_id, occupied in locations.items() if occupied == node]
            active = [
                row
                for row in orders
                if row["mode"] == mode and row["status"] == "IN_PROGRESS" and node in row["pickup_nodes"]
            ]
            occupants = [row for row in active if row["robot_id"] in robot_ids]
            waiting = [row["order_name"] for row in active if row["phase"] == "WAITING_PICKUP"]
            reserved = sorted({r.robot_id for r in reservations if r.resource == node_key(node) and r.end > now})
            status = "OCCUPIED" if robot_ids else "UNKNOWN" if unknown else "FREE"
            result[node] = {
                "status": status,
                "robot_ids": robot_ids,
                "order_name": occupants[0]["order_name"] if occupants else None,
                "phase": occupants[0]["phase"] if occupants else None,
                "waiting_orders": waiting,
                "reserved_robot_ids": reserved,
            }
        return result


workstation_manager = WorkstationManager()
