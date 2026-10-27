from __future__ import annotations

from typing import Any
from copy import deepcopy
from functools import wraps
from ..services.real_navigation import RealNavigation, measured_distance
from ..config import (REAL_ARRIVAL_DISTANCE_M, ZENOH_MISSING_GRACE_S, DOCK_STATION_MAP,
                      DOCK_ACTION_TIMEOUT_S, DOCK_CONTACT_TIMEOUT_S, STATION_EXIT_SPEED_MPS,
                      STATION_EXIT_TIMEOUT_S, STATION_EXIT_TOLERANCE_M, REAL_ARRIVAL_CONFIRM_TIMEOUT_S,
                      REAL_DOCK_OCCUPANCY_TOLERANCE_M)
from ..services.station_exit import forward_distance, StationExitError
from time import monotonic, strftime


from ..services.mqtt_manager import mqtt_manager
from ..models.robot import RobotState, NavigationType
from ..schemas.robot import normalize_robot_id, to_ui_robot_id
from ..services.fleet_manager import fleet_manager
from ..services.pathfinding import DistanceAStar
from ..services.route_graph import get_node, load_route_graph, find_edge_ids, locate_current_node, find_nearest_node

try:
    from action_msgs.msg import GoalStatus
except ImportError:
    GoalStatus = None
import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .fms_ros_node import FmsRosNode

ARRIVAL_DISTANCE_THRESHOLD = REAL_ARRIVAL_DISTANCE_M

GOAL_YAWS = {"0": 0.0, "1": 0.0, "2": 0.0, "3": 0.0, "4": 0.0, "5": -math.pi / 2, "6": -math.pi / 2}

CHARGING_STATION_NODES = {"robot1": "0", "robot2": "1", "robot3": "2"}

ARUCO_MARKER_IDS = {
    "6": 25,
    # "6": 28,
    "5": 24,
    # "5": 29,
    "4": 26,
    "3": 27,
}

# 작업 Node별 OMX 장비
NODE_OMX_MAP = {"5": "omx1", "6": "omx2", "3": "omx3", "4": "omx4"}

# 작업 Node별 처리 품목
NODE_ITEM_MAP = {"5": ["A", "B"], "6": ["C", "D"], "3": ["A", "B", "C", "D"], "4": ["A", "B", "C", "D"]}


# 경유점 노드를 간소화한다.
def simplify_waypoint_nodes(node_ids):
    # 노드가 2개 이하이면 줄일 필요 없음
    if len(node_ids) <= 2:
        return node_ids.copy()

    result = [node_ids[0]]

    for index in range(1, len(node_ids) - 1):
        previous = get_node(node_ids[index - 1])
        current = get_node(node_ids[index])
        next_node = get_node(node_ids[index + 1])

        # 이전 노드 -> 현재 노드 방향
        angle1 = math.atan2(current["y"] - previous["y"], current["x"] - previous["x"])

        # 현재 노드 -> 다음 노드 방향
        angle2 = math.atan2(next_node["y"] - current["y"], next_node["x"] - current["x"])

        # 두 진행 방향의 차이
        angle_diff = abs(math.atan2(math.sin(angle2 - angle1), math.cos(angle2 - angle1)))

        # 거의 직선이면 현재 노드를 Waypoint에서 제외
        if angle_diff < math.radians(5):
            continue

        # 방향이 바뀌는 노드는 유지
        result.append(node_ids[index])

    # 목적지는 항상 유지
    result.append(node_ids[-1])

    return result


# 주행 작업이 잠겨 있는지 확인한다.
def _navigation_locked(method):
    # 주행 처리에 사용할 잠금을 적용한다.
    @wraps(method)
    def locked(self, *args, **kwargs):
        with self._navigation.lock:
            return method(self, *args, **kwargs)

    return locked


