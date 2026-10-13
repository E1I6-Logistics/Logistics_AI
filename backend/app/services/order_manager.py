"""공통 주문 대기열, 배차, Real 작업 단계."""

import asyncio
from threading import RLock
from uuid import uuid4

from ..models.robot import RobotState
from ..ros2.ros_gateway import CHARGING_STATION_NODES, NODE_OMX_MAP, ros_gateway
from ..schemas.robot import normalize_robot_id
from .fleet_manager import fleet_manager
from .mode_service import mode_manager
from .mqtt_manager import mqtt_manager
from .order_dispatcher import available_robots
from .order_route_dispatcher import dispatch_order_route
from .scenario_service import scenario_manager
from .workstation_manager import workstation_manager


class OrderManager:
    def __init__(self):
        self.lock = RLock()
        self.orders = {}
        self.returning = {}  # robot_id -> completed order_id
        self._return_started = set()
        self._route_tasks = {}

    @staticmethod
    def get_pickup_nodes(items):
        nodes = []
        if items.get("A", 0) or items.get("B", 0):
            nodes.append("5")
        if items.get("C", 0) or items.get("D", 0):
            nodes.append("6")
        return nodes

    def create_order(self, items, total_quantity, order_name=None, driving_mode="standard",
                     selector=None, model=None):
        scenario_manager.ensure_available()
        if set(items) != {"A", "B", "C", "D"} or any(
            not isinstance(value, int) or value < 0 for value in items.values()
        ):
            raise ValueError("A/B/C/D 수량을 0 이상의 정수로 입력하세요.")
        if sum(items.values()) != total_quantity or not 1 <= total_quantity <= 5:
            raise ValueError("총 수량은 품목 합계와 같고 1~5개여야 합니다.")
        if driving_mode not in ("standard", "llm"):
            raise ValueError("지원하지 않는 Planning Mode입니다.")
        if driving_mode == "llm":
            from .route_model_catalog import require_enabled_model
            if not selector or not model:
                raise ValueError("AI 경로 모델을 선택하세요.")
            require_enabled_model(selector, model)
        with scenario_manager.lock, self.lock:
            scenario_manager.ensure_available()
            order_id = str(uuid4())
            row = {
                "order_id": order_id,
                "order_name": order_name or f"ORDER-{len(self.orders) + 1:03d}",
                "items": dict(items), "total_quantity": total_quantity,
                "pickup_nodes": self.get_pickup_nodes(items),
                "pickup_index": 0, "workstation_node": None,
                "robot_id": None, "status": "PENDING", "phase": "PENDING",
                "mode": mode_manager.mode, "driving_mode": driving_mode,
                "selector": selector, "model": model, "route_started": False,
            }
            self.orders[order_id] = row
            return self.public_order(row)

    @staticmethod
    def public_order(row):
        return {key: value for key, value in row.items() if key != "route_started"}

    def list_orders(self):
        with self.lock:
            return [self.public_order(row) for row in self.orders.values()]

    def get_order(self, order_id):
        with self.lock:
            row = self.orders.get(order_id)
            return self.public_order(row) if row else None

    def is_current(self, order_id, robot_id, phase=None):
        with self.lock:
            row = self.orders.get(order_id)
            return bool(row and row["robot_id"] == robot_id
                        and row["status"] == "IN_PROGRESS"
                        and (phase is None or row["phase"] == phase)
                        and mode_manager.mode == row["mode"])

    def set_phase(self, order_id, phase, expected=None):
        with self.lock:
            row = self.orders.get(order_id)
            if (row and row["status"] == "IN_PROGRESS"
                    and (expected is None or row["phase"] == expected)):
                row["phase"] = phase

    def cancel_order(self, order_id):
        with self.lock:
            row = self.orders.get(order_id)
            if row is None or row["status"] in ("COMPLETED", "CANCELLED"):
                return False
            row["status"] = row["phase"] = "CANCELLED"
            workstation_manager.release(order_id)
            robot_id, mode = row["robot_id"], row["mode"]
        if robot_id:
            if mode == "simulation":
                from .mock_data import mock_fms
                mock_fms.stop_robot(robot_id)
            else:
                robot = fleet_manager.get_robot(robot_id)
                if robot and robot.state != RobotState.EMERGENCY_STOP and robot.connected:
                    ros_gateway.cancel_current_work(robot_id, lambda: None)
                if robot and robot.order_id == order_id:
                    robot.clear_order()
        return True

    def cancel_robot(self, robot_id, mode):
        robot_id = normalize_robot_id(robot_id)
        with self.lock:
            completed_id = self.returning.pop(robot_id, None)
            self._return_started.discard(robot_id)
            if completed_id in self.orders:
                self.orders[completed_id]["phase"] = "COMPLETED"
            ids = [row["order_id"] for row in self.orders.values()
                   if row["robot_id"] == robot_id and row["mode"] == mode
                   and row["status"] == "IN_PROGRESS"]
        for order_id in ids:
            self.cancel_order(order_id)

    async def tick(self):
        if scenario_manager.active:
            return
        mode = mode_manager.mode
        preemptions = []
        with self.lock:
            busy = {row["robot_id"] for row in self.orders.values()
                    if row["status"] == "IN_PROGRESS" and row["robot_id"]}
            candidates = available_robots(mode, busy, set(self.returning))
            for row in self.orders.values():
                if row["mode"] != mode or row["status"] != "PENDING" or not candidates:
                    continue
                blocked_by_earlier_pickup = False
                if mode == "simulation":
                    for previous in self.orders.values():
                        if previous is row:
                            break
                        if (previous["mode"] == mode and previous["status"] == "IN_PROGRESS"
                                and set(previous["pickup_nodes"]).intersection(row["pickup_nodes"])):
                            blocked_by_earlier_pickup = True
                            break
                candidate_index = None
                for index, candidate in enumerate(candidates):
                    # A returning robot must leave the workstation before
                    # waiting behind an earlier order at the same pickup.
                    if blocked_by_earlier_pickup and candidate in self.returning:
                        continue
                    candidate_index = index
                    break
                if candidate_index is None:
                    continue
                robot_id = candidates.pop(candidate_index)
                row["robot_id"] = robot_id
                row["status"] = "IN_PROGRESS"
                returning = robot_id in self.returning
                row["phase"] = ("WAITING_RETURN_STOP" if mode == "real" and returning
                                else "MOVING_TO_PICKUP")
                row["route_started"] = False
                busy.add(robot_id)
                previous_order_id = self.returning.pop(robot_id, None)
                if previous_order_id in self.orders:
                    self.orders[previous_order_id]["phase"] = "COMPLETED"
                self._return_started.discard(robot_id)
                if mode == "simulation":
                    from .mock_data import mock_fms
                    snapshot = mock_fms.robot_snapshot(robot_id, mode)
                    if snapshot["goal_node"] is not None:
                        mock_fms.stop_robot(robot_id)
                    from .simulation_order_runner import simulation_order_runner
                    simulation_order_runner.start(row["order_id"])
                else:
                    if returning:
                        preemptions.append((row["order_id"], robot_id))
                    else:
                        robot = fleet_manager.get_robot(robot_id)
                        robot.assign_order(row["order_id"], row["items"], row["total_quantity"],
                                           row["pickup_nodes"], None)
            active = [row for row in self.orders.values()
                      if row["mode"] == mode and row["status"] == "IN_PROGRESS"]
        if mode == "real":
            for order_id, robot_id in preemptions:
                try:
                    ros_gateway.cancel_current_work(
                        robot_id,
                        lambda oid=order_id, rid=robot_id: self._start_after_return_stop(oid, rid),
                    )
                except (ValueError, RuntimeError) as exc:
                    self.fail(order_id, str(exc))
            for row in active:
                await self._tick_real(row)
            await self._tick_real_returns()

    def _start_after_return_stop(self, order_id, robot_id):
        with self.lock:
            if not self.is_current(order_id, robot_id, "WAITING_RETURN_STOP"):
                return
            robot = fleet_manager.get_robot(robot_id)
            if robot is None or not robot.connected or robot.occupied_node is None:
                self.fail(order_id, "복귀 정지 후 로봇 위치를 확인할 수 없습니다.")
                return
            row = self.orders[order_id]
            robot.assign_order(order_id, row["items"], row["total_quantity"],
                               row["pickup_nodes"], None)
            row["phase"] = "MOVING_TO_PICKUP"

    async def _tick_real(self, row):
        order_id = row["order_id"]
        robot = fleet_manager.get_robot(row["robot_id"])
        if robot is None or not robot.connected or robot.state in (RobotState.PAUSED, RobotState.EMERGENCY_STOP):
            return
        phase = row["phase"]
        if phase == "WAITING_WORKSTATION":
            node = workstation_manager.choose(order_id, "real")
            if node is None:
                return
            with self.lock:
                if not self.is_current(order_id, robot.robot_id, phase):
                    workstation_manager.release(order_id)
                    return
                row["workstation_node"] = robot.order_workstation_node = node
                row["phase"] = "MOVING_TO_WORKSTATION"
                row["route_started"] = False
            phase = "MOVING_TO_WORKSTATION"
        if phase not in ("MOVING_TO_PICKUP", "MOVING_TO_WORKSTATION"):
            return
        target = (row["pickup_nodes"][row["pickup_index"]] if phase == "MOVING_TO_PICKUP"
                  else row["workstation_node"])
        if robot.current_node == target and robot.state == RobotState.WAITING:
            self.set_phase(order_id, "WORKING_AT_PICKUP" if phase == "MOVING_TO_PICKUP"
                           else "WORKING_AT_WORKSTATION", expected=phase)
            return
        with self.lock:
            if (row["phase"] == phase and not row["route_started"]
                    and order_id not in self._route_tasks):
                row["route_started"] = True
                self._route_tasks[order_id] = asyncio.create_task(
                    self._move_real(row, target, phase)
                )

    async def _move_real(self, row, target, phase):
        order_id = row["order_id"]
        try:
            robot = fleet_manager.get_robot(row["robot_id"])
            start = robot.occupied_node if robot else None
            if not self.is_current(order_id, row["robot_id"], phase):
                return
            if start is None:
                raise ValueError("실제 로봇의 출발 노드를 확인할 수 없습니다.")
            await dispatch_order_route(
                row, target, start,
                valid=lambda: self.is_current(order_id, row["robot_id"], phase),
            )
        except Exception as exc:
            if self.is_current(order_id, row["robot_id"], phase):
                self.fail(order_id, str(exc))
        finally:
            self._route_tasks.pop(order_id, None)

    async def _tick_real_returns(self):
        for robot_id in list(self.returning):
            robot = fleet_manager.get_robot(robot_id)
            station = CHARGING_STATION_NODES.get(robot_id)
            if robot is None or not robot.connected or station is None:
                continue
            if (robot.occupied_node == station and robot.state == RobotState.IDLE
                    and not ros_gateway.navigation_active(robot_id)):
                completed_id = self.returning.pop(robot_id, None)
                if completed_id in self.orders:
                    self.orders[completed_id]["phase"] = "COMPLETED"
                self._return_started.discard(robot_id)
                robot.returning = False
                continue
            key = f"return:{robot_id}"
            if robot_id not in self._return_started and key not in self._route_tasks:
                self._return_started.add(robot_id)
                self._route_tasks[key] = asyncio.create_task(self._return_real(robot_id, station, key))

    async def _return_real(self, robot_id, station, key):
        order_id = self.returning.get(robot_id)
        if order_id is None:
            self._route_tasks.pop(key, None)
            return
        try:
            row = self.orders[order_id]
            robot = fleet_manager.get_robot(robot_id)
            if robot is None or robot.occupied_node is None:
                raise ValueError("복귀 출발 노드를 확인할 수 없습니다.")
            await dispatch_order_route(
                row, station, robot.occupied_node,
                valid=lambda: self.returning.get(robot_id) == order_id,
            )
        except Exception as exc:
            self.returning.pop(robot_id, None)
            self._return_started.discard(robot_id)
            self.orders[order_id]["phase"] = "COMPLETED"
            self.orders[order_id]["return_error"] = str(exc)
        finally:
            self._route_tasks.pop(key, None)

    def fail(self, order_id, reason):
        with self.lock:
            row = self.orders.get(order_id)
            if row is None or row["status"] != "IN_PROGRESS":
                return
            row["status"] = row["phase"] = "FAILED"
            row["error"] = reason
            workstation_manager.release(order_id)
            if row["mode"] == "simulation":
                from .mock_data import mock_fms
                # A failed order no longer owns this robot. Stop its route
                # before a later queue tick can assign another order.
                mock_fms.stop_robot(row["robot_id"])
            else:
                robot = fleet_manager.get_robot(row["robot_id"])
                if robot:
                    robot.pause("next_destination_failed", reason)

    def on_mqtt_result(self, omx, data):
        # MQTT 콜백은 단계만 변경한다. 다음 경로는 FastAPI 루프에서 시작한다.
        with ros_gateway._navigation.lock, self.lock:
            order_id = data.get("job_id")
            row = self.orders.get(order_id)
            if not row or row["mode"] != "real" or row["status"] != "IN_PROGRESS":
                return
            robot = fleet_manager.get_robot(row["robot_id"])
            if (robot is None or robot.order_id != order_id or robot.state != RobotState.WAITING
                    or robot.current_node is None or NODE_OMX_MAP.get(robot.current_node) != omx.omx_id):
                return
            if row["phase"] in ("WORKING_AT_PICKUP", "MOVING_TO_PICKUP"):
                if robot.current_node != row["pickup_nodes"][row["pickup_index"]]:
                    return
            elif row["phase"] in ("WORKING_AT_WORKSTATION", "MOVING_TO_WORKSTATION"):
                if robot.current_node != row["workstation_node"]:
                    return
            else:
                return
            if not data.get("success", False):
                self.fail(order_id, f"OMX 작업 실패: {omx.omx_id}")
                return
            if row["phase"] in ("WORKING_AT_PICKUP", "MOVING_TO_PICKUP"):
                row["pickup_index"] += 1
                robot.order_pickup_index = row["pickup_index"]
                if row["pickup_index"] < len(row["pickup_nodes"]):
                    row["phase"] = "MOVING_TO_PICKUP"
                    row["route_started"] = False
                else:
                    row["phase"] = "WAITING_WORKSTATION"
            else:
                row["status"] = "COMPLETED"
                row["phase"] = "RETURNING"
                workstation_manager.release(order_id)
                robot.clear_order()
                robot.returning = True
                self.returning[robot.robot_id] = order_id


order_manager = OrderManager()
mqtt_manager.set_result_callback(order_manager.on_mqtt_result)
