"""교통조정안 테스트 계약 v0.1: 정지한 로봇들의 경로와 상태를 검증한다.

기존 주문/ROS 흐름에는 자동 연결하지 않는다. 경로 변경 경로는 호출자가
제공하고, 모든 조정안은 기존 TrafficManager의 예약·구간 허가로 사전 검사한다.
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import asdict, dataclass
import hashlib
import json
import math
from typing import Any

from .pathfinding import DistanceAStar
from .reservation import edge_key
from .traffic_manager import TrafficManager
from .traffic_metrics import estimate_plan

SCHEMA_VERSION = "traffic-situation/0.1"
SELECTION_OBJECTIVE = "MIN_MEAN_GOAL_COMPLETION_V1"
CHOICE_INSTRUCTIONS = (
    "Select one complete joint action from the offered candidates. All movement plans "
    "passed a stopped-state reservation precheck. Primary objective: MINIMIZE "
    "estimated_metrics.mean_goal_completion_seconds. Smaller is better. This mean "
    "includes ONLY robots with has_movement_request=true; a parked robot's return "
    "is not an extra task completion. Do NOT prefer total distance or all_steps_completion_seconds "
    "over a smaller mean_goal_completion_seconds. Null is unresolved, not zero cost. "
    "When the mean is equal, prefer a smaller max_turn_wait_seconds, then smaller "
    "total_distance_m, then earlier request_order and larger waiting_seconds. "
    "Steps execute in listed order; a later step waits for the previous step to finish. "
    "Estimates use constant speed and exclude inference, turns and reservation delays. "
    "Choose based on these values, not the candidate ID or its position in the list."
)


def fingerprint(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
    ).hexdigest()


def node_id(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value):
        raise ValueError("노드 ID는 비어 있지 않은 문자열 또는 정수여야 합니다.")
    return str(value)


@dataclass(frozen=True)
class Step:
    robot_id: str
    action: str
    path: tuple[str, ...]


@dataclass(frozen=True)
class JointAction:
    id: str
    kind: str
    steps: tuple[Step, ...]
    initial_waits: tuple[tuple[str, str], ...]

    def to_dict(self) -> dict:
        # steps 순서가 선행 조건이다. YIELD 완료 전에 다른 로봇을 출발시키지 않는다.
        return {
            "id": self.id,
            "kind": self.kind,
            "steps": [asdict(step) for step in self.steps],
            "initial_waits": [
                {"robot_id": rid, "action": "WAIT", "wait_node": node}
                for rid, node in self.initial_waits
            ],
            "release_rule": "PREVIOUS_STEP_COMPLETED",
        }


@dataclass(frozen=True)
class TrafficDecision:
    snapshot_hash: str
    joint_action_id: str
    selection_source: str
    model_result: dict

    def to_dict(self) -> dict:
        return {**asdict(self), "fms_execution_approved": False}


class TrafficSituation:
    """팀 합의 전의 테스트용 입력. 이동 중/활성 예약 입력은 명시적으로 거부."""

    def __init__(self, payload: dict, *, recovery_selector=None, recovery_cancelled=None):
        self._source = payload
        self.snapshot_hash = fingerprint(payload)
        self.data = deepcopy(payload)
        if self.data.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(f"schema_version은 {SCHEMA_VERSION}이어야 합니다.")
        if self.data.get("mode") != "simulation":
            raise ValueError("v0.1은 격리 시뮬레이션만 지원합니다.")
        if not isinstance(self.data.get("snapshot_id"), str) or not self.data["snapshot_id"]:
            raise ValueError("snapshot_id가 필요합니다.")
        self.graph = self.data["graph"]
        self.planner = DistanceAStar(self.graph)
        self.config = self.data["config"]
        if not isinstance(self.config.get("recovery_motion_estimates", False), bool):
            raise ValueError("recovery_motion_estimates는 true 또는 false여야 합니다.")
        self.allow_proactive_hold = self.config.get("allow_proactive_hold", False)
        if not isinstance(self.allow_proactive_hold, bool):
            raise ValueError("allow_proactive_hold는 true 또는 false여야 합니다.")
        if self.config.get("reservations") != []:
            raise ValueError("v0.1은 활성 예약이 없는 정지 상태만 지원합니다.")
        self.wait_nodes = {node_id(n) for n in self.config["wait_allowed_nodes"]}
        self.blocked_nodes = {node_id(n) for n in self.config.get("blocked_nodes", [])}
        self.blocked_edges = {
            edge_key(*pair) for pair in self.config.get("blocked_edges", [])
        }
        if not (self.wait_nodes | self.blocked_nodes) <= self.planner.nodes.keys():
            raise ValueError("대기/차단 노드가 지도에 없습니다.")
        self.requests: dict[str, dict] = {}
        occupied = {}
        for raw in self.data["route_requests"]:
            order = raw["order"]
            rid = order["robot_id"]
            if not isinstance(rid, str) or not rid or rid in self.requests:
                raise ValueError("로봇 ID는 비어 있지 않고 중복되지 않아야 합니다.")
            if not order.get("selector") or not order.get("model"):
                raise ValueError("각 요청의 selector/model이 필요합니다.")
            if (
                raw.get("status") != "IDLE"
                or raw.get("occupied_edge") is not None
                or raw.get("navigation_id") is not None
                or raw.get("route") is not None
                or raw.get("goal_node") is not None
            ):
                raise ValueError("v0.1은 노드에서 정지한 IDLE 로봇만 지원합니다.")
            start, goal = node_id(raw["current_node"]), node_id(raw["node"])
            if start in occupied:
                raise ValueError("초기 노드 중복 점유입니다.")
            if start not in self.wait_nodes:
                raise ValueError("초기 정차 노드를 대기 허용 노드로 명시하세요.")
            path = self.check_path(raw["result"]["path"], start=start, goal=goal)
            request_order = raw["request_order"]
            waiting_seconds = raw["waiting_seconds"]
            if (
                isinstance(request_order, bool)
                or not isinstance(request_order, int)
                or request_order < 0
                or isinstance(waiting_seconds, bool)
                or not isinstance(waiting_seconds, (int, float))
                or not math.isfinite(waiting_seconds)
                or waiting_seconds < 0
            ):
                raise ValueError("통행 요청 순서·대기 시간이 올바르지 않습니다.")
            occupied[start] = rid
            self.requests[rid] = {
                **raw, "current_node": start, "node": goal, "path": path,
            }
        if not self.requests:
            raise ValueError("로봇이 한 대 이상 필요합니다.")
        expected = {f"node:{node}": rid for node, rid in occupied.items()}
        if self.config.get("zone_occupancy") != expected:
            raise ValueError("zone_occupancy와 현재 노드 점유가 일치하지 않습니다.")
        self.fixed_plan = self.config.get("fixed_plan")
        self.recovery_search = self.config.get("recovery_search")
        self.recovery_strategy = self.config.get("recovery_strategy", "BREADTH_FIRST")
        self.search_report = None
        if self.fixed_plan is not None and self.recovery_search is not None:
            raise ValueError("고정 계획과 자동 탐색을 동시에 지정할 수 없습니다.")
        if self.recovery_search is not None:
            from .traffic_recovery_search import search_recovery_plan

            plan, self.search_report = search_recovery_plan(
                self, self.recovery_search, strategy=self.recovery_strategy, selector=recovery_selector,
                guidance_options=self.config.get("recovery_guidance"), cancelled=recovery_cancelled,
                branch_policy=self.config.get("recovery_branch_policy", "DISCARD"),
                guidance_budget_policy=self.config.get("recovery_guidance_budget_policy", "STOP"),
            )
            self.plans = {plan.id: plan} if plan else {"HOLD_ALL": self._plan("HOLD_ALL", "HOLD", [])}
            self.rejected = {}
        else:
            self.plans, self.rejected = (
                self._build_fixed_plan() if self.fixed_plan is not None else self._build_plans()
            )
        self.estimates = {
            key: estimate_plan(plan, self.requests, self.planner.nodes)
            for key, plan in self.plans.items()
        }

    def check_path(self, values, *, start, goal) -> tuple[str, ...]:
        if not isinstance(values, (list, tuple)) or not values:
            raise ValueError("경로는 비어 있지 않은 노드 목록이어야 합니다.")
        path = tuple(node_id(n) for n in values)
        if path[0] != start or path[-1] != goal:
            raise ValueError("경로의 출발·목적지가 요청과 다릅니다.")
        if any(n not in self.planner.nodes for n in path):
            raise ValueError("경로에 지도에 없는 노드가 있습니다.")
        pairs = list(zip(path, path[1:]))
        if any(not any(b == n for n, _ in self.planner.edges[a]) for a, b in pairs):
            raise ValueError("경로에 존재하지 않는 유향 엣지가 있습니다.")
        if len(set(pairs)) != len(pairs) or max(Counter(path).values()) > 2:
            raise ValueError("경로 방문 제한 위반입니다.")
        return path

    def robot_states(self) -> dict[str, dict]:
        states = {}
        for rid, request in self.requests.items():
            start = request["current_node"]
            x, y = self.planner.nodes[start]
            states[rid] = dict(
                robot_id=rid, x=x, y=y, yaw=0.0, status="IDLE",
                current_node=start, occupied_node=start, occupied_edge=None,
                route=None, goal_node=None, navigation_id=None,
                request_order=None, stop_requested=False,
            )
        return states

    def assert_fresh(self):
        if fingerprint(self._source) != self.snapshot_hash:
            raise ValueError("STALE_INPUT: 입력이 판단 도중 변경되었습니다.")

    def _plan(self, key, kind, steps):
        first = steps[0].robot_id if steps else None
        return JointAction(
            key, kind, tuple(steps),
            tuple((rid, r["current_node"]) for rid, r in self.requests.items() if rid != first),
        )

    def _build_fixed_plan(self):
        """시험에 명시된 한 계획도 전체 예약·복귀 검사를 통과해야 실행한다."""
        raw = self.fixed_plan
        if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not raw["id"] or raw["id"] == "HOLD_ALL":
            raise ValueError("고정 계획 ID가 올바르지 않습니다.")
        rows = raw.get("steps")
        if not isinstance(rows, list) or not 1 <= len(rows) <= 32:
            raise ValueError("고정 계획은 1~32단계여야 합니다.")
        steps = []
        for row in rows:
            if not isinstance(row, dict) or row.get("robot_id") not in self.requests:
                raise ValueError("고정 계획의 로봇 ID가 올바르지 않습니다.")
            if row.get("action") not in ("YIELD", "PROCEED", "REROUTE"):
                raise ValueError("고정 계획의 이동 동작이 올바르지 않습니다.")
            path = row.get("path")
            if not isinstance(path, (list, tuple)) or len(path) < 2:
                raise ValueError("고정 계획의 이동 경로가 필요합니다.")
            path = tuple(node_id(n) for n in path)
            if row["action"] == "YIELD" and path[-1] not in self.wait_nodes:
                raise ValueError("양보 목적지는 대기 허용 노드여야 합니다.")
            steps.append(Step(row["robot_id"], row["action"], path))
        plan = self._plan(raw["id"], "FIXED_SCENARIO", steps)
        reason = self._preflight(plan)
        if reason is not None:
            raise ValueError(f"고정 계획 사전 검사 실패: {reason}")
        return {plan.id: plan}, {}

    def _build_plans(self):
        moving = sorted(
            (rid for rid, r in self.requests.items() if r["current_node"] != r["node"]),
            key=lambda rid: (self.requests[rid]["request_order"], rid),
        )
        proposed = []
        # 모든 순열 대신, 첫 진행 대상 N개 + 이후 FIFO인 N개 조정안으로 제한한다.
        for index, first in enumerate(moving, 1):
            sequence = [first, *(rid for rid in moving if rid != first)]
            proposed.append(self._plan(
                f"A{index}", "PATH_KEEPING",
                [Step(rid, "PROCEED", self.requests[rid]["path"]) for rid in sequence],
            ))
        alternates = self.config.get("reroute_paths", {})
        if alternates:
            if not set(alternates) <= set(moving):
                raise ValueError("우회 경로 대상은 이동 요청이 있는 로봇이어야 합니다.")
            steps = []
            for rid in moving:
                request = self.requests[rid]
                path = request["path"]
                if rid in alternates:
                    path = self.check_path(
                        alternates[rid], start=request["current_node"], goal=request["node"]
                    )
                    if path == request["path"]:
                        raise ValueError("우회 경로가 기존 경로와 같습니다.")
                steps.append(Step(rid, "REROUTE" if rid in alternates else "PROCEED", path))
            proposed.append(self._plan("B_REROUTE", "PATH_CHANGING", steps))
        for rid, supplied in self.config.get("yield_paths", {}).items():
            if rid not in self.requests:
                raise ValueError("양보 대상 로봇이 없습니다.")
            request = self.requests[rid]
            out = tuple(node_id(n) for n in supplied["out"])
            if len(out) < 2 or out[-1] not in self.wait_nodes:
                raise ValueError("양보 이동의 도착점은 별도 대기 노드여야 합니다.")
            out = self.check_path(out, start=request["current_node"], goal=out[-1])
            resume = self.check_path(supplied["resume"], start=out[-1], goal=request["node"])
            steps = [Step(rid, "YIELD", out)]
            steps.extend(
                Step(other, "PROCEED", self.requests[other]["path"])
                for other in moving if other != rid
            )
            if len(resume) > 1:
                steps.append(Step(rid, "PROCEED", resume))
            proposed.append(self._plan(f"B_YIELD_{rid}", "PATH_CHANGING", steps))
        feasible, rejected = {}, {}
        for plan in proposed:
            reason = self._preflight(plan)
            if reason is None:
                feasible[plan.id] = plan
            else:
                rejected[plan.id] = reason
        # 진행 가능한 조정안이 있으면 그중에서 선택한다. 모델 응답을 사후에
        # 덮어쓰지 않고 호출 전에 선택지를 제한한다. 이동 불가 시 대기는 유지한다.
        if not feasible or self.allow_proactive_hold:
            hold = self._plan("HOLD_ALL", "HOLD", [])
            feasible[hold.id] = hold
        return feasible, rejected

    def _preflight(self, plan: JointAction, *, require_goal_restoration=True) -> str | None:
        """복사본에서 예약·진입 검사. 탐색 중간 경로만 최종 복귀 검사를 보류한다.

        실행할 완성 계획은 기본값 True로 다시 검사한다.
        """
        states = self.robot_states()
        traffic = TrafficManager(states, speed_mps=0.2, safety_margin=0.2)
        now = 1.0
        try:
            for step in plan.steps:
                if any(n in self.blocked_nodes for n in step.path):
                    return "BLOCKED_NODE"
                if any(edge_key(a, b) in self.blocked_edges for a, b in zip(step.path, step.path[1:])):
                    return "BLOCKED_EDGE"
                robot = states[step.robot_id]
                self.check_path(step.path, start=robot["occupied_node"], goal=step.path[-1])
                traffic.request_navigation_after_stop(
                    step.robot_id, step.path[-1], now=now,
                    graph=self.graph, required_path=step.path,
                )
                if robot["route"] is None and robot["goal_node"] is not None:
                    return "NO_FEASIBLE_RESERVATION"
                for index, (a, b) in enumerate(zip(step.path, step.path[1:])):
                    departure = robot["route"]["segment_departures"][index]
                    arrival = departure + math.dist(self.planner.nodes[a], self.planner.nodes[b]) / 0.2
                    permission = traffic.check_segment_permission(
                        step.robot_id, now=departure + 1e-8,
                        expected_arrival_at=arrival, graph=self.graph,
                    )
                    if permission.decision != "allowed":
                        return permission.reason
                    traffic.confirm_edge_occupancy(step.robot_id)
                    robot["x"], robot["y"] = self.planner.nodes[b]
                    traffic.confirm_node_arrival(step.robot_id, b)
                    now = arrival + 0.5
            if require_goal_restoration and any(states[rid]["occupied_node"] != r["node"] for rid, r in self.requests.items()):
                return "ORIGINAL_GOAL_NOT_RESTORED"
        except (ValueError, KeyError, TypeError) as error:
            return str(error)
        return None

    def preview(self) -> dict:
        self.assert_fresh()
        return {
            "schema_version": SCHEMA_VERSION,
            "snapshot_id": self.data["snapshot_id"],
            "snapshot_hash": self.snapshot_hash,
            "policy": {
                "plan_source": "RECOVERY_SEARCH" if self.recovery_search is not None else (
                    "FIXED_SCENARIO" if self.fixed_plan is not None else "GENERATED_CANDIDATES"
                ),
                "allow_proactive_hold": self.allow_proactive_hold,
                "progress_rule": "SELECT_FEASIBLE_MOVEMENT_ELSE_HOLD",
                "selection_objective": self.search_report["objective"] if self.search_report else SELECTION_OBJECTIVE,
                "long_wait_protection": "NONE" if self.search_report else "TIE_BREAK_ONLY_NO_MAX_WAIT_GUARANTEE",
            },
            "joint_actions": [p.to_dict() for p in self.plans.values()],
            "candidate_estimates": deepcopy(self.estimates),
            "rejected_joint_actions": self.rejected,
            "recovery_search": deepcopy(self.search_report),
            "fms_execution_approved": False,
        }

    def _request_sequence(self, plan):
        """양보 준비 동작은 건너뛰고 원래 이동 요청을 완료시키는 순서를 비교한다."""
        ranked = sorted(
            (rid for rid, r in self.requests.items() if r["current_node"] != r["node"]),
            key=lambda rid: (self.requests[rid]["request_order"], -self.requests[rid]["waiting_seconds"], rid),
        )
        rank = {rid: index for index, rid in enumerate(ranked)}
        sequence = []
        for step in plan.steps:
            if (step.robot_id in rank and step.action != "YIELD"
                    and step.path[-1] == self.requests[step.robot_id]["node"]
                    and rank[step.robot_id] not in sequence):
                sequence.append(rank[step.robot_id])
        return tuple(sequence) if sequence else (math.inf,)

    def _baseline_choice(self, strategy):
        def key(plan):
            e = self.estimates[plan.id]
            if e["mean_goal_completion_seconds"] is None:
                return (math.inf,)
            if strategy == "request_order":
                return (0, self._request_sequence(plan), plan.kind != "PATH_KEEPING",
                        e["total_distance_m"], plan.id)
            return (0, e["mean_goal_completion_seconds"], e["max_turn_wait_seconds"],
                    e["total_distance_m"], self._request_sequence(plan), plan.id)
        return min(self.plans.values(), key=key).id

    def decide(self, selector=None, *, strategy="kev") -> TrafficDecision:
        self.assert_fresh()
        if strategy not in ("kev", "request_order", "min_mean_completion"):
            raise ValueError(f"알 수 없는 선택 방식: {strategy}")
        result = {}
        if len(self.plans) == 1:
            choice = next(iter(self.plans))
            source = ("KEV_GUIDED_SEARCH" if self.search_report["guidance_source"] == "KEV" else "TEST_GUIDED_SEARCH") if (
                self.search_report and self.search_report.get("guidance_source")
            ) else "CODE_SEARCH" if self.recovery_search is not None else (
                "FIXED_SCENARIO" if self.fixed_plan is not None else "SINGLE_OPTION"
            )
            if self.search_report and self.search_report.get("code_after_model_budget_decisions", 0):
                source = "HYBRID_KEV_CODE_SEARCH" if self.search_report["guidance_source"] == "KEV" else "HYBRID_TEST_CODE_SEARCH"
        elif strategy != "kev":
            choice, source = self._baseline_choice(strategy), strategy.upper()
            result = {
                "strategy": strategy,
                "selection_objective": "REQUEST_ORDER_LEXICOGRAPHIC" if strategy == "request_order" else SELECTION_OBJECTIVE,
            }
        else:
            if selector is None:
                raise ValueError("여러 교통조정안 중 선택할 selector가 필요합니다.")
            result = selector.select_choice(
                self.data,
                {key: json.dumps({**plan.to_dict(), "estimated_metrics": self.estimates[key]}, ensure_ascii=False)
                 for key, plan in self.plans.items()},
                CHOICE_INSTRUCTIONS,
                question_id="traffic_joint_action",
            )
            choice = result.get("choice")
            source = "KEV" if getattr(selector, "name", None) == "kev" else "TEST_DOUBLE"
        self.assert_fresh()
        if choice not in self.plans:
            raise ValueError(f"알 수 없는 교통조정안: {choice}")
        return TrafficDecision(self.snapshot_hash, choice, source, deepcopy(result))

    def checked_plan(self, decision: TrafficDecision) -> JointAction:
        self.assert_fresh()
        if decision.snapshot_hash != self.snapshot_hash:
            raise ValueError("STALE_DECISION: 다른 입력 스냅샷의 결정입니다.")
        if decision.joint_action_id not in self.plans:
            raise ValueError("알 수 없는 교통조정안입니다.")
        plan = self.plans[decision.joint_action_id]
        if plan.steps and self._preflight(plan) is not None:
            raise ValueError("교통조정안 재검사에 실패했습니다.")
        return plan
