"""전략 선택→경로 생성→검사를 코드/모델에 같은 조건으로 제공하는 격리 탐색.

일반 주문 서비스에 연결하지 않는다. 첫 유효 계획에서 끝내지 않고 한도 안에서
찾은 완성 계획의 평균 이동 완료 시간을 비교한다. 전역 최적해를 보장하지 않는다.
"""

from copy import deepcopy
from dataclasses import asdict, dataclass
import json
import math
from time import monotonic

from backend.app.services.route_service import plan_route
from backend.app.services.traffic_joint_actions import Step, TrafficSituation
from backend.app.services.traffic_metrics import estimate_plan
from backend.app.services.traffic_recovery_guidance import bounded_choice


STRATEGY_INSTRUCTIONS = (
    "Choose ONE next traffic strategy to TEST, not a complete validated plan. "
    "KEEP tries the robot's supplied A* path; DETOUR generates a new A* path excluding "
    "the indicated shared resource. Other robots wait at their hypothetical current nodes. "
    "Candidates have NOT passed route generation, occupancy or reservation checks. "
    "Use the graph, hypothetical robot positions, goals, partial plan and recent failures. "
    "Find complete plans delivering ALL moving robots to their goals. Parked robots cannot "
    "be moved in this comparison. Minimize the mean goal completion time of moving robots "
    "among valid plans found within the common budget. A parked robot will not leave by itself. "
    "Invalid strategies are rejected by code; feedback follows. Prefer exploring a different "
    "promising branch after failures. Do not invent IDs, paths or time estimates. "
    "No robot moves until a complete plan passes validation. Candidate order is not quality."
)


