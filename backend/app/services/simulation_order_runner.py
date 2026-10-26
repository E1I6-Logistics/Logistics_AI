"""기존 MockFmsStore 주행을 이용하는 Simulation 주문 실행기."""

import asyncio
import os
from time import monotonic

from ..ros2.ros_gateway import CHARGING_STATION_NODES
from .mode_service import mode_manager
from .mock_data import mock_fms
from .order_route_dispatcher import dispatch_order_route
from .workstation_manager import workstation_manager


class SimulationOrderRunner:
    # SimulationOrderRunner의 초기 상태를 설정한다.
    def __init__(self):
        self.work_seconds = float(os.getenv("FMS_SIMULATION_WORK_SECONDS", "1.5"))
        self.tasks = {}

    # 관련을 시작한다.
    def start(self, order_id):
        if order_id not in self.tasks:
            task = asyncio.create_task(self.run(order_id), name=f"simulation-order-{order_id}")
            self.tasks[order_id] = task
            task.add_done_callback(lambda _task: self.tasks.pop(order_id, None))

    # 복구를 시작한다.
    def start_recovery(self, order_id, robot_id):
        key = f"recovery:{robot_id}"
        if key not in self.tasks:
            task = asyncio.create_task(self.recover(order_id, robot_id))
            self.tasks[key] = task
            task.add_done_callback(lambda _task: self.tasks.pop(key, None))

    # 실패한 주문의 로봇을 복귀시킨다.
    async def recover(self, order_id, robot_id):
        from .order_manager import order_manager

        order = order_manager.orders[order_id]

        # 현재 작업이 유효한지 확인한다.
        def valid():
            return mode_manager.mode == "simulation" and order_manager.recovering.get(robot_id) == order_id

        try:
            if not valid():
                return
            station = CHARGING_STATION_NODES.get(robot_id)
            if station is None:
                raise ValueError("담당 스테이션이 없습니다.")
            snapshot = mock_fms.robot_snapshot(robot_id, "simulation")
            if snapshot["connection_state"] != "ONLINE":
                raise ValueError("연결이 끊겨 복귀할 수 없습니다.")
            # Failure recovery is an explicit safety return using the existing
            # Algorithm planner, even when the failed order used an AI model.
            recovery = dict(order, operation_mode="algorithm", driving_mode="standard", robot_id=robot_id)
            order["recovery_status"] = "RETURNING"
            await dispatch_order_route(recovery, station, snapshot["occupied_node"], valid=valid)
            started = monotonic()
            while valid():
                snapshot = mock_fms.robot_snapshot(robot_id, "simulation")
                if snapshot["connection_state"] != "ONLINE" or snapshot.get("stop_requested"):
                    raise ValueError("복귀 중 연결 또는 정지 상태가 변경되었습니다.")
                if snapshot["occupied_node"] == station and snapshot["goal_node"] is None and snapshot["route"] is None:
                    order["recovery_status"] = "RETURNED"
                    return
                if monotonic() - started > 180:
                    raise ValueError("실패 후 복귀 제한 시간(180초)을 초과했습니다.")
                if snapshot["status"] == "IDLE" and snapshot["route"] is None and snapshot["goal_node"] is None:
                    await dispatch_order_route(recovery, station, snapshot["occupied_node"], valid=valid)
                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            if valid():
                mock_fms.stop_robot(robot_id)
                order["recovery_status"] = "STOPPED"
            raise
        except Exception as exc:
            if valid():
                mock_fms.stop_robot(robot_id)
                order["recovery_status"] = "BLOCKED"
                order["recovery_error"] = str(exc)
                print(
                    f"[ORDER RECOVERY FAILED] name={order['order_name']} " f"robot={robot_id} reason={exc}", flush=True
                )
        finally:
            if order_manager.recovering.get(robot_id) == order_id:
                order_manager.recovering.pop(robot_id, None)

    # 관련을 종료한다.
    async def shutdown(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _wait_for_pickup_turn(self, order):
        """같은 픽업의 현재 작업·실제 점유만 기다린다. 전체 주문 완료는 기다리지 않는다."""
        from .order_manager import order_manager

        order_id = order["order_id"]
        robot_id = order["robot_id"]
        pickup = order["pickup_nodes"][order["pickup_index"]]
        while order_manager.is_current(order_id, robot_id):
            blocked = False
            with order_manager.lock:
                for previous in order_manager.orders.values():
                    if previous["order_id"] == order_id:
                        break
                    if (
                        previous["mode"] == "simulation"
                        and previous["status"] == "IN_PROGRESS"
                        and previous["phase"] in ("WAITING_PICKUP", "MOVING_TO_PICKUP", "WORKING_AT_PICKUP")
                        and previous["pickup_nodes"][previous["pickup_index"]] == pickup
                    ):
                        blocked = True
                        break
            if not blocked:
                blocked = any(
                    r["robot_id"] != robot_id and r.get("occupied_node") == pickup
                    for r in mock_fms.robot_snapshots("simulation")
                )
            if not blocked:
                return True
            order_manager.set_phase(order_id, "WAITING_PICKUP")
            await asyncio.sleep(0.1)
        return False

    # 모의 로봇을 다음 목적지로 이동시킨다.
    async def _move(self, order, node, phase):
        from .order_manager import order_manager

        order_id, robot_id = order["order_id"], order["robot_id"]
        if not order_manager.is_current(order_id, robot_id, phase):
            return False
        snapshot = mock_fms.robot_snapshot(robot_id, "simulation")
        start = snapshot["occupied_node"]
        if (
            snapshot["goal_node"] is not None
            or snapshot["route"] is not None
            or (start is None and order["driving_mode"] == "llm")
        ):
            raise ValueError("로봇의 출발 노드를 확인할 수 없습니다.")
        await dispatch_order_route(
            order, node, start, valid=lambda: order_manager.is_current(order_id, robot_id, phase)
        )
        order["_held_node"] = None
        retry_count = 0
        while order_manager.is_current(order_id, robot_id, phase):
            snapshot = mock_fms.robot_snapshot(robot_id, "simulation")
            if snapshot["connection_state"] != "ONLINE" or snapshot.get("stop_requested"):
                raise ValueError("로봇 연결 또는 정지 상태가 변경되었습니다.")
            if snapshot["occupied_node"] == node and snapshot["goal_node"] is None:
                # An arrived robot must not be treated as a parked robot that
                # can yield while its pickup/OMX timer or AI request is active.
                mock_fms.stop_robot(robot_id)
                order["_held_node"] = node
                return True
            # TrafficManager can finish a yield at an intermediate node.
            # Request the same destination again from that confirmed node.
            if snapshot["goal_node"] is None and snapshot["route"] is None:
                if snapshot["status"] == "IDLE" and (
                    snapshot["occupied_node"] is not None or snapshot["occupied_edge"] is not None
                ):
                    retry_count += 1
                    if retry_count > 10:
                        raise ValueError("목적지 재요청 횟수를 초과했습니다.")
                    await dispatch_order_route(
                        order,
                        node,
                        snapshot["occupied_node"],
                        valid=lambda: order_manager.is_current(order_id, robot_id, phase),
                    )
                elif snapshot["status"] != "WAITING":
                    raise ValueError("목적지에 도착하기 전에 주행이 종료되었습니다.")
            await asyncio.sleep(0.1)
        return False

    # 픽업 또는 작업대에서 모의 작업을 수행한다.
    async def _work(self, order, phase):
        from .order_manager import order_manager

        await asyncio.sleep(self.work_seconds)
        if not order_manager.is_current(order["order_id"], order["robot_id"], phase):
            return False
        snapshot = mock_fms.robot_snapshot(order["robot_id"], "simulation")
        if (
            snapshot.get("connection_state") != "ONLINE"
            or snapshot["status"] != "IDLE"
            or order.get("_held_node") is None
            or snapshot["occupied_node"] != order.get("_held_node")
            or snapshot["route"] is not None
            or snapshot["goal_node"] is not None
        ):
            raise ValueError("모의 작업 중 연결 또는 작업 위치가 변경되었습니다.")
        return True

    # 관련을 실행한다.
    async def run(self, order_id):
        from .order_manager import order_manager

        order = order_manager.orders[order_id]
        robot_id = order["robot_id"]
        try:
            for index, pickup in enumerate(order["pickup_nodes"]):
                if not order_manager.is_current(order_id, robot_id):
                    return
                order["pickup_index"] = index
                if not await self._wait_for_pickup_turn(order):
                    return
                order_manager.set_phase(order_id, "MOVING_TO_PICKUP")
                if not await self._move(order, pickup, "MOVING_TO_PICKUP"):
                    return
                order_manager.set_phase(order_id, "WORKING_AT_PICKUP")
                if not await self._work(order, "WORKING_AT_PICKUP"):
                    return
                print(f"[ORDER SIM] robot={robot_id} pickup=N{pickup} simulated work complete", flush=True)

            order_manager.set_phase(order_id, "WAITING_WORKSTATION")
            while order_manager.is_current(order_id, robot_id, "WAITING_WORKSTATION"):
                node = workstation_manager.choose(order_id, "simulation", order["operation_mode"])
                if node is not None:
                    if not order_manager.is_current(order_id, robot_id, "WAITING_WORKSTATION"):
                        workstation_manager.release(order_id)
                        return
                    order["workstation_node"] = node
                    break
                await asyncio.sleep(0.25)
            if order["workstation_node"] is None:
                return

            order_manager.set_phase(order_id, "MOVING_TO_WORKSTATION")
            if not await self._move(order, order["workstation_node"], "MOVING_TO_WORKSTATION"):
                return
            order_manager.set_phase(order_id, "WORKING_AT_WORKSTATION")
            if not await self._work(order, "WORKING_AT_WORKSTATION"):
                return
            print(
                f"[ORDER SIM] robot={robot_id} workstation=N{order['workstation_node']} simulated work complete",
                flush=True,
            )
            with order_manager.lock:
                if not order_manager.is_current(order_id, robot_id, "WORKING_AT_WORKSTATION"):
                    return
                order["status"] = "COMPLETED"
                order["phase"] = "RETURNING"
                workstation_manager.release(order_id)
                order_manager.returning[robot_id] = order_id

            station = CHARGING_STATION_NODES.get(robot_id)
            if station is None:
                return
            # 복귀는 기존 Mock 이동과 TrafficManager 예약을 그대로 사용한다.
            snapshot = mock_fms.robot_snapshot(robot_id, "simulation")
            if snapshot["occupied_node"] is None:
                raise ValueError("복귀 출발 노드를 확인할 수 없습니다.")
            await dispatch_order_route(
                order,
                station,
                snapshot["occupied_node"],
                valid=lambda: order_manager.returning.get(robot_id) == order_id,
            )
            order["_held_node"] = None
            return_retries = 0
            while mode_manager.mode == "simulation" and order_manager.returning.get(robot_id) == order_id:
                snapshot = mock_fms.robot_snapshot(robot_id, "simulation")
                if snapshot["connection_state"] != "ONLINE" or snapshot.get("stop_requested"):
                    raise ValueError("복귀 중 로봇 연결 또는 정지 상태가 변경되었습니다.")
                if snapshot["occupied_node"] == station and snapshot["goal_node"] is None and snapshot["route"] is None:
                    order_manager.returning.pop(robot_id, None)
                    order["phase"] = "COMPLETED"
                    return
                if (
                    snapshot["goal_node"] is None
                    and snapshot["route"] is None
                    and snapshot["status"] == "IDLE"
                    and (snapshot["occupied_node"] is not None or snapshot["occupied_edge"] is not None)
                ):
                    return_retries += 1
                    if return_retries > 10:
                        raise ValueError("복귀 목적지 재요청 횟수를 초과했습니다.")
                    await dispatch_order_route(
                        order,
                        station,
                        snapshot["occupied_node"],
                        valid=lambda: order_manager.returning.get(robot_id) == order_id,
                    )
                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            workstation_manager.release(order_id)
            raise
        except Exception as exc:
            if order["status"] == "COMPLETED":
                if order_manager.returning.get(robot_id) == order_id:
                    order_manager.returning.pop(robot_id, None)
                order["phase"] = "COMPLETED"
                order["return_error"] = str(exc)
            else:
                order_manager.fail(order_id, str(exc))
        finally:
            if order["status"] != "COMPLETED":
                workstation_manager.release(order_id)


simulation_order_runner = SimulationOrderRunner()