class RosGateway:
    # RosGateway의 초기 상태를 설정한다.
    @property
    def navigation(self):
        return self._navigation

    def __init__(self) -> None:
        self._ros_node: FmsRosNode | None = None
        self._waiting_omx = {}
        self._missing_since = {}
        self._dock_attempts = {}
        self._charging_docks = {}  # 현재 충전 접촉/주문 배정 전 출발 문맥. 도킹 성공 이력이 아니다.
        self._departing_docks = {}
        self._consumed_contacts = {}  # 출발에 사용한 ON이 뒤늦게 CHARGING을 다시 만들지 못하게 한다.
        self._unverified_docks = set()  # 실제 도킹/이탈 실패만 보류한다. 시작 위치는 보류하지 않는다.
        self._navigation = RealNavigation(
            fleet_manager,
            lambda **kwargs: self._ros_node.send_follow_waypoints_goal(**kwargs),
            self._cancel_navigation_goal,
            lambda robot_id: self.on_navigation_result(robot_id, GoalStatus.STATUS_SUCCEEDED),
            GOAL_YAWS,
            ARRIVAL_DISTANCE_THRESHOLD,
            station_nodes=CHARGING_STATION_NODES,
            before_send=self._before_dock_navigation_send,
            departure_ready=self._dock_planning_ready,
            before_request=self._validate_dock_departure,
            on_requested=self._on_dock_navigation_requested,
            navigation_kind=self._dock_navigation_kind,
            mapf_ready=lambda rid: self._ros_node is not None and self._ros_node.is_registered(rid),
        )

    # ROS 노드를 설정한다.
    def set_ros_node(self, ros_node: FmsRosNode) -> None:
        self._ros_node = ros_node
        ros_node.navigation_lock = self._navigation.lock
        ros_node.navigation_result_callback = self._on_segment_result
        ros_node.navigation_feedback_callback = self._navigation.on_feedback
        ros_node.navigation_error_callback = self._navigation.on_execution_error
        ros_node.navigation_started_callback = self._on_dock_navigation_started
        ros_node.precision_dock_result_callback = self.on_precision_dock_result
        ros_node.station_exit_result_callback = self.on_station_exit_result
        ros_node.station_exit_started_callback = self._on_dock_navigation_started
        ros_node.aruco_align_result_callback = self.on_aruco_align_result

    # 구간 결과 이벤트를 처리한다.
    def _on_segment_result(self, robot_id: str, status: int) -> None:
        self._navigation.on_result(robot_id, status == GoalStatus.STATUS_SUCCEEDED, status=status)

    # 주행을 진행한다.
    def advance_navigation(self) -> None:
        if self._ros_node is not None:
            self._ros_node.reconcile_navigation_results()
        self._tick_charging_docks()
        self._navigation.tick()
        self._tick_charging_docks()

    def _validate_dock_departure(self, robot_id):
        self._sync_dock_contact(robot_id)
        robot = fleet_manager.get_robot(robot_id)
        if robot is None:
            raise ValueError(f"Robot을 찾을 수 없습니다: {robot_id}")
        if robot_id in self._unverified_docks:
            raise ValueError("도킹 또는 이탈 실패: 정지 확인 후 충전소 복귀 명령으로 복구하세요.")
        if robot_id in self._departing_docks:
            if not robot.connected or robot.state == RobotState.EMERGENCY_STOP:
                raise ValueError("도킹 로봇 연결 또는 비상정지 상태를 확인하세요.")
            return  # 출발을 승인한 뒤에는 MQTT 해제를 일반 주행의 통과 조건으로 쓰지 않는다.
        if robot_id not in self._charging_docks and robot.state != RobotState.CHARGING:
            return
        if robot.state == RobotState.CHARGING:
            contact = self._live_dock_contact(robot_id)
            if contact is None:
                raise ValueError(f"충전소 접촉 상태를 확인할 수 없습니다: {robot_id}")
            self._charging_docks[robot_id] = contact
        if robot is None or not robot.connected or robot.state == RobotState.EMERGENCY_STOP:
            raise ValueError("도킹 로봇 연결 또는 비상정지 상태를 확인하세요.")
        if robot.battery is None or not math.isfinite(robot.battery) or not 0 < robot.battery <= 100:
            raise ValueError(f"출발 가능한 배터리 상태를 확인할 수 없습니다: {robot_id}")

    @_navigation_locked
    def dock_dispatch_allowed(self, robot_id):
        try:
            self._validate_dock_departure(robot_id)
            return True
        except ValueError:
            return False

    @_navigation_locked
    def charging_dispatch_ready(self, robot_id):
        robot = fleet_manager.get_robot(robot_id)
        if robot is None or not robot.connected or robot.state != RobotState.CHARGING:
            return False
        try:
            self._validate_dock_departure(robot_id)
        except ValueError:
            return False
        return bool(robot.occupied_node == CHARGING_STATION_NODES.get(robot_id)
                    and robot.occupied_edge is None and robot.goal_node is None
                    and robot.route is None and robot.order_id is None
                    and not self.navigation_active(robot_id)
                    and self._ros_node is not None and self._ros_node.is_registered(robot_id))

    @_navigation_locked
    def docking_station_snapshots(self):
        rows = []
        for station in (1, 2, 3):
            sample = mqtt_manager.get_dock_snapshot(station)
            robot_id = next((rid for rid, sid in DOCK_STATION_MAP.items() if sid == station), None)
            robot = fleet_manager.get_robot(robot_id) if robot_id else None
            rows.append({
                "station": station, "contact": sample["contact"],
                "received_at_unix": sample.get("received_at_unix"),
                "age_s": sample["age_s"], "stable": sample["stable"],
                "broker_connected": sample["broker_connected"],
                "freshness_policy": sample["freshness_policy"],
                "robot_id": robot_id,
                "docking_complete": bool(robot and robot.connected and robot.state == RobotState.CHARGING
                                         and robot_id in self._charging_docks and sample["stable"]
                                         and sample.get("status") == 1
                                         and sample["generation"] == self._charging_docks[robot_id]["generation"]),
            })
        return rows

    @_navigation_locked
    def _tick_charging_docks(self):
        # 기존 FMS 루프에서만 상태 전환한다. MQTT 수신 스레드는 센서만 저장한다.
        now = monotonic()
        for robot_id, attempt in list(self._dock_attempts.items()):
            robot = fleet_manager.get_robot(robot_id)
            if (robot is None or not robot.connected or robot.state != RobotState.DOCKING
                    or robot.navigation_type != NavigationType.CHARGING):
                self._dock_attempts.pop(robot_id, None)
                self._unverified_docks.add(robot_id)
                if self._ros_node.has_active_auxiliary(robot_id):
                    self._ros_node.cancel_auxiliary(robot_id)
                continue
            sample = mqtt_manager.get_dock_snapshot(attempt["station"], now=now)
            reason = None
            if not sample["broker_connected"] or sample["generation"] != attempt["generation"]:
                reason = "dock_sensor_unavailable"
            elif attempt["succeeded_at"] is None:
                if now - attempt["started_at"] >= DOCK_ACTION_TIMEOUT_S:
                    reason = "dock_action_timeout"
            elif now - attempt["succeeded_at"] >= DOCK_CONTACT_TIMEOUT_S:
                reason = "dock_contact_timeout"
            elif mqtt_manager.dock_contact_valid(
                attempt["station"], started_at=attempt["started_at"], generation=attempt["generation"], now=now
            ):
                contact = self._live_dock_contact(robot_id)
                if contact is None:
                    continue
                robot.current_node = robot.goal_node
                robot.navigation_type = None
                robot.route = None
                robot.goal_node = None
                self._charging_docks[robot_id] = contact
                robot.docked_station = attempt["station"]
                self._dock_attempts.pop(robot_id, None)
                self._unverified_docks.discard(robot_id)
                robot.set_state(RobotState.CHARGING)
                continue
            if reason is not None:
                self._dock_attempts.pop(robot_id, None)
                self._unverified_docks.add(robot_id)
                robot.pause(reason)
                if self._ros_node.has_active_auxiliary(robot_id):
                    self._ros_node.cancel_auxiliary(robot_id)

        for robot in fleet_manager.get_all_robots():
            self._sync_dock_contact(robot.robot_id)

        for robot_id, departure in list(self._departing_docks.items()):
            robot = fleet_manager.get_robot(robot_id)
            if departure["stop_requested"]:
                continue
            if robot is None:
                continue
            invalid = (not robot.connected
                       or robot.state in (RobotState.PAUSED, RobotState.EMERGENCY_STOP))
            if departure.get("sent_at") is not None:
                invalid = invalid or now - departure["sent_at"] >= STATION_EXIT_TIMEOUT_S
                try:
                    measured_distance(robot, departure["target"], now)
                except ValueError:
                    invalid = True
            if invalid:
                self._fail_station_exit(robot_id, "station_exit_failed")
                continue
            if departure.get("action_done_at") is not None:
                distance = measured_distance(robot, departure["target"], now)
                if (distance <= STATION_EXIT_TOLERANCE_M
                        and robot.pose_received_at >= departure["action_done_at"]):
                    self._departing_docks.pop(robot_id, None)
                    robot.docked_station = None
                    self._navigation.finish_station_exit(robot_id)
                    print(f"[STATION EXIT] robot={robot_id} completed distance_to_node={distance:.3f}m", flush=True)
                elif now - departure["action_done_at"] >= REAL_ARRIVAL_CONFIRM_TIMEOUT_S:
                    self._fail_station_exit(robot_id, "station_exit_failed")

    def _live_dock_contact(self, robot_id):
        station = DOCK_STATION_MAP.get(robot_id)
        if station is None:
            return None
        sample = mqtt_manager.get_dock_snapshot(station)
        if sample["trusted"] and sample.get("status") == 1 and sample["stable"]:
            return {"station": station, "generation": sample["generation"],
                    "contact_since": sample["contact_since"]}
        return None

    def _sync_dock_contact(self, robot_id):
        """정지 중인 담당 로봇의 접촉을 동기화한다. 센서만으로 이동/작업/정지를 덮어쓰지 않는다."""
        robot = fleet_manager.get_robot(robot_id)
        if robot is None or not robot.connected:
            return
        station = DOCK_STATION_MAP.get(robot_id)
        if station is None:
            return
        sample = mqtt_manager.get_dock_snapshot(station)
        if sample["released_stable"]:
            self._consumed_contacts.pop(robot_id, None)
        if (robot_id in self._departing_docks or robot_id in self._dock_attempts
                or robot_id in self._unverified_docks
                or robot.state not in (RobotState.IDLE, RobotState.INITIALIZING, RobotState.CHARGING)
                or robot.order_id is not None or robot.navigation_type is not None
                or robot.goal_node is not None or robot.route is not None
                or self.navigation_active(robot_id)):
            return
        if sample["released_stable"]:
            self._charging_docks.pop(robot_id, None)
            robot.docked_station = None
            if robot.state == RobotState.CHARGING:
                robot.set_state(RobotState.IDLE)
            return
        contact = self._live_dock_contact(robot_id)
        if contact is None or robot.occupied_node != CHARGING_STATION_NODES.get(robot_id):
            return
        token = (contact["generation"], contact["contact_since"])
        if self._consumed_contacts.get(robot_id) == token:
            return
        try:
            node = get_node(CHARGING_STATION_NODES[robot_id])
            if measured_distance(robot, (node["x"], node["y"]), monotonic()) > REAL_DOCK_OCCUPANCY_TOLERANCE_M:
                return
        except (OSError, KeyError, ValueError):
            return
        self._charging_docks[robot_id] = contact
        robot.docked_station = station
        if robot.state != RobotState.CHARGING:
            robot.set_state(RobotState.CHARGING)

    def _finish_dock_stop(self, robot_id):
        self._navigation.cancel_after_stop(robot_id)
        self._departing_docks.pop(robot_id, None)

    def _dock_planning_ready(self, robot_id):
        if robot_id not in self._charging_docks:
            return False
        robot = fleet_manager.get_robot(robot_id)
        if robot is None or robot.state not in (RobotState.CHARGING, RobotState.TASK_ASSIGNED, RobotState.WAITING):
            return False
        try:
            self._validate_dock_departure(robot_id)
            return True
        except ValueError:
            return False

    def _on_dock_navigation_requested(self, robot_id):
        if robot_id in self._departing_docks:
            if robot_id in self._navigation._robots:
                self._navigation.protect_station_exit(robot_id)
            return
        contact = self._charging_docks.pop(robot_id, None)
        if contact is not None:
            self._consumed_contacts[robot_id] = (contact["generation"], contact["contact_since"])
            self._departing_docks[robot_id] = dict(contact, moving_at=None, stop_requested=False)
            fleet_manager.get_robot(robot_id).docked_station = None
        if robot_id in self._departing_docks and robot_id in self._navigation._robots:
            # 같은 MAPF 일괄 요청의 다른 로봇도 이탈 전 앞 노드가 비워진다고 가정하지 못하게 한다.
            self._navigation.protect_station_exit(robot_id)

    def _dock_navigation_kind(self, robot_id, goal, default):
        # 주문 자동 복귀는 일반 노드 이동이다. 충전 목적은 명시적인 요청만 전달한다.
        return default

    def _before_dock_navigation_send(self, robot_id):
        departure = self._departing_docks.get(robot_id)
        if departure is None:
            return
        if departure["stop_requested"]:
            raise StationExitError("직진 이탈 정지가 완료되지 않았습니다.")
        if departure.get("sent_at") is not None:
            return False
        robot = fleet_manager.get_robot(robot_id)
        node = get_node(CHARGING_STATION_NODES[robot_id])
        target = (node["x"], node["y"])
        distance = forward_distance(robot, target, monotonic())
        if distance == 0:
            self._departing_docks.pop(robot_id, None)
            self._navigation.clear_station_exit_protection(robot_id)
            return
        if distance / STATION_EXIT_SPEED_MPS >= STATION_EXIT_TIMEOUT_S:
            raise StationExitError("이탈 거리와 속도에 비해 제한 시간이 짧습니다.")
        departure.update(target=target, sent_at=monotonic(), action_done_at=None)
        self._navigation.begin_station_exit(robot_id)
        try:
            self._ros_node.send_station_exit(robot_id, distance, STATION_EXIT_SPEED_MPS, STATION_EXIT_TIMEOUT_S)
        except (ValueError, RuntimeError):
            self._fail_station_exit(robot_id, "station_exit_failed")
        else:
            print(f"[STATION EXIT] robot={robot_id} target={target} distance={distance:.3f}m "
                  f"speed={STATION_EXIT_SPEED_MPS:.3f}m/s", flush=True)
        return False

    @_navigation_locked
    def _on_dock_navigation_started(self, robot_id):
        departure = self._departing_docks.get(robot_id)
        if departure is not None and departure["moving_at"] is None:
            departure["moving_at"] = monotonic()

    @_navigation_locked
    def on_station_exit_result(self, robot_id, status):
        departure = self._departing_docks.get(robot_id)
        if departure is None or departure["stop_requested"] or departure.get("sent_at") is None:
            return
        if status != GoalStatus.STATUS_SUCCEEDED:
            self._fail_station_exit(robot_id, "station_exit_failed")
            return
        departure["action_done_at"] = monotonic()
        self._tick_charging_docks()

    def _fail_station_exit(self, robot_id, reason):
        departure = self._departing_docks[robot_id]
        departure["stop_requested"] = True
        self._unverified_docks.add(robot_id)
        robot = fleet_manager.get_robot(robot_id)
        if robot.state != RobotState.EMERGENCY_STOP:
            robot.pause(reason)
        self._navigation.stop(robot_id, lambda: self._finish_dock_stop(robot_id), reason=reason)

    def _cancel_navigation_goal(self, robot_id, callback):
        """직진 Action도 terminal result가 확인되어야 기존 Nav2 정지가 완료된다."""
        def stopped_auxiliary():
            departure = self._departing_docks.pop(robot_id, None)
            if departure is not None:
                if departure.get("sent_at") is None:
                    self._charging_docks[robot_id] = departure
                    self._consumed_contacts.pop(robot_id, None)
                else:
                    self._unverified_docks.add(robot_id)
            self._ros_node.cancel_follow_waypoints(robot_id, callback=callback)
        if self._ros_node.has_active_auxiliary(robot_id):
            self._ros_node.cancel_auxiliary(robot_id, callback=stopped_auxiliary)
        else:
            stopped_auxiliary()

    # 연결 로봇을 동기화한다.
    @_navigation_locked
    def sync_connected_robots(self, connections: list[dict]) -> None:

        if self._ros_node is None:
            raise RuntimeError("FMS ROS node is not initialized")

        # 이번 조회에서 확인된 로봇 이름을 모은다.
        connected_robot_ids = []

        for connection in connections:
            if not connection.get("connected"):
                continue

            robot_id = str(connection.get("name", "")).strip()
            if not robot_id:
                continue

            connected_robot_ids.append(robot_id)
            self._missing_since.pop(robot_id, None)
            # 처음 본 로봇은 등록하고, 이미 있던 로봇은 연결 상태를 갱신한다.
            fleet_manager.register_robot(robot_id)

            # 이 로봇의 ROS 통신 준비가 안 됐다면 준비한다.
            self._ros_node.register_robot(robot_id)

        # 한 번 보이지 않았다는 이유로 바로 연결이 끊겼다고 판단하지 않는다.
        for robot in fleet_manager.get_all_robots():

            if robot.robot_id not in connected_robot_ids:
                now = monotonic()
                first_missing = self._missing_since.get(robot.robot_id)
                if first_missing is None:
                    first_missing = now
                    self._missing_since[robot.robot_id] = now
                missing_seconds = now - first_missing
                if missing_seconds < ZENOH_MISSING_GRACE_S:
                    continue
                if robot.connected:
                    print(
                        f"[{strftime('%Y-%m-%d %H:%M:%S')}] [WARN] [ROS CONNECTION SYNC] "
                        f"robot={robot.robot_id} 연결 목록에서 {missing_seconds:.1f}초 동안 보이지 않음 "
                        f"seen={connected_robot_ids} order_id={robot.order_id}; OFFLINE 처리",
                        flush=True,
                    )
                fleet_manager.disconnect_robot(robot.robot_id)

    # 로봇 ID를 정규화하고 ROS 노드에 등록 여부를 확인
    # 로봇 ID를 해결한다.
    def _resolve_robot_id(self, robot_id: str) -> str:
        robot_id = normalize_robot_id(robot_id)

        if self._ros_node is None:
            raise RuntimeError("ROS node is not initialized")

        if not self._ros_node.is_registered(robot_id):
            raise ValueError(f"Robot is not registered: {robot_id}")

        return robot_id

    # 주행 요청을 검증한다.
    def _validate_navigation_request(self, robot_id, node_id):
        # 기존 Goal을 취소하기 전에 입력 오류를 거절한다.
        robot = fleet_manager.get_robot(robot_id)
        if robot is None or not robot.connected:
            raise ValueError(f"Robot이 연결되어 있지 않습니다: {robot_id}")
        if robot.state == RobotState.EMERGENCY_STOP:
            raise ValueError(f"비상정지 상태입니다: {robot_id}")
        target = get_node(node_id)
        measured_distance(robot, (target["x"], target["y"]), monotonic())

    # OMX terminal 이벤트를 처리한다.
    def _on_omx_terminal(self, robot_id, job_id):
        with self._navigation.lock:
            waiting = self._waiting_omx.get(robot_id)
            if waiting is None or waiting[0] != job_id:
                return
            _, callback, reason = self._waiting_omx.pop(robot_id)
            print(
                f"[{strftime('%Y-%m-%d %H:%M:%S')}] [INFO] [ROS GATEWAY OMX] robot={robot_id} "
                f"job_id={job_id} terminal confirmed",
                flush=True,
            )
            robot = fleet_manager.get_robot(robot_id)
            if callback is not None and robot is not None and robot.state != RobotState.EMERGENCY_STOP:
                try:
                    self._stop_before_navigation(robot_id, callback, reason, cancel_order=False)
                except (ValueError, RuntimeError) as exc:
                    robot.pause("omx_next_request_failed", str(exc))
                    print(
                        f"[{strftime('%Y-%m-%d %H:%M:%S')}] [ERROR] [ROS GATEWAY OMX ERROR] robot={robot_id} "
                        f"next_request_failed={exc!r}",
                        flush=True,
                    )

    def _stop_before_navigation(self, robot_id, callback, reason, *, cancel_order=True):
        """OMX·도킹·정렬·Nav2의 기존 작업 종료를 순서대로 확인한 뒤 다음 작업을 실행한다."""
        if reason != "charging_request" and robot_id not in self._dock_attempts:
            self._validate_dock_departure(robot_id)
        if self._dock_attempts.pop(robot_id, None) is not None:
            self._unverified_docks.add(robot_id)
        if robot_id in self._waiting_omx:
            job_id = self._waiting_omx[robot_id][0]
            self._waiting_omx[robot_id] = (job_id, callback, reason)
            print(
                f"[{strftime('%Y-%m-%d %H:%M:%S')}] [INFO] [ROS GATEWAY OMX] robot={robot_id} "
                f"new request queued until job_id={job_id} finishes",
                flush=True,
            )
            return
        robot = fleet_manager.get_robot(robot_id)
        if cancel_order and robot is not None and robot.order_id is not None:
            job_id = robot.order_id
            robot.clear_order()
            self._waiting_omx[robot_id] = (job_id, callback, reason)
            if not mqtt_manager.cancel_job(
                job_id, callback=lambda rid=robot_id, job=job_id: self._on_omx_terminal(rid, job)
            ):
                robot.set_state(RobotState.WAITING)
                return
            self._waiting_omx.pop(robot_id, None)
        if self._ros_node.has_active_auxiliary(robot_id):
            self._ros_node.cancel_auxiliary(
                robot_id, callback=lambda: self._navigation.stop(robot_id, callback, reason=reason)
            )
        else:
            self._navigation.stop(robot_id, callback, reason=reason)

    # 현재 작업을 취소한다.
    @_navigation_locked
    def cancel_current_work(self, robot_id, callback):
        robot_id = self._resolve_robot_id(robot_id)
        robot = fleet_manager.get_robot(robot_id)
        if robot is None or not robot.connected or robot.state == RobotState.EMERGENCY_STOP:
            raise ValueError(f"새 작업을 받을 수 없는 Robot입니다: {robot_id}")
        self._stop_before_navigation(robot_id, callback, "new_order_request")

    # ROS 노드에 cmd_vel 명령을 발행하고, 응답을 반환
    # 로봇에 속도 명령을 전송한다.
    @_navigation_locked
    def cmd_vel(self, robot_id: str, linear_x: float, angular_z: float) -> dict:
        robot_id = self._resolve_robot_id(robot_id)

        robot = fleet_manager.get_robot(robot_id)

        if robot is None:
            raise ValueError(f"Robot을 찾을 수 없습니다: {robot_id}")

        if robot.state == RobotState.EMERGENCY_STOP:
            raise ValueError(f"비상정지 상태입니다: {robot_id}")

        if not robot.connected:
            raise ValueError(f"Robot이 연결되어 있지 않습니다: {robot_id}")

        self._sync_dock_contact(robot_id)
        if not math.isfinite(linear_x) or not math.isfinite(angular_z):
            raise ValueError("속도 명령은 유한한 값이어야 합니다.")
        if (robot_id in self._charging_docks or robot_id in self._departing_docks
                or robot.state == RobotState.CHARGING or robot_id in self._unverified_docks):
            if linear_x or angular_z:
                raise ValueError("도킹 로봇은 검증된 경로 주행 또는 재도킹으로 출발하세요.")
            self._ros_node.publish_cmd_vel(robot_id=robot_id, linear_x=0.0, angular_z=0.0)
            return {"type": "cmd_vel_ack", "robot_id": robot_id, "source": "ros2"}
        active = (
            robot_id in self._navigation._executing
            or robot_id in self._navigation._stopping
            or robot_id in self._navigation._arrivals
            or robot.state in (RobotState.DOCKING, RobotState.TASK_ASSIGNED)
            or (robot.order_id is not None and robot.state == RobotState.WAITING)
        )
        if active and (linear_x != 0.0 or angular_z != 0.0):
            raise ValueError(f"자동 동작 중입니다. 먼저 정지해야 합니다: {robot_id}")
        can_update = not active
        if robot_id in self._navigation._robots:
            can_update = (
                self._navigation._traffic.validate_manual_velocity(robot_id, linear_x, angular_z) and can_update
            )
        self._ros_node.publish_cmd_vel(robot_id=robot_id, linear_x=linear_x, angular_z=angular_z)

        if not can_update:
            pass
        elif linear_x == 0.0 and angular_z == 0.0:
            robot.set_state(RobotState.IDLE)
        else:
            robot.set_state(RobotState.MOVING)

        return {
            "type": "cmd_vel_ack",
            "robot_id": robot_id,
            "linear_x": linear_x,
            "angular_z": angular_z,
            "source": "ros2",
        }

    # 로봇의 비상 정지를 요청한다.
    @_navigation_locked
    def emergency_stop(self, robot_id: str) -> dict:
        robot_id = self._resolve_robot_id(robot_id)
        robot = fleet_manager.get_robot(robot_id)

        if robot is None:
            raise ValueError(f"Robot을 찾을 수 없습니다: {robot_id}")

        if not robot.connected:
            raise ValueError(f"Robot이 연결되어 있지 않습니다: {robot_id}")

        # 취소/전송 중 예외가 나도 비상정지 상태를 잃지 않는다.
        robot.set_state(RobotState.EMERGENCY_STOP)
        if self._dock_attempts.pop(robot_id, None) is not None:
            self._unverified_docks.add(robot_id)
        if robot_id in self._waiting_omx:
            job_id, _, _ = self._waiting_omx[robot_id]
            self._waiting_omx[robot_id] = (job_id, None, "emergency_stop")
        if robot.order_id is not None:
            job_id = robot.order_id
            self._waiting_omx[robot_id] = (job_id, None, "emergency_stop")
            if mqtt_manager.cancel_job(
                job_id, callback=lambda rid=robot_id, job=job_id: self._on_omx_terminal(rid, job)
            ):
                self._waiting_omx.pop(robot_id, None)
            robot.clear_order()
        self._ros_node.cancel_auxiliary(robot_id)
        # 실행 중인 Nav2 주행 취소
        self._navigation.stop(
            robot_id, callback=lambda: self._navigation.cancel_after_stop(robot_id), reason="emergency_stop"
        )
        # 속도 명령 0
        self._ros_node.publish_cmd_vel(robot_id=robot_id, linear_x=0.0, angular_z=0.0)

        robot.route = None
        robot.goal_node = None
        robot.navigation_type = None
        robot.set_state(RobotState.EMERGENCY_STOP)
        confirmed = (
            robot_id not in self._navigation._stopping
            and not self._ros_node.has_active_auxiliary(robot_id)
            and robot_id not in self._waiting_omx
        )
        level = "INFO" if confirmed else "WARN"
        print(
            f"[{strftime('%Y-%m-%d %H:%M:%S')}] [{level}] [ROS GATEWAY STOP] robot={robot_id} "
            f"cancel_confirmed={confirmed}",
            flush=True,
        )

        return {
            "success": True,
            "robot_id": robot_id,
            "command": "emergency-stop",
            "cancel_confirmed": confirmed,
            "source": "ros2",
        }

    # 모든 로봇의 비상 정지를 요청한다.
    def emergency_stop_all(self) -> dict:
        robots = fleet_manager.get_all_robots()

        stopped_robots = []
        confirmations = {}
        skipped_offline = []

        for robot in robots:
            if not robot.connected:
                skipped_offline.append(robot.robot_id)
                continue

            result = self.emergency_stop(robot.robot_id)
            stopped_robots.append(robot.robot_id)
            confirmations[robot.robot_id] = result["cancel_confirmed"]

        return {
            "success": True,
            "command": "emergency-stop-all",
            "robots": stopped_robots,
            "cancel_confirmed": confirmations,
            "skipped_offline": skipped_offline,
            "source": "ros2",
        }

    # 로봇의 비상 정지 상태를 해제한다.
    @_navigation_locked
    def emergency_release(self, robot_id: str) -> dict:
        robot_id = self._resolve_robot_id(robot_id)
        robot = fleet_manager.get_robot(robot_id)

        if robot is None:
            raise ValueError(f"Robot을 찾을 수 없습니다: {robot_id}")

        if not robot.connected:
            raise ValueError(f"Robot이 연결되어 있지 않습니다: {robot_id}")

        if robot.state != RobotState.EMERGENCY_STOP:
            raise ValueError(f"비상정지 상태가 아닙니다: {robot_id}")

        if robot_id in self._navigation._stopping:
            raise ValueError(f"주행 취소 종료를 기다리고 있습니다: {robot_id}")
        if self._ros_node.has_active_auxiliary(robot_id):
            raise ValueError(f"도킹 또는 ArUco Action 종료를 기다리고 있습니다: {robot_id}")
        if robot_id in self._waiting_omx:
            raise ValueError(f"OMX 작업 종료를 기다리고 있습니다: {robot_id}")
        if robot_id in self._unverified_docks:
            robot.pause("dock_recovery_required")
        else:
            robot.set_state(RobotState.CHARGING if robot_id in self._charging_docks else RobotState.IDLE)
            self._sync_dock_contact(robot_id)

        return {
            "success": True,
            "robot_id": robot_id,
            "command": "emergency-release",
            "state": robot.state.value,
            "source": "ros2",
        }

    # 모든 로봇의 비상 정지 상태를 해제한다.
    def emergency_release_all(self) -> dict:
        robots = fleet_manager.get_all_robots()

        released_robots = []

        for robot in robots:
            if not robot.connected:
                continue

            if robot.state != RobotState.EMERGENCY_STOP:
                continue

            self.emergency_release(robot.robot_id)
            released_robots.append(robot.robot_id)

        return {"success": True, "command": "emergency-release-all", "robots": released_robots, "source": "ros2"}

    # 로봇이 현재 주행 중인지 확인한다.
    def navigation_active(self, robot_id):
        return self._navigation is not None and self._navigation.is_active(robot_id)

    # 실제 로봇을 Route Graph의 목적지 Node로 이동
    # 지정한 노드로 로봇 주행을 요청한다.
    @_navigation_locked
    def navigate_to_node(self, robot_id: str, node_id: str | int, *, _order_step=False, required_path=None) -> dict:
        # Robot ID 정규화 및 ROS 등록 여부 확인
        robot_id = self._resolve_robot_id(robot_id)
        robot = fleet_manager.get_robot(robot_id)

        if robot is None:
            raise ValueError(f"Robot을 찾을 수 없습니다: {robot_id}")

        if robot.state == RobotState.EMERGENCY_STOP:
            raise ValueError(f"비상정지 상태입니다: {robot_id}")

        self._validate_navigation_request(robot_id, node_id)
        if robot_id in self._unverified_docks:
            raise ValueError("도킹 또는 이탈 실패 후에는 충전소 복귀 명령으로 복구하세요.")
        if required_path is not None:
            required_path = tuple(str(n) for n in required_path)
            if robot.state not in (
                RobotState.IDLE,
                RobotState.CHARGING,
                RobotState.TASK_ASSIGNED,
                RobotState.WAITING,
            ) or self.navigation_active(robot_id):
                raise ValueError("로봇이 주행 중이거나 대기 중입니다.")
            if not required_path or required_path[0] != robot.occupied_node:
                raise ValueError("LLM 경로의 출발 노드와 현재 점유가 다릅니다.")
        same_active_goal = (
            robot.goal_node == str(node_id)
            and (_order_step or robot.order_id is None)
            and robot.state in (RobotState.NAVIGATING, RobotState.WAITING)
            and robot_id not in self._navigation._stopping
            and (robot_id in self._navigation._requests or robot_id in self._navigation._executing)
        )
        if not same_active_goal:
            self._stop_before_navigation(
                robot_id,
                lambda: self._start_navigation(robot_id, node_id, NavigationType.GOAL,
                    required_path=required_path),
                "new_goal_request",
                cancel_order=not _order_step,
            )

        return {
            "success": True,
            "status": "PROCESSING",
            "robot_id": robot_id,
            "command": "goal-node",
            "target_node": str(node_id),
            "source": "ros2",
        }

    # 로봇의 충전소 복귀 주행을 요청한다.
    @_navigation_locked
    def navigate_to_charging_station(self, robot_id: str) -> dict:
        robot_id = self._resolve_robot_id(robot_id)
        robot = fleet_manager.get_robot(robot_id)

        if robot is None:
            raise ValueError(f"Robot을 찾을 수 없습니다: {robot_id}")

        if robot.state == RobotState.EMERGENCY_STOP:
            raise ValueError(f"비상정지 상태입니다: {robot_id}")

        charging_node = CHARGING_STATION_NODES.get(robot_id)

        if charging_node is None:
            raise ValueError(f"충전 스테이션이 지정되지 않은 Robot입니다: {robot_id}")

        if self.charging_dispatch_ready(robot_id):
            return {"success": True, "status": "SUCCESS", "robot_id": robot_id,
                    "command": "charging", "target_node": charging_node, "source": "ros2"}
        self._validate_navigation_request(robot_id, charging_node)
        self._stop_before_navigation(
            robot_id,
            lambda: self._start_navigation(robot_id, charging_node, NavigationType.CHARGING),
            "charging_request",
        )

        return {
            "success": True,
            "status": "PROCESSING",
            "robot_id": robot_id,
            "command": "charging",
            "target_node": charging_node,
            "source": "ros2",
        }

    # 주행을 시작한다.
    @_navigation_locked
    def _start_navigation(
        self,
        robot_id: str,
        node_id: str | int,
        navigation_type: NavigationType = NavigationType.GOAL,
        *,
        required_path=None,
    ) -> None:
        # 경로/예약 정책은 시뮬레이션과 동일한 TrafficManager를 사용한다.
        # 기존 이동 목적과 최종 도착 이후의 시퀀스는 Robot에 유지한다.
        try:
            if navigation_type == NavigationType.CHARGING and robot_id in self._unverified_docks:
                self._charging_docks.pop(robot_id, None)
                self._departing_docks.pop(robot_id, None)
                self._unverified_docks.discard(robot_id)
                robot = fleet_manager.get_robot(robot_id)
                robot.docked_station = None
                if robot.occupied_node == CHARGING_STATION_NODES.get(robot_id):
                    contact = self._live_dock_contact(robot_id)
                    # 실제 실패의 명시적 복구도 접촉 중이면 직진 이탈부터 수행한다.
                    if contact is not None:
                        self._charging_docks[robot_id] = contact
            self._navigation.request(robot_id, node_id, navigation_type, required_path=required_path)
        except (ValueError, RuntimeError):
            robot = fleet_manager.get_robot(robot_id)
            if robot is not None:
                self._navigation._pause(robot_id, "navigation_request_failed")
            raise

    # 주행 결과 이벤트를 처리한다.
    @_navigation_locked
    def on_navigation_result(self, robot_id: str, status: int) -> None:
        robot = fleet_manager.get_robot(robot_id)

        if robot is None:
            return

        if status == GoalStatus.STATUS_SUCCEEDED:
            if robot.state in (RobotState.EMERGENCY_STOP, RobotState.PAUSED) or not robot.connected:
                return
            departure = self._departing_docks.get(robot_id)
            if departure is not None:
                # 일반 주행 결과가 이탈 완료보다 먼저 오면 작업을 시작하거나 성공을 보관하지 않는다.
                self._fail_station_exit(robot_id, "station_exit_failed")
                return
            # 최종 후속 작업도 같은 원본 좌표/최신성/거리 조건을 통과해야 한다.
            try:
                if robot.goal_node is None:
                    raise ValueError("최종 목적지가 없습니다.")
                goal = get_node(robot.goal_node)
                distance = measured_distance(robot, (goal["x"], goal["y"]), monotonic())
                print(
                    f"[{strftime('%Y-%m-%d %H:%M:%S')}] [INFO] [ROS GATEWAY ARRIVAL] robot={robot_id} "
                    f"goal={robot.goal_node} distance={distance:.3f}m "
                    f"tolerance={ARRIVAL_DISTANCE_THRESHOLD:.3f}m",
                    flush=True,
                )
                if distance > ARRIVAL_DISTANCE_THRESHOLD:
                    raise ValueError(f"도착 위치 불일치: {distance:.3f}m")
            except (OSError, KeyError, ValueError) as exc:
                self._navigation._pause(robot_id, f"final_arrival_failed:{exc}")
                return

            # 가장 가까운 노드 복귀 완료
            if robot.navigation_type == NavigationType.RETURN:
                robot.current_node = robot.goal_node
                robot.navigation_type = None
                robot.route = None
                robot.set_state(RobotState.IDLE)
                print(
                    f"[{strftime('%Y-%m-%d %H:%M:%S')}] [INFO] [ROS GATEWAY RETURN] robot={robot_id} "
                    f"node={robot.current_node}",
                    flush=True,
                )
                return

            if robot.navigation_type == NavigationType.CHARGING:
                print(
                    f"[{strftime('%Y-%m-%d %H:%M:%S')}] [INFO] [ROS GATEWAY CHARGING] robot={robot_id} "
                    f"station={robot.goal_node}",
                    flush=True,
                )

                if robot.goal_node != CHARGING_STATION_NODES.get(robot_id):
                    robot.pause("dock_station_unverified")
                    return
                station = DOCK_STATION_MAP.get(robot_id)
                if station is None:
                    robot.pause("dock_station_unverified")
                    return
                sample = mqtt_manager.get_dock_snapshot(station)
                if not sample["broker_connected"]:
                    robot.pause("dock_sensor_unavailable")
                    return
                self._dock_attempts[robot_id] = {
                    "station": station, "started_at": monotonic(),
                    "generation": sample["generation"], "succeeded_at": None,
                }
                robot.set_state(RobotState.DOCKING)
                try:
                    self._ros_node.send_precision_dock(robot_id)
                except (ValueError, RuntimeError) as exc:
                    self._dock_attempts.pop(robot_id, None)
                    self._unverified_docks.add(robot_id)
                    robot.pause("precision_dock_goal_send_failed", str(exc))
                    print(
                        f"[{strftime('%Y-%m-%d %H:%M:%S')}] [ERROR] [ROS GATEWAY DOCK ERROR] "
                        f"robot={robot_id} goal_send_failed={exc!r}",
                        flush=True,
                    )
                return

            # 일반 목적지 이동 완료
            print(
                f"[{strftime('%Y-%m-%d %H:%M:%S')}] [INFO] [ROS GATEWAY ARRIVAL] robot={robot_id} "
                f"goal={robot.goal_node}",
                flush=True,
            )

            robot.current_node = robot.goal_node
            robot.navigation_type = None
            robot.route = None

            # 현재 목적지 Node의 ArUco Marker ID 확인
            marker_id = ARUCO_MARKER_IDS.get(robot.current_node)

            if marker_id is not None:
                robot.set_state(RobotState.DOCKING)

                print(
                    f"[{strftime('%Y-%m-%d %H:%M:%S')}] [INFO] [ROS GATEWAY ARUCO] robot={robot_id} "
                    f"node={robot.current_node} alignment starting marker_id={marker_id}",
                    flush=True,
                )

                try:
                    self._ros_node.send_aruco_align(robot_id=robot_id, marker_id=marker_id)
                except (ValueError, RuntimeError) as exc:
                    robot.pause("aruco_goal_send_failed", str(exc))
                    print(
                        f"[{strftime('%Y-%m-%d %H:%M:%S')}] [ERROR] [ROS GATEWAY ARUCO ERROR] "
                        f"robot={robot_id} goal_send_failed={exc!r}",
                        flush=True,
                    )
                return

            # ArUco 정렬 대상이 아닌 일반 Node
            robot.set_state(RobotState.IDLE)

        elif status == GoalStatus.STATUS_CANCELED:
            # 취소 처리
            robot.route = None
            pass

        elif status == GoalStatus.STATUS_ABORTED:
            # 실패 처리
            robot.pause("navigation_aborted", f"status={status}")

    # 로봇을 가장 가까운 노드로 복귀시킨다.
    @_navigation_locked
    def return_to_nearest_node(self, robot_id: str) -> dict:
        robot_id = self._resolve_robot_id(robot_id)

        robot = fleet_manager.get_robot(robot_id)

        if robot is None:
            raise ValueError(f"Robot을 찾을 수 없습니다: {robot_id}")

        if not robot.connected:
            raise ValueError(f"Robot이 연결되어 있지 않습니다: {robot_id}")

        if robot.state == RobotState.EMERGENCY_STOP:
            raise ValueError(f"비상정지 상태입니다: {robot_id}")

        if robot.x is None or robot.y is None:
            raise ValueError(f"Robot 위치를 아직 받지 못했습니다: {robot_id}")

        graph = load_route_graph()
        path_plan = DistanceAStar(graph)

        nearest_node = find_nearest_node(nodes=path_plan.nodes, x=robot.x, y=robot.y)

        if nearest_node is None:
            raise ValueError("가장 가까운 Node를 찾을 수 없습니다.")

        target = get_node(nearest_node)

        # 기존 복귀 목적과 yaw는 유지하며 동일한 예약 실행부를 통과한다.
        self._validate_navigation_request(robot_id, nearest_node)
        self._stop_before_navigation(
            robot_id, lambda: self._start_navigation(robot_id, nearest_node, NavigationType.RETURN), "return_request"
        )

        return {
            "success": True,
            "status": "SUCCESS",
            "robot_id": robot_id,
            "command": "return-nearest-node",
            "nearest_node": nearest_node,
            "target": {"node_id": target["id"], "x": target["x"], "y": target["y"]},
            "source": "ros2",
        }

    # 정밀 도킹 결과 이벤트를 처리한다.
    @_navigation_locked
    def on_precision_dock_result(self, robot_id: str, status: int) -> None:
        robot = fleet_manager.get_robot(robot_id)

        if robot is None:
            return

        attempt = self._dock_attempts.get(robot_id)
        if (not robot.connected or robot.state != RobotState.DOCKING
                or robot.navigation_type != NavigationType.CHARGING or attempt is None):
            return

        if monotonic() - attempt["started_at"] >= DOCK_ACTION_TIMEOUT_S:
            self._dock_attempts.pop(robot_id, None)
            self._unverified_docks.add(robot_id)
            robot.pause("dock_action_timeout")
            return

        if status == GoalStatus.STATUS_SUCCEEDED:
            if attempt["succeeded_at"] is None:
                attempt["succeeded_at"] = monotonic()
            self._tick_charging_docks()
        else:
            self._dock_attempts.pop(robot_id, None)
            self._unverified_docks.add(robot_id)
            robot.pause("precision_dock_failed", f"status={status}")
            print(f"[{strftime('%Y-%m-%d %H:%M:%S')}] [WARN] [ROS GATEWAY DOCK] robot={robot_id} failed", flush=True)


    # ArUco 정렬 결과 이벤트를 처리한다.
    @_navigation_locked
    def on_aruco_align_result(self, robot_id: str, status: int) -> None:
        robot = fleet_manager.get_robot(robot_id)

        if robot is None or robot.state == RobotState.EMERGENCY_STOP:
            return

        if not robot.connected or robot.state != RobotState.DOCKING:
            return

        if status == GoalStatus.STATUS_SUCCEEDED:
            print(
                f"[{strftime('%Y-%m-%d %H:%M:%S')}] [INFO] [ROS GATEWAY ARUCO] robot={robot_id} "
                f"aligned node={robot.current_node}",
                flush=True,
            )

            if robot.order_id is None:
                # 일반 Node 이동은 정렬만 완료한다. 주문 없는 OMX 명령은 보내지 않는다.
                robot.set_state(RobotState.IDLE)
                print(
                    f"[{strftime('%Y-%m-%d %H:%M:%S')}] [INFO] [ROS GATEWAY ARUCO] robot={robot_id} "
                    "no order, OMX skipped",
                    flush=True,
                )
                return

            # 현재 Node에 연결된 OMX 조회
            omx_id = NODE_OMX_MAP.get(robot.current_node)

            if omx_id is None:
                print(
                    f"[{strftime('%Y-%m-%d %H:%M:%S')}] [INFO] [ROS GATEWAY ARUCO] robot={robot_id} "
                    f"no OMX for node={robot.current_node}",
                    flush=True,
                )
                robot.set_state(RobotState.IDLE)
                return

            # 현재 Node에서 처리할 품목 조회
            item_keys = NODE_ITEM_MAP.get(robot.current_node, [])
            items = {}

            for key in item_keys:
                quantity = robot.order_items.get(key, 0)
                if quantity > 0:
                    items[key] = quantity

            if not items:
                robot.pause("order_items_missing_for_node", f"order_id={robot.order_id}, node={robot.current_node}")
                print(
                    f"[{strftime('%Y-%m-%d %H:%M:%S')}] [INFO] [ROS GATEWAY ARUCO] robot={robot_id} "
                    f"order={robot.order_id} has no items for node={robot.current_node}",
                    flush=True,
                )
                return

            print(
                f"[{strftime('%Y-%m-%d %H:%M:%S')}] [INFO] [ROS GATEWAY MQTT] robot={robot_id} "
                f"omx={omx_id} items={items}",
                flush=True,
            )

            # 빠른 MQTT Result가 publish 직후 도착해도 현재 단계의 결과로 처리한다.
            robot.set_state(RobotState.WAITING)
            try:
                mqtt_manager.send_job(omx_id=omx_id, job_id=robot.order_id, items=items)
            except (ValueError, RuntimeError) as exc:
                print(
                    f"[{strftime('%Y-%m-%d %H:%M:%S')}] [ERROR] [ROS GATEWAY MQTT ERROR] "
                    f"robot={robot_id} error={exc!r}",
                    flush=True,
                )
                robot.pause("mqtt_job_send_failed", str(exc))
                return

        else:
            print(
                f"[{strftime('%Y-%m-%d %H:%M:%S')}] [WARN] [ROS GATEWAY ARUCO] robot={robot_id} "
                f"failed status={status}",
                flush=True,
            )
            robot.pause("aruco_alignment_failed", f"status={status}")


ros_gateway = RosGateway()