@dataclass(frozen=True)
class ComparisonLimits:
    max_checks: int = 128
    max_steps: int = 8
    max_seconds: float = 10.0
    max_route_calls: int = 16
    max_model_calls: int | None = 8
    request_seconds: float = 2.0

    def __post_init__(self):
        for key, upper in (("max_checks", 10000), ("max_steps", 32),
                           ("max_route_calls", 128), ("max_model_calls", 32)):
            value = getattr(self, key)
            if key == "max_model_calls" and value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= upper:
                raise ValueError(f"{key} 한도가 올바르지 않습니다.")
        for key in ("max_seconds", "request_seconds"):
            value = getattr(self, key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= 30:
                raise ValueError(f"{key} 시간 한도가 올바르지 않습니다.")


class LimitReached(RuntimeError):
    pass


class PlanningScene(TrafficSituation):
    """기존 입력 검증·예약 검사는 재사용하고 미리 조정안을 생성하지 않는다."""

    def _build_plans(self):
        return {}, {}


class PlanningBudget:
    def __init__(self, limits, *, clock=monotonic):
        self.limits, self.clock = limits, clock
        self.started = clock()
        self.checks = self.route_calls = self.model_calls = self.forced = 0
        self.attempts, self.calls, self.routes = [], [], []

    def remaining_seconds(self):
        return self.limits.max_seconds - (self.clock() - self.started)

    def guard_time(self):
        if self.remaining_seconds() <= 0:
            raise LimitReached("TIME_LIMIT")

    def check(self, scene, plan, *, complete):
        self.guard_time()
        if self.checks >= self.limits.max_checks:
            raise LimitReached("CHECK_LIMIT")
        self.checks += 1
        reason = scene._preflight(plan, require_goal_restoration=complete)
        self.attempts.append(dict(number=self.checks, complete=complete,
                                 plan=plan.to_dict(), result=reason or "VALID"))
        # 동기 검사는 강제 중단하지 않지만 시간 한도 뒤의 결과는 채택하지 않는다.
        self.guard_time()
        return reason

    def route(self, graph, start, goal, *, selector=None, model=None, traffic=None):
        """세 방식 모두 같은 A*를 사용하며 경로 생성에 모델을 호출하지 않는다."""
        self.guard_time()
        if self.route_calls >= self.limits.max_route_calls:
            raise LimitReached("ROUTE_CALL_LIMIT")
        self.route_calls += 1
        record = dict(start=str(start), goal=str(goal))
        self.routes.append(record)
        try:
            path = plan_route(str(start), str(goal), graph)["node_ids"]
            record["path"] = list(path)
            return [int(node) for node in path]
        except (ValueError, KeyError) as exc:
            record["error"] = str(exc)
            raise
        finally:
            self.guard_time()

    def choose(self, selector, state, candidates, instructions, question_id):
        return self._answer(selector, state, candidates, instructions, question_id)["choice"]

    def rank(self, selector, state, candidates, instructions, question_id):
        """동일 상태의 선택 확률로 시도 순서를 정한다. 실행 가능 순위는 아니다."""
        result = self._answer(selector, state, candidates, instructions, question_id)
        if len(candidates) == 1:
            return list(candidates)
        probabilities = result.get("probabilities")
        valid = isinstance(probabilities, dict) and set(probabilities) == set(candidates)
        if valid:
            values = list(probabilities.values())
            valid = all(not isinstance(p, bool) and isinstance(p, (int, float))
                        and math.isfinite(p) and 0 <= p <= 1 for p in values)
            valid = valid and abs(sum(values) - 1.0) <= 0.01
        if not valid:
            self.calls[-1]["status"] = "INVALID_MODEL_PROBABILITIES"
            raise LimitReached("INVALID_MODEL_PROBABILITIES")
        # 동점은 입력 순서를 유지한다. 확률은 시간·안전성의 보증으로 쓰지 않는다.
        order = sorted(candidates, key=lambda key: -probabilities[key])
        self.calls[-1]["probability_order"] = order
        return order

    def _answer(self, selector, state, candidates, instructions, question_id):
        self.guard_time()
        if not candidates:
            raise ValueError("선택할 전략이 없습니다.")
        if len(candidates) == 1:
            self.forced += 1
            return dict(choice=next(iter(candidates)))
        if self.limits.max_model_calls is not None and self.model_calls >= self.limits.max_model_calls:
            raise LimitReached("MODEL_CALL_LIMIT")
        self.model_calls += 1
        record = dict(number=self.model_calls, question_id=question_id,
                      state=deepcopy(state), criteria=deepcopy(candidates), instructions=instructions)
        self.calls.append(record)
        # 공통 도구의 고정 질문명을 이 비교의 실제 질문명으로 바꾼다.
        class QuestionSelector:
            def select_choice(self, state, criteria, instructions, **kwargs):
                return selector.select_choice(state, criteria, instructions, question_id=question_id)

        started = monotonic()
        status, result = bounded_choice(
            QuestionSelector(), state, candidates, instructions=instructions,
            timeout=min(self.limits.request_seconds, self.remaining_seconds()), cancelled=lambda: False,
        )
        record.update(status=status, result=deepcopy(result), elapsed_seconds=monotonic() - started)
        self.guard_time()
        if status != "RESPONSE":
            raise LimitReached(status)
        choice = result.get("choice") if isinstance(result, dict) else None
        if not isinstance(choice, str) or choice not in candidates:
            record["status"] = "INVALID_MODEL_CHOICE"
            raise LimitReached("INVALID_MODEL_CHOICE")
        inference = getattr(selector, "last_inference", None)
        if inference is not None:
            record["inference"] = deepcopy(inference)
        return result

    def report(self):
        return dict(limits=asdict(self.limits), checks=self.checks, route_calls=self.route_calls,
                    model_calls=self.model_calls, forced_choices=self.forced,
                    planning_seconds=self.clock() - self.started, attempts=self.attempts,
                    model_requests=self.calls, route_requests=self.routes)


def plan_cost(plan, scene):
    metrics = estimate_plan(plan, scene.requests, scene.planner.nodes)
    return (metrics["mean_goal_completion_seconds"], metrics["max_turn_wait_seconds"], metrics["total_distance_m"])


def strategy_options(scene, steps, pending, graph_type, conflict_module):
    """경로를 먼저 만들지 않고 진행 대상과 회피할 공유 구간만 나열한다."""
    positions = {rid: r["current_node"] for rid, r in scene.requests.items()}
    for step in steps:
        positions[step.robot_id] = step.path[-1]
    paths = {rid: [int(n) for n in r["path"]] if rid in pending else [int(positions[rid])]
             for rid, r in scene.requests.items()}
    zones = conflict_module.group_conflict_zones(conflict_module.zone_candidates(paths))
    options = []
    for rid in pending:
        options.append(dict(robot_id=rid, action="KEEP", avoid_zone=None))
        for zid, zone in zones.items():
            if rid in zone["robots"]:
                options.append(dict(robot_id=rid, action="DETOUR", avoid_zone=zid, zone=deepcopy(zone)))
    return options, dict(
        planning_only=True, route_graph=graph_type(scene.graph).compact,
        robots=[dict(robot_id=rid, current_node=positions[rid], goal_node=r["node"],
                     remaining_path=list(r["path"]) if rid in pending else [positions[rid]],
                     has_movement_request=r["current_node"] != r["node"],
                     pending=rid in pending, waiting_seconds=r["waiting_seconds"])
                for rid, r in scene.requests.items()],
        shared_zones=zones, partial_steps=[asdict(step) for step in steps],
        scope="STOPPED_SERIAL_WAIT_KEEP_OR_REROUTE_NO_PARKED_YIELD",
    )


def detour_path(scene, rid, zone, graph_type, budget):
    nodes = set(zone.get("nodes", []))
    edges = {tuple(edge) for edge in zone.get("edges", [])}
    if zone["kind"] == "node":
        nodes.add(zone["node"])
    elif zone["kind"] == "edge":
        edges.add(tuple(zone["edge"]))
    edges |= {(b, a) for a, b in edges}
    r = scene.requests[rid]
    if int(r["current_node"]) in nodes or int(r["node"]) in nodes:
        raise ValueError("우회로 출발·목적지를 제거할 수 없습니다.")
    virtual = graph_type(scene.graph).virtual_graph(nodes, edges)
    path = budget.route(virtual, r["current_node"], r["node"])
    # jhlee 원본의 엄격한 엣지 회피 조건도 공통 적용한다.
    if any(a in path and b in path for a, b in edges):
        raise ValueError("회피한 엣지의 양 끝점이 경로에 함께 남았습니다.")
    return scene.check_path(path, start=r["current_node"], goal=r["node"])


def search_strategies(scene, budget, graph_type, conflict_module, *, selector=None, probability_order=False):
    """동일 DFS 실행기에서 코드 순서와 KEV 제안 순서만 바꾼다."""
    if probability_order and selector is None:
        raise ValueError("확률 순서 탐색에는 모델 선택기가 필요합니다.")
    pending = tuple(sorted(
        (rid for rid, r in scene.requests.items() if r["current_node"] != r["node"]),
        key=lambda rid: (scene.requests[rid]["request_order"], rid),
    ))
    best, valid_count, history, seen_plans = None, 0, [], set()
    first_solution_seconds = None
    rankings = []

    def add_feedback(state):
        state.update(recent_checks=deepcopy(history[-8:]),
                     remaining_seconds=budget.remaining_seconds(),
                     remaining_checks=budget.limits.max_checks - budget.checks,
                     best_found_metrics=None if best is None else estimate_plan(best, scene.requests, scene.planner.nodes))

    def explore(steps, pending):
        nonlocal best, valid_count, first_solution_seconds
        budget.guard_time()
        if len(steps) >= budget.limits.max_steps:
            return
        options, state = strategy_options(scene, steps, pending, graph_type, conflict_module)
        if probability_order and options:
            criteria = {f"S{i + 1}": json.dumps(option, ensure_ascii=False) for i, option in enumerate(options)}
            add_feedback(state)
            order = budget.rank(selector, state, criteria, STRATEGY_INSTRUCTIONS, "traffic_strategy")
            rankings.append(dict(partial_steps=deepcopy(state["partial_steps"]), pending=list(pending),
                                 order=order, strategies={key: json.loads(value) for key, value in criteria.items()},
                                 source="MODEL_PROBABILITIES" if len(options) > 1 else "SINGLE_OPTION",
                                 model_call=budget.model_calls if len(options) > 1 else None,
                                 feedback_count=len(history)))
            options = [options[int(key[1:]) - 1] for key in order]
        while options:
            budget.guard_time()
            index = 0
            if selector is not None and not probability_order:
                criteria = {f"S{i + 1}": json.dumps(option, ensure_ascii=False) for i, option in enumerate(options)}
                add_feedback(state)
                key = budget.choose(selector, state, criteria, STRATEGY_INSTRUCTIONS, "traffic_strategy")
                index = int(key[1:]) - 1
            option = options.pop(index)
            rid, request = option["robot_id"], scene.requests[option["robot_id"]]
            try:
                path = request["path"] if option["action"] == "KEEP" else detour_path(scene, rid, option["zone"], graph_type, budget)
            except (ValueError, KeyError) as exc:
                history.append(dict(strategy=deepcopy(option), result="ROUTE_REJECTED", reason=str(exc)))
                continue
            step = Step(rid, "PROCEED" if option["action"] == "KEEP" else "REROUTE", tuple(path))
            next_steps = (*steps, step)
            signature = tuple((s.robot_id, s.path) for s in next_steps)
            if signature in seen_plans:
                history.append(dict(strategy=deepcopy(option), result="DUPLICATE_PATH_PREFIX"))
                continue
            seen_plans.add(signature)
            remaining = tuple(other for other in pending if other != rid)
            plan = scene._plan("STRATEGY_PLAN", "STRATEGY_SEARCH", next_steps)
            reason = budget.check(scene, plan, complete=not remaining)
            history.append(dict(strategy=deepcopy(option), result=reason or "VALID"))
            if reason:
                continue
            if remaining:
                explore(next_steps, remaining)
            else:
                valid_count += 1
                if first_solution_seconds is None:
                    first_solution_seconds = budget.clock() - budget.started
                if best is None or plan_cost(plan, scene) < plan_cost(best, scene):
                    best = plan

    status = "EXHAUSTED_COMMON_STRATEGIES"
    try:
        explore((), pending)
    except LimitReached as exc:
        status = str(exc)
    return best, dict(stop_reason=status, valid_complete_plans=valid_count,
                     first_solution_seconds=first_solution_seconds, strategy_feedback=history,
                     selection_policy="FROZEN_PROBABILITY_ORDER_PER_PREFIX" if probability_order else (
                         "MODEL_REQUERY_EACH_CHOICE" if selector is not None else "FIXED_CODE_ORDER"),
                     probability_rankings=rankings,
                     optimality_claim=False, **budget.report())
