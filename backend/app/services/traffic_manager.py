"""예약, 대기 재시도, 교착 해소 및 구간 주행 허가 정책.

ROS, mock 데이터, 좌표 보간에 의존하지 않는다. 호출자가 그래프, 시각 및
점유 상태를 제공한다. 시각은 예약표와 동일한 monotonic 기준을 사용한다.
robots는 robot_id를 키로 하는 상태 dict이며 필요한 필드는 doc/traffic_manager.md 참고.
상태 갱신과 정책 호출은 lock으로 함께 보호해야 한다.

*_after_stop / confirm_node_arrival API는 호출자가 실제 정지/도착을 확인한 뒤 호출한다.
이 서비스는 정지 명령을 보내거나 실제 정지를 확인하지 않는다.
"""

from copy import copy, deepcopy
from dataclasses import dataclass
import math
from threading import RLock
from typing import Any, Literal
from uuid import uuid4

from .occupancy import occupied_resource
from .pathfinding import DistanceAStar
from .reservation import ReservationTable, build_schedule, node_key, edge_key
from .route_graph import find_edge_ids


# TurtleBot3 Burger 규격 풋프린트: 반경 0.105m (지름 0.21m)
TURTLEBOT3_BURGER_RADIUS_M = 0.06
TURTLEBOT3_CLEARANCE_DIAMETER_M = 0.12


@dataclass(frozen=True)
class SegmentPermission:
    decision: Literal["allowed", "waiting", "replan"]
    reason: str


def safe_travel_distance(
    x: float,
    y: float,
    dx: float,
    dy: float,
    max_step: float,
    other_positions: list[tuple[float, float]],
    min_clearance: float = TURTLEBOT3_CLEARANCE_DIAMETER_M,
) -> float:
    """전진 이동 시 다른 로봇과의 거리가 min_clearance 미만으로 좁혀지지 않는 최대 이동 거리를 반환한다."""
    step = max_step
    dist_to_target = math.hypot(dx, dy)
    if dist_to_target < 1e-9:
        return 0.0
    ux = dx / dist_to_target
    uy = dy / dist_to_target

    for ox, oy in other_positions:
        vx = ox - x
        vy = oy - y
        curr_dist = math.hypot(vx, vy)
        dot = ux * vx + uy * vy
        c = curr_dist**2 - min_clearance**2
        if c < 0:
            if dot > 0:
                return 0.0
            continue
        if dot <= 0:
            continue
        discriminant = dot**2 - c
        if discriminant > 0:
            s1 = dot - math.sqrt(discriminant)
            if s1 > 0:
                step = min(step, max(0.0, s1 - 1e-4))
            elif dot > 0:
                return 0.0
    return max(0.0, step)


