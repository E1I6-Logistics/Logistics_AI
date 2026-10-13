"""N3/N4의 실제 점유와 주문별 임시 할당을 함께 관리한다."""

from threading import RLock
from time import monotonic

from ..config import REAL_POSE_TIMEOUT_S


class WorkstationManager:
    def __init__(self):
        self._lock = RLock()
        self._assigned = {}  # node -> order_id

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
            robot.connected and (
                (robot.occupied_node is None and robot.occupied_edge is None)
                or robot.pose_received_at is None
                or monotonic() - robot.pose_received_at > REAL_POSE_TIMEOUT_S
            )
            for robot in rows
        )
        return occupied, unknown

    def choose(self, order_id, mode):
        with self._lock:
            occupied, unknown = self._occupied(mode)
            if unknown:
                return None
            for node in ("3", "4"):
                if node not in occupied and self._assigned.get(node) in (None, order_id):
                    self._assigned[node] = order_id
                    return node
            return None

    def release(self, order_id):
        with self._lock:
            for node, assigned_order in list(self._assigned.items()):
                if assigned_order == order_id:
                    del self._assigned[node]

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


workstation_manager = WorkstationManager()
