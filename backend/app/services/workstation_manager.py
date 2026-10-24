"""N3/N4의 실제 점유와 주문별 임시 할당을 함께 관리한다."""

from threading import RLock
from time import monotonic

from ..config import REAL_POSE_TIMEOUT_S
from .pickup_queue import requested_pickup_node


class WorkstationManager:
    # WorkstationManager의 초기 상태를 설정한다.
    def __init__(self):
        self._lock = RLock()
        self._assigned = {}  # node -> order_id

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
        from .mode_service import feature_engine

        # A future AI selector can branch here without changing traffic control.
        if feature_engine("workstation", operation_mode) != "algorithm":
            raise ValueError("지원하지 않는 작업대 선택 방식입니다.")
        with self._lock:
            occupied, unknown = self._occupied(mode)
            if unknown:
                return None
            for node in ("3", "4"):
                if node not in occupied and self._assigned.get(node) in (None, order_id):
                    self._assigned[node] = order_id
                    return node
            return None

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
            waiting = [row["order_name"] for row in active
                       if row["phase"] == "WAITING_PICKUP" and requested_pickup_node(row) == node]
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