class TrafficManager:
    # TrafficManager의 초기 상태를 설정한다.
    def __init__(self, robots: dict[str, dict[str, Any]], reservations=None, *, speed_mps: float, safety_margin: float):
        if not all(math.isfinite(v) and v > 0 for v in (speed_mps, safety_margin)):
            raise ValueError("속도와 안전 여유는 유한한 양수여야 합니다.")
        self._robots = robots
        self._reservations = reservations if reservations is not None else ReservationTable()
        self.speed_mps = speed_mps
        self.safety_margin = safety_margin
        self.robot_radius_m = TURTLEBOT3_BURGER_RADIUS_M
        self.lock = RLock()
        self._pending: dict[str, None] = {}
        self._concession: dict[str, dict] = {}
        self._required_paths = {}
        self._request_sequence = 0
        # 연결이 끊긴 주행 로봇의 남은 경로. 재연결해 실제 점유를 확인할 때까지 다른 로봇에 양보하지 않는다.
        self._protected_resources = {}

    def planning_snapshot(self):
        """계획자는 실행기 소유 상태의 복사본만 조회한다."""
        with self.lock:
            return (deepcopy(self._robots), dict(self._required_paths),
                    deepcopy(self._protected_resources))

    def planning_wait_reservations(self):
        """MAPF에는 예약 소유자만 전달하고 FMS 시간표는 노출하지 않는다."""
        with self.lock:
            owners = {}
            for row in self._reservations.snapshot():
                if row.resource[0] == "node":
                    owners.setdefault(row.resource[1], set()).add(row.robot_id)
            return {node: sorted(robots) for node, robots in owners.items()}

    def commit_required_routes(self, graph, pending, paths, waits, ordered, *, now):
        with self.lock:
            previous_rows = self._reservations.snapshot()
            trial = self._planning_copy()
            for rid in ordered:
                for dependency in waits[rid]:
                    dependency["navigation_id"] = trial._robots[dependency["robot_id"]]["navigation_id"]
                trial.request_navigation_after_stop(
                    rid, pending[rid]["node"], now=now, graph=graph,
                    required_path=paths[rid], mapf_wait_for=waits[rid],
                )
            if not self._reservations.compare_and_replace(previous_rows, trial._reservations.snapshot()):
                return False
            for rid, robot in trial._robots.items():
                self._robots[rid].clear()
                self._robots[rid].update(robot)
            self._required_paths = trial._required_paths
            self._pending = trial._pending
            self._concession = trial._concession
            self._protected_resources = trial._protected_resources
            self._request_sequence = trial._request_sequence
            return True

    def recover_required_path(self, robot_id, path, decision, graph, *, now, initial_goal=None):
        with self.lock:
            target = decision.get("temporary_goal")
            if initial_goal is not None:
                if target is None:
                    self.request_navigation_after_stop(
                        robot_id, initial_goal, now=now, graph=graph, required_path=path,
                    )
                else:
                    self.start_required_path_maneuver(
                        robot_id, path, original_goal=initial_goal, temporary_goal=target,
                        now=now, graph=graph,
                    )
            elif not self.replace_required_path_after_stop(
                robot_id, path, now=now, graph=graph, temporary_goal=target,
            ):
                return False
            self._robots[robot_id]["kev_recovery"] = {
                "action": decision["action"], "path": list(path), "choice": decision["choice"],
                "confidence": decision.get("confidence"),
            }
            return True

    # 로봇이 점유 중인 자원을 보호 목록에 넣는다.
    def protect_resources(self, robot_id, resources):
        with self.lock:
            self._protected_resources[robot_id] = set(resources)

    # 로봇의 보호 자원 목록을 해제한다.
    def clear_protected_resources(self, robot_id):
        with self.lock:
            self._protected_resources.pop(robot_id, None)

    # 다른 로봇이 자원을 보호 중인지 확인한다.
    def is_resource_protected(self, robot_id, resource):
        with self.lock:
            return any(
                owner != robot_id and resource in resources for owner, resources in self._protected_resources.items()
            )

    def reset_after_stop(self):
        """전체 실행 종료 및 점유 보존이 확인된 상태에서 계획만 초기화한다."""
        with self.lock:
            for robot in self._robots.values():
                self.cancel_navigation_after_stop(robot["robot_id"])
            requests = {(row.robot_id, row.navigation_id) for row in self._reservations.snapshot()}
            for robot_id, navigation_id in requests:
                self._reservations.release_request(robot_id, navigation_id)
            self._pending.clear()
            self._concession.clear()
            self._protected_resources.clear()
            self._request_sequence = 0

    # 진행 중인 주행 요청이 있는지 확인한다.
    def has_active_requests(self) -> bool:
        with self.lock:
            return bool(self._concession) or any(robot["goal_node"] is not None for robot in self._robots.values())

    def request_navigation_after_stop(self, robot_id, goal_node, *, now, graph, required_path=None, task_priority=0, mapf_wait_for=None):
        """기존 실행 정지와 점유 갱신 후 새 요청을 접수하고 예약한다."""
        with self.lock:
            self._validate_time(now)
            planner = DistanceAStar(graph)
            goal_node = str(goal_node)
            if goal_node not in planner.nodes:
                raise ValueError("목적지 노드가 존재하지 않습니다.")
            robot = self._robots[robot_id]
            occupied_resource(robot, graph)
            # 경로 생성 중 상태가 바뀐 요청은 기존 주행을 취소하기 전에 거부한다.
            fixed = None
            if required_path is not None:
                fixed = tuple(str(n) for n in required_path)
                if robot["goal_node"] is not None or robot["route"] is not None or robot["status"] != "IDLE":
                    raise ValueError("로봇이 주행 중이거나 대기 중입니다. 정지 후 다시 시도하세요.")
                if not fixed or fixed[0] != robot["occupied_node"] or fixed[-1] != goal_node:
                    raise ValueError("LLM 경로의 출발·목적지와 로봇 상태가 다릅니다.")
                if any(n not in planner.nodes for n in fixed) or any(
                    not any(n == b for n, _ in planner.edges[a]) for a, b in zip(fixed, fixed[1:])
                ):
                    raise ValueError("LLM 경로에 존재하지 않는 노드·유향 간선이 있습니다.")
                # 모델 결과의 방문 제한을 예약 경계에서도 다시 확인한다.
                steps = list(zip(fixed, fixed[1:]))
                if len(set(steps)) != len(steps) or any(fixed.count(n) > 2 for n in set(fixed)):
                    raise ValueError("LLM 경로가 간선 재사용 또는 노드 방문 제한을 위반했습니다.")
            self.cancel_navigation_after_stop(robot_id)
            if fixed is not None:
                self._required_paths[robot_id] = fixed
            robot["task_priority"] = int(task_priority)
            robot["stop_requested"] = False
            self._request_sequence += 1
            robot["request_order"] = self._request_sequence
            robot["navigation_id"] = uuid4().hex
            robot["goal_node"] = goal_node
            if mapf_wait_for:
                robot["mapf_wait_for"] = deepcopy(mapf_wait_for)
            self._wait_for_reservation(robot)
            self._retry_waiting(now, graph, planner)

    def cancel_navigation_after_stop(self, robot_id):
        """정지가 확인된 요청을 취소한다. 실제 위치·점유는 남긴다."""
        with self.lock:
            robot = self._robots[robot_id]
            robot["status"] = "IDLE"
            robot["goal_node"] = None
            robot["navigation_id"] = None
            robot["request_order"] = None
            robot["task_priority"] = 0
            robot["stop_requested"] = True
            robot.pop("kev_recovery", None)
            robot.pop("kev_maneuver", None)
            robot.pop("mapf_wait_for", None)
            self._required_paths.pop(robot_id, None)
            self._concession.pop(robot_id, None)
            self._pending.pop(robot_id, None)
            self._release_schedule(robot)
            robot["route"] = None

    # 정지 후 경로 예약이 가능할 때까지 대기한다.
    def wait_for_reservation_after_stop(self, robot_id):
        with self.lock:
            self._wait_for_reservation(self._robots[robot_id])

    def replace_required_path_after_stop(self, robot_id, path, *, now, graph, temporary_goal=None):
        """노드 위 정지가 확인된 AI 요청의 작업 ID를 보존하며 경로·예약을 교체한다.

        후보는 사본에서 예약하고, 실패 시 기존 경로와 예약을 그대로 남긴다.
        호출자는 추론 중 위치/주문/지도 변경 여부를 먼저 확인해야 한다.
        """
        with self.lock:
            self._validate_time(now)
            robot = self._robots[robot_id]
            if (
                robot_id not in self._required_paths
                or robot["status"] != "WAITING"
                or robot.get("stop_requested")
                or robot.get("connection_state", "ONLINE") != "ONLINE"
                or robot.get("occupied_node") is None
                or robot.get("occupied_edge") is not None
                or robot.get("goal_node") is None
            ):
                raise ValueError("AI 경로 교체는 노드 위에서 대기 중인 요청에만 가능합니다.")
            from .kev_mapf.kev_graph import Graph

            fixed = tuple(str(n) for n in path)
            target = robot["goal_node"] if temporary_goal is None else str(temporary_goal)
            Graph(graph).check_path(
                list(fixed), start=int(robot["occupied_node"]), goal=int(target)
            )
            if temporary_goal is not None and (len(fixed) < 2 or target == robot["occupied_node"]):
                raise ValueError("임시 대피는 다른 노드로 이동해야 합니다.")
            if fixed == self._required_paths[robot_id]:
                return False
            trial = self._planning_copy()
            mover = trial._robots[robot_id]
            mover.pop("mapf_wait_for", None)
            trial._release_schedule(mover)
            mover["route"] = None
            if temporary_goal is None:
                mover.pop("kev_maneuver", None)
            else:
                mover["kev_maneuver"] = {"target_node": target, "phase": "MOVING_ASIDE"}
            trial._required_paths[robot_id] = fixed
            if not trial._try_schedule(mover, now, graph, DistanceAStar(graph)):
                return False
            rows = [r for r in trial._reservations.snapshot() if r.robot_id == robot_id]
            if rows:
                if not self._reservations.replace_request(robot_id, robot["navigation_id"], rows):
                    return False
            else:
                self._release_schedule(robot)
            self._required_paths[robot_id] = fixed
            robot.pop("kev_maneuver", None)
            robot.pop("mapf_wait_for", None)
            robot.update(mover)
            self._pending.pop(robot_id, None)
            return True

    def start_required_path_maneuver(self, robot_id, path, *, original_goal, temporary_goal, now, graph):
        """출발 대기 요청에서 KEV가 선택한 임시 경로만 예약·실행한다."""
        with self.lock:
            robot = self._robots[robot_id]
            if robot["status"] != "IDLE" or robot.get("goal_node") is not None or robot.get("route") is not None:
                raise ValueError("임시 출발은 작업이 없는 노드 위 대기 로봇에만 가능합니다.")
            if str(original_goal) == str(temporary_goal):
                raise ValueError("임시 대피와 원래 목적지는 구분해야 합니다.")
            self.request_navigation_after_stop(
                robot_id, str(temporary_goal), now=now, graph=graph, required_path=path,
            )
            # The reservation covers only the KEV-selected temporary route.
            # Keep the same request ID but never complete the business task here.
            robot["goal_node"] = str(original_goal)
            robot["kev_maneuver"] = {"target_node": str(temporary_goal), "phase": "MOVING_ASIDE"}

    def retry_waiting(self, now, graph, planner=None):
        """정지 중인 대기 요청을 재계획한다. 양보 종료 시 실행 정지는 호출자 책임."""
        with self.lock:
            self._validate_time(now)
            self._retry_waiting(now, graph, planner or DistanceAStar(graph))

    def confirm_node_arrival(self, robot_id, node_id):
        """실행기가 좌표를 갱신하고 도착을 확인한 뒤 구간 진행을 반영한다."""
        with self.lock:
            robot = self._robots[robot_id]
            route = robot["route"]
            if route is None:
                raise ValueError("진행 중인 경로가 없습니다.")
            index = route["segment_index"] + 1
            if index >= len(route["node_ids"]) or str(node_id) != route["node_ids"][index]:
                raise ValueError("현재 구간의 도착 노드와 일치하지 않습니다.")
            robot["current_node"] = str(node_id)
            robot["occupied_node"] = str(node_id)
            robot["occupied_edge"] = None
            route["segment_index"] = index
            if robot_id in self._required_paths:
                self._required_paths[robot_id] = self._required_paths[robot_id][1:]
            if index == len(route["node_ids"]) - 1:
                self._finish_route(robot)

    # 로봇이 간선에 진입한 점유 상태를 확정한다.
    def confirm_edge_occupancy(self, robot_id):
        with self.lock:
            robot = self._robots[robot_id]
            route = robot["route"]
            if route is None:
                raise ValueError("진행 중인 경로가 없습니다.")
            robot["current_node"] = None
            robot["occupied_node"] = None
            robot["occupied_edge"] = route["edge_ids"][route["segment_index"]]

    def validate_manual_velocity(self, robot_id, linear_x, angular_z) -> bool:
        """수동 명령 가능 여부. 예약 중 영속도는 허용하지만 False로 상태 보존 지시."""
        with self.lock:
            robot = self._robots[robot_id]
            if robot["goal_node"] is not None or robot_id in self._concession:
                if linear_x != 0 or angular_z != 0:
                    raise ValueError("예약 주행 또는 대기 중에는 먼저 정지 명령을 실행해야 합니다.")
                return False
            return True

    # 자원이 다른 로봇에 의해 막혔는지 확인한다.
    def is_resource_blocked(self, robot_id, resource, *, now, graph) -> bool:
        with self.lock:
            self._validate_time(now)
            return (
                self.is_resource_protected(robot_id, resource)
                or any(
                    other["robot_id"] != robot_id and occupied_resource(other, graph) == resource
                    for other in self._robots.values()
                )
                or any(
                    row.robot_id != robot_id and row.resource == resource and row.end > now
                    for row in self._reservations.snapshot()
                )
            )

    # 예약 계산에 사용할 시간을 검증한다.
    @staticmethod
    def _validate_time(value):
        if not math.isfinite(value) or value < 0:
            raise ValueError("시각은 유한한 0 이상의 값이어야 합니다.")

    def check_segment_permission(self, robot_id, *, now, expected_arrival_at, graph) -> SegmentPermission:
        """예약·출발시각·실제 점유·예상 도착을 검사한다. 상태/예약을 변경하지 않는다.

        expected_arrival_at은 실행기가 계산한다. tick 기반 실행기는 해당 tick에서
        이동할 거리를 반영하고, 실제 실행기는 관측 기반 잔여 이동 시간을 반영한다.
        replan은 즉시 예약 해제를 뜻하지 않는다. 정지 확인 후 대기로 전환해야 한다.
        """
        with self.lock:
            self._validate_time(now)
            self._validate_time(expected_arrival_at)
            if self._mapf_zone_waiting(self._robots[robot_id]):
                return SegmentPermission("waiting", "mapf_priority_zone_not_clear")
            robot = self._robots[robot_id]
            route = robot["route"]
            if route is None or route.get("departure_at") is None:
                return SegmentPermission("replan", "missing_schedule")
            index = route["segment_index"]
            nodes = route["node_ids"]
            if not 0 <= index < len(nodes) - 1:
                raise ValueError("경로 진행 인덱스가 올바르지 않습니다.")
            resource = edge_key(nodes[index], nodes[index + 1])
            target = node_key(nodes[index + 1])
            rows = [
                row
                for row in self._reservations.snapshot()
                if row.robot_id == robot_id
                and row.navigation_id == robot["navigation_id"]
                and row.segment_index == index
            ]
            edge = next((row for row in rows if row.resource == resource), None)
            destination = next((row for row in rows if row.resource == target), None)
            if edge is None or destination is None:
                return SegmentPermission("replan", "missing_reservation")
            if now <= route["segment_departures"][index]:
                return SegmentPermission("waiting", "departure_time")
            curr_node = str(nodes[index])
            target_node = str(nodes[index + 1])
            is_at_upstream_node = (robot.get("occupied_node") == curr_node)

            if self.is_resource_protected(robot_id, resource) or self.is_resource_protected(robot_id, target):
                return SegmentPermission("replan", "offline_robot_route")
            if (
                expected_arrival_at + self.safety_margin > edge.end + 1e-6
                or expected_arrival_at + self.safety_margin > destination.end + 1e-6
            ):
                return SegmentPermission("replan", "schedule_overrun")

            for other in self._robots.values():
                if other is robot:
                    continue
                occ = occupied_resource(other, graph)
                if occ == resource:
                    return SegmentPermission("replan", "occupied")
                if occ == target and (
                    now >= destination.start
                    or other["route"] is None
                    or other["route"].get("departure_at") is None
                ):
                    return SegmentPermission("replan", "occupied")

            if is_at_upstream_node:
                self_goal = str(robot.get("goal_node")) if robot.get("goal_node") is not None else None
                prio_self = int(robot.get("task_priority", 0))
                order_self = robot.get("request_order") or math.inf

                for other in self._robots.values():
                    if other is robot:
                        continue
                    other_goal = str(other.get("goal_node")) if other.get("goal_node") is not None else None
                    other_occ = str(other.get("occupied_node")) if other.get("occupied_node") is not None else None
                    o_route = other.get("route")
                    # An unscheduled blocked route is not an approaching robot.
                    # Yielding to its hypothetical next node can prevent the
                    # KEV-selected departure from ever clearing the blockage.
                    if o_route and o_route.get("node_ids") and o_route.get("departure_at") is not None:
                        o_idx = o_route.get("segment_index", 0)
                        o_nodes = [str(n) for n in o_route["node_ids"]]
                        if o_idx + 1 < len(o_nodes) and o_nodes[o_idx + 1] == target_node:
                            # 상대방이 내 현재 점유 노드로 이동하려는 경우 -> 내가 먼저 노드를 비워주어야 함
                            if other_goal == curr_node:
                                continue
                            # 내가 상대방의 점유 노드로 가려는 경우 -> 상대방이 먼저 비울 때까지 대기
                            if self_goal == other_occ:
                                return SegmentPermission("waiting", "yield_to_vacating_robot")
                            # 상대방이 이미 진입로(엣지) 주행 중인 경우 -> 상류 노드에서 대기
                            if other_occ is None:
                                return SegmentPermission("waiting", "other_on_approach")
                            # 둘 다 상류 노드에서 진입 준비 중인 경우 -> 우선순위 및 접수순서 비교
                            prio_other = int(other.get("task_priority", 0))
                            order_other = other.get("request_order") or math.inf
                            if (prio_other, -order_other) > (prio_self, -order_self):
                                return SegmentPermission("waiting", "target_node_precedence")

                    if other_occ == target_node:
                        if other_goal == target_node and (other.get("route") is None or other["route"].get("departure_at") is None):
                            return SegmentPermission("replan", "target_node_parked")
                        if other.get("route") is None or other["route"].get("departure_at") is None or now >= destination.start:
                            return SegmentPermission("waiting", "target_node_occupied")
            return SegmentPermission("allowed", "reserved")

    # 로봇의 기존 예약 일정을 해제한다.
    def _release_schedule(self, robot):
        # 호출자는 실행 종료/정지를 확인해야 한다. 위치·점유는 유지한다.
        requests = {row.navigation_id for row in self._reservations.snapshot() if row.robot_id == robot["robot_id"]}
        for navigation_id in requests:
            self._reservations.release_request(robot["robot_id"], navigation_id)

    # 예약을 기다리는 로봇을 대기열에 넣는다.
    def _wait_for_reservation(self, robot):
        robot["status"] = "WAITING"
        if robot["route"] is not None:
            robot["route"]["phase"] = "waiting"
            robot["route"]["departure_at"] = None
        self._release_schedule(robot)
        self._pending.setdefault(robot["robot_id"], None)

    # 로봇의 경로 예약을 시도한다.
    def _try_schedule(self, robot, now, graph, planner, *, vacating_nodes=None):
        if self._mapf_zone_waiting(robot):
            return False
        maneuver = robot.get("kev_maneuver")
        if maneuver and maneuver["phase"] == "ASIDE":
            # KEV must generate and approve the route back to the original goal.
            return False
        others = [row for row in self._reservations.snapshot() if row.robot_id != robot["robot_id"]]
        blocked = {}
        for row in others:
            blocked.setdefault(row.resource, []).append((row.start, row.end))
        for owner, resources in self._protected_resources.items():
            if owner != robot["robot_id"]:
                for resource in resources:
                    blocked.setdefault(resource, []).append((now, math.inf))
        for other in self._robots.values():
            if other is robot:
                continue
            resource = occupied_resource(other, graph)
            route = other["route"]
            scheduled = route is not None and route.get("departure_at") is not None
            if not scheduled and resource[0] == "node" and vacating_nodes and resource[1] in vacating_nodes:
                until = vacating_nodes[resource[1]]
            else:
                until = (
                    max(
                        (
                            row.end
                            for row in others
                            if row.robot_id == other["robot_id"] and row.resource == resource and row.start <= now < row.end
                        ),
                        default=math.inf,
                    )
                    if scheduled
                    else math.inf
                )
            blocked.setdefault(resource, []).append((max(0.0, now - self.safety_margin), until))

        resource = occupied_resource(robot, graph)
        plan = planner.plan_timed(
            maneuver["target_node"] if maneuver else
            self._concession.get(robot["robot_id"], {}).get("node", robot["goal_node"]),
            (robot["x"], robot["y"]),
            robot["occupied_node"],
            resource[1:] if resource[0] == "edge" else None,
            blocked=blocked,
            now=now,
            speed_mps=self.speed_mps,
            safety_margin=self.safety_margin,
            required_path=self._required_paths.get(robot["robot_id"]),
        )
        if plan is None:
            return False
        node_ids, departures = plan["node_ids"], plan["segment_departures"]
        edge_ids = find_edge_ids(graph, node_ids)
        waypoint_yaws = [float(robot["yaw"])]
        for start, end in zip(node_ids, node_ids[1:]):
            ax, ay = planner.nodes[start]
            bx, by = planner.nodes[end]
            waypoint_yaws.append(
                math.atan2(by - ay, bx - ax) if math.hypot(bx - ax, by - ay) > 1e-9 else waypoint_yaws[-1]
            )
        departure = departures[0] if departures else now
        batch = build_schedule(
            robot["robot_id"],
            robot["navigation_id"],
            node_ids,
            planner.nodes,
            (robot["x"], robot["y"]),
            robot["occupied_node"],
            now=now,
            departure_at=departure,
            speed_mps=self.speed_mps,
            safety_margin=self.safety_margin,
            segment_departures=departures,
        )
        if not self._reservations.replace_request(robot["robot_id"], robot["navigation_id"], batch):
            return False
        # 경로와 시간표를 예약 성공 후 함께 교체한다.
        if not departures:
            self._finish_route(robot)
        else:
            robot["route"] = {
                "node_ids": node_ids,
                "edge_ids": edge_ids,
                "waypoint_yaws": waypoint_yaws,
                "segment_index": 0,
                "phase": "ready",
                "departure_at": departure,
                "segment_departures": departures,
                "route_source": "llm" if robot["robot_id"] in self._required_paths else "algorithm",
            }
            robot["status"] = "WAITING" if departure > now else "NAVIGATING"
        return True

    def _mapf_zone_waiting(self, robot):
        """KEV의 WAIT는 해당 공유 구간 진입에만 적용하며 작업 완료를 기다리지 않는다."""
        route = robot.get("route") or {}
        nodes = route.get("node_ids") or list(self._required_paths.get(robot["robot_id"], ()))
        index = route.get("segment_index", 0)
        if index + 1 >= len(nodes):
            return False
        next_resources = {node_key(nodes[index+1]), edge_key(nodes[index], nodes[index+1])}
        for dependency in robot.get("mapf_wait_for", []):
            resources = {tuple(r) for r in dependency["resources"]}
            if not next_resources & resources:
                continue
            high = self._robots.get(dependency["robot_id"])
            if not high or high.get("navigation_id") != dependency["navigation_id"] or high.get("goal_node") is None:
                continue
            high_route = high.get("route") or {}
            remaining = (high_route.get("node_ids") or list(self._required_paths.get(high["robot_id"], ())))[
                high_route.get("segment_index", 0):
            ]
            future_nodes = remaining[1:] if high.get("occupied_edge") is not None else remaining
            future = {node_key(n) for n in future_nodes} | {edge_key(a, b) for a, b in zip(remaining, remaining[1:])}
            if not remaining or future & resources & next_resources:
                return True
        return False

    # 대피 도착은 원래 명령의 완료가 아니므로, 실제 점유를 유지
    # 도착한 로봇의 경로 예약을 완료 처리한다.
    def _finish_route(self, robot):
        robot["route"] = None
        self._release_schedule(robot)
        if robot.get("kev_maneuver"):
            robot["kev_maneuver"]["phase"] = "ASIDE"
            robot["status"] = "WAITING"
            self._pending.pop(robot["robot_id"], None)
        elif robot["robot_id"] in self._concession:
            robot["status"] = "WAITING"
            self._pending.setdefault(robot["robot_id"], None)
        else:
            self._required_paths.pop(robot["robot_id"], None)
            robot.pop("mapf_wait_for", None)
            robot["goal_node"] = None
            robot["request_order"] = None
            robot["status"] = "IDLE"

    # 양보 후보 검증은 실제 로봇/공용 예약표를 변경하지 않음
    # 경로 계획에 사용할 로봇 상태 사본을 만든다.
    def _planning_copy(self):
        trial = copy(self)
        trial.lock = RLock()
        trial._robots = deepcopy(self._robots)
        trial._required_paths = dict(self._required_paths)
        trial._pending = dict(self._pending)
        trial._concession = deepcopy(self._concession)
        trial._protected_resources = deepcopy(self._protected_resources)
        trial._reservations = ReservationTable()
        rows = self._reservations.snapshot()
        for robot_id, navigation_id in {(r.robot_id, r.navigation_id) for r in rows}:
            trial._reservations.try_commit(
                robot_id,
                navigation_id,
                tuple(r for r in rows if (r.robot_id, r.navigation_id) == (robot_id, navigation_id)),
            )
        return trial

    # 진행 중인 양보는 완료/취소까지 유지하여 왕복 양보를 방지
    # 로봇 간 교착 상태를 해소한다.
    def _resolve_deadlock(self, now, graph, planner):
        if self._concession or not self._pending:
            return
        # LLM 경로는 양보 경로로 바꾸지 않고 동일 경로의 예약을 기다린다.
        waiting = sorted(
            (rid for rid in self._pending if rid not in self._required_paths),
            key=lambda rid: self._robots[rid]["request_order"],
        )
        parked = [
            rid
            for rid, robot in self._robots.items()
            if robot["status"] == "IDLE"
            and robot["goal_node"] is None
            and robot["route"] is None
            and not robot["stop_requested"]
        ]
        occupied = {rid: occupied_resource(self._robots[rid], graph) for rid in waiting}
        dependencies = {}
        for rid in waiting:
            trial = self._planning_copy()
            for other in waiting:
                if other != rid:
                    trial._robots.pop(other)
                    trial._release_schedule(self._robots[other])
            robot = trial._robots[rid]

            # 현재 검사 방식에서 교착 관계를 확인하지 못한 경우 (더 있을 수도..)
            if not trial._try_schedule(robot, now, graph, planner) or robot["route"] is None:
                dependencies[rid] = set()
                continue
            path = robot["route"]["node_ids"]
            resources = {node_key(n) for n in path}
            resources.update(edge_key(a, b) for a, b in zip(path, path[1:]))
            dependencies[rid] = {other for other in waiting if other != rid and occupied[other] in resources}

        # 목적지에 도달할 수 있는지 확인한다.
        def reachable(start):
            seen, todo = set(), list(dependencies[start])
            while todo:
                rid = todo.pop()
                if rid not in seen:
                    seen.add(rid)
                    todo.extend(dependencies[rid] - seen)
            return seen

        reach = {rid: reachable(rid) for rid in waiting}
        for priority in waiting:
            cycle = [rid for rid in waiting if rid != priority and rid in reach[priority] and priority in reach[rid]]
            # 이동 요청이 없어도 이 로봇의 점유를 제외하면 길이 열리는지 검사
            blockers = []
            for rid in parked:
                trial = self._planning_copy()
                trial._robots.pop(rid)
                trial._release_schedule(self._robots[rid])
                if trial._try_schedule(trial._robots[priority], now, graph, planner):
                    blockers.append(rid)
            if not cycle and not blockers:
                continue
            # 같은 교착 집합에서는 최초 명령이 우선이며 후순위만 양보
            if any(self._robots[rid]["request_order"] < self._robots[priority]["request_order"] for rid in cycle):
                continue
            candidates = []
            for concession in [*reversed(cycle), *blockers]:
                robot = self._robots[concession]
                for node in planner.nodes:
                    if node == robot["occupied_node"]:
                        continue
                    trial = self._planning_copy()
                    trial._concession[concession] = {
                        "node": node,
                        "priority": priority,
                        "navigation_id": self._robots[priority]["navigation_id"],
                    }
                    mover = trial._robots[concession]
                    if mover["goal_node"] is None:
                        mover["navigation_id"] = uuid4().hex
                    if not trial._try_schedule(mover, now, graph, planner):
                        continue
                    route = mover["route"]
                    if route is None or not trial._try_schedule(trial._robots[priority], now, graph, planner):
                        continue
                    # 유휴 로봇은 대피 지점에서 정차하므로 원래 목적지 재개 검사가 없다.
                    if robot["goal_node"] is not None:
                        # 우선 로봇의 목적지 정차 이후에도 원래 목적지로 복귀 가능해야 한다.
                        resume = trial._planning_copy()
                        leader = resume._robots[priority]
                        leader["x"], leader["y"] = planner.nodes[leader["goal_node"]]
                        leader["occupied_node"] = leader["goal_node"]
                        leader["occupied_edge"] = None
                        leader["route"] = None
                        resume._release_schedule(leader)
                        follower = resume._robots[concession]
                        follower["x"], follower["y"] = planner.nodes[node]
                        follower["occupied_node"], follower["occupied_edge"] = node, None
                        follower["route"] = None
                        resume._concession.pop(concession)
                        resume._release_schedule(follower)
                        can_resume = resume._try_schedule(follower, now, graph, planner)
                        remaining = [rid for rid in cycle if rid != concession]

                        while not can_resume and remaining:
                            progressed = False
                            for rid in list(remaining):
                                other = resume._robots[rid]
                                goal = other["goal_node"]
                                if not resume._try_schedule(other, now, graph, planner):
                                    continue
                                other["x"], other["y"] = planner.nodes[goal]
                                other["occupied_node"], other["occupied_edge"] = goal, None
                                other["route"] = None
                                resume._release_schedule(other)
                                remaining.remove(rid)
                                progressed = True
                            can_resume = resume._try_schedule(follower, now, graph, planner)
                            if not progressed:
                                break
                        if not can_resume:
                            continue
                    points = [(robot["x"], robot["y"])] + [planner.nodes[n] for n in route["node_ids"][1:]]
                    distance = sum(math.dist(a, b) for a, b in zip(points, points[1:]))
                    candidates.append(
                        (distance, -(robot["request_order"] or 0), node, concession, trial._concession[concession])
                    )
            for _, _, _, concession, maneuver in sorted(candidates):
                robot = self._robots[concession]
                previous_navigation = robot["navigation_id"]
                if robot["goal_node"] is None:
                    robot["navigation_id"] = uuid4().hex
                self._concession[concession] = maneuver
                if self._try_schedule(robot, now, graph, planner):
                    self._pending.pop(concession, None)
                    return
                self._concession.pop(concession)
                robot["navigation_id"] = previous_navigation

    # 대기 중인 로봇의 경로 예약을 다시 시도한다.
    def _retry_waiting(self, now, graph, planner):
        for rid, maneuver in list(self._concession.items()):
            leader = self._robots[maneuver["priority"]]
            if leader["goal_node"] is None or leader["navigation_id"] != maneuver["navigation_id"]:
                # 우선 명령 완료/취소/교체 시 현재 위치에서 원래 목적지를 재개
                self._concession.pop(rid)
                robot = self._robots[rid]
                if robot["goal_node"] is None:
                    self._finish_route(robot)
                    self._pending.pop(rid, None)
                else:
                    self._wait_for_reservation(robot)
        changed = True
        while changed and self._pending:
            changed = False
            pending_ids = list(
                sorted(
                    self._pending,
                    key=lambda rid: (
                        not any(
                            str(self._robots[o].get("goal_node")) == str(self._robots[rid].get("occupied_node"))
                            for o in self._pending
                            if o != rid
                        ),
                        -int(self._robots[rid].get("task_priority", 0)),
                        self._robots[rid].get("request_order") or math.inf,
                    ),
                )
            )
            for robot_id in pending_ids:
                if robot_id not in self._pending:
                    continue
                robot = self._robots[robot_id]
                if robot["goal_node"] is None and robot_id not in self._concession:
                    self._pending.pop(robot_id, None)
                    changed = True
                elif robot_id in self._concession and robot["occupied_node"] == self._concession[robot_id]["node"]:
                    continue
                elif self._try_schedule(robot, now, graph, planner):
                    self._pending.pop(robot_id, None)
                    changed = True
        self._resolve_deadlock(now, graph, planner)
        self._resolve_required_path_cycles(now, graph, planner)

    # AI 고정 경로(required_path) 로봇들이 서로의 초기 점유 노드를 목적지로 하여 맞물린 교착을 해소한다.
    def _resolve_required_path_cycles(self, now, graph, planner):
        pending_reqs = [
            rid
            for rid in self._pending
            if rid in self._required_paths
            and not self._robots[rid].get("kev_maneuver")
            and self._robots[rid]["goal_node"] is not None
            and self._robots[rid]["route"] is None
        ]
        if len(pending_reqs) < 2:
            return

        clearance_time = (self.robot_radius_m * 2) / self.speed_mps

        for starter_id in sorted(pending_reqs, key=lambda r: self._robots[r]["request_order"] or 0):
            trial = self._planning_copy()
            vacating_nodes = {}
            for rid in pending_reqs:
                req = self._required_paths.get(rid)
                occ = self._robots[rid]["occupied_node"]
                if req and len(req) > 1 and req[0] == occ:
                    vacating_nodes[occ] = now + clearance_time

            starter = trial._robots[starter_id]
            if not trial._try_schedule(starter, now, graph, planner, vacating_nodes=vacating_nodes):
                continue

            trial._pending.pop(starter_id, None)
            remaining = [rid for rid in pending_reqs if rid != starter_id]
            all_ok = True
            for rid in remaining:
                if not trial._try_schedule(trial._robots[rid], now, graph, planner, vacating_nodes=vacating_nodes):
                    all_ok = False
                    break
                trial._pending.pop(rid, None)

            if all_ok:
                for rid in pending_reqs:
                    self._robots[rid].update(trial._robots[rid])
                    batch_rows = [r for r in trial._reservations.snapshot() if r.robot_id == rid]
                    self._reservations.replace_request(rid, self._robots[rid]["navigation_id"], batch_rows)
                    self._pending.pop(rid, None)
                return
