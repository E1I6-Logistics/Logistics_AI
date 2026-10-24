"""공급 경로에서 이동 선후 관계를 계산한다. 예약 검사·완성 계획 탐색은 하지 않는다."""

from copy import deepcopy
import json
import math
from time import monotonic

from .traffic_recovery_guidance import GuidanceLimits, bounded_choice


INSTRUCTIONS = (
    "Choose ONE planning action. Movement choices have NOT passed reservation checks. "
    "Use dependency_facts: clear current occupants before passage, and do not finish on a node "
    "that another unfinished robot must still visit. A finished robot cannot move again under "
    "this supplied-paths, one-yield-per-robot contract. Restore yielded robots after passage. "
    "BACKTRACK discards only the current hypothetical branch; it never reverses a real robot. "
    "Choose BACKTRACK if the branch has an irreversible blocker, or all offered moves are blocked "
    "or would create a final blocker. Otherwise choose a useful unblocked move. "
    "Facts are path relations, NOT full reservation verdicts or a complete-plan solution. "
    "Code checks every selected movement; no actual robot moves before a complete plan passes. "
    "Find a complete valid plan within the limits. Do not infer preference from IDs or order."
)

DEFER_INSTRUCTIONS = INSTRUCTIONS.replace(
    "BACKTRACK discards only the current hypothetical branch; it never reverses a real robot.",
    "BACKTRACK postpones this hypothetical branch without deleting its untried moves. "
    "It can be resumed later and then cannot be postponed again. It never reverses a real robot."
)

MOTION_INSTRUCTIONS = (
    " Motion estimates use graph coordinates and the stated constant speed. First avoid "
    "current occupants and final blockers, and delay restores that block passage. Among "
    "useful unblocked actions, prefer a smaller "
    "estimated_motion.mean_goal_completion_lower_bound_seconds, then smaller "
    "all_returns_completion_lower_bound_seconds, then travel_seconds. For equal mean bounds "
    "prefer clearing needed nodes and avoid unnecessary early restores. The mean counts ONLY "
    "original movement requests, not parked robots returning. Values are optimistic lower "
    "bounds from the chosen prefix and remaining supplied paths; they ignore other robots' "
    "future prerequisite moves, inference, turns and reservation delays. Null means unknown, "
    "not zero. A smaller estimate proves neither feasibility nor a globally faster plan. "
    "The search still returns its first complete checked plan, not a global time optimum."
)


def dependency_snapshot(scene, robot_ids, stages, choices, moves, *, chosen_steps=()):
    positions, remaining, robots = {}, {}, []
    for rid, stage in zip(robot_ids, stages):
        request = scene.requests[rid]
        outgoing = [m for m in moves if m.step.robot_id == rid and m.from_stage == stage]
        position = (request["current_node"] if stage == 0 else request["node"] if stage == 2 else
                    next(m.step.path[-1] for m in moves if m.step.robot_id == rid and m.step.action == "YIELD"))
        positions[rid] = position
        paths = []
        unfinished = stage == 1 or (stage == 0 and request["current_node"] != request["node"])
        if unfinished:
            for move in outgoing:
                path = list(move.step.path)
                if move.to_stage == 1:
                    resume = next(m for m in moves if m.step.robot_id == rid and m.from_stage == 1)
                    path.extend(resume.step.path[1:])
                paths.append(path)
        required = set.intersection(*(set(path[1:]) for path in paths)) if paths else set()
        remaining[rid] = required
        robots.append(dict(robot_id=rid, current_node=position, original_goal=request["node"],
                           stage=("INITIAL", "YIELDED", "FINISHED")[stage], can_leave=bool(outgoing),
                           unfinished=unfinished, remaining_supplied_paths=paths, must_visit_nodes=sorted(required)))
    irreversible = [dict(blocker=r["robot_id"], node=r["current_node"], blocked_robot=other)
                    for r in robots if not r["can_leave"] for other in robot_ids
                    if other != r["robot_id"] and r["current_node"] in remaining[other]]
    offered = {}
    for index, _ in choices:
        move = moves[index]
        rid, end = move.step.robot_id, move.step.path[-1]
        offered[f"M{index + 1}"] = dict(
            robot_id=rid, action=move.step.action, path=list(move.step.path),
            purpose="RESTORE_ORIGINAL_GOAL" if move.from_stage == 1 else move.step.action,
            current_other_occupants=[dict(robot_id=other, node=node) for node in move.step.path[1:]
                                     for other in robot_ids if other != rid and positions[other] == node],
            finishes_on_required_nodes=[dict(robot_id=other, node=end) for other in robot_ids
                                        if other != rid and move.to_stage == 2 and end in remaining[other]],
            clears_required_nodes=[dict(robot_id=other, node=positions[rid]) for other in robot_ids
                                    if other != rid and positions[rid] != end and positions[rid] in remaining[other]],
        )
    facts = dict(scope="SUPPLIED_PATHS_ONE_YIELD_PER_ROBOT_NOT_RESERVATION_OR_COMPLETION_ORACLE",
                 robots=robots, irreversible_blockers=irreversible, offered_moves=offered)
    if scene.config.get("recovery_motion_estimates", False):
        from .traffic_recovery_motion import add_motion_estimates

        add_motion_estimates(facts, scene, robot_ids, stages, chosen_steps, choices, moves)
    return facts


def dependency_code_choice(facts, *, can_backtrack):
    """같은 관계 정보를 받는 단순 코드 대조군. 뒤의 완성 계획을 탐색해 고르지 않는다."""
    offered = facts["offered_moves"]
    all_bad = all(v["current_other_occupants"] or v["finishes_on_required_nodes"] for v in offered.values())
    if can_backtrack and (facts["irreversible_blockers"] or all_bad):
        return "BACKTRACK"

    def rank(key):
        value = offered[key]
        relation = (bool(value["current_other_occupants"]), bool(value["finishes_on_required_nodes"]),
                    -len(value["clears_required_nodes"]), value["purpose"] == "RESTORE_ORIGINAL_GOAL")
        cost = value.get("estimated_motion")
        if cost is None:
            return (*relation, int(key[1:]))
        times = tuple(cost[k] if cost[k] is not None else math.inf for k in (
            "mean_goal_completion_lower_bound_seconds", "all_returns_completion_lower_bound_seconds", "travel_seconds"
        ))
        return (*relation[:2], times[0], *relation[2:], *times[1:], int(key[1:]))

    return min(offered, key=rank)


class DependencyGuidance:
    def __init__(self, selector, options, *, cancelled):
        self.limits = GuidanceLimits.parse(options)
        if selector is None:
            raise ValueError("KEV 관계 기반 탐색에는 선택기가 필요합니다.")
        if self.limits.input_style != "original":
            raise ValueError("관계 기반 실험은 별도의 고정 입력 형식을 사용합니다.")
        self.selector, self.cancelled = selector, cancelled
        self.calls = []

    def choose(self, scene, robot_ids, stages, steps, choices, moves, *, attempts,
               remaining_seconds, remaining_checks, dependencies, can_backtrack,
               backtrack_effect="DISCARD", deferred_count=0):
        if len(self.calls) >= self.limits.max_calls:
            return "MODEL_CALL_LIMIT", None
        state = dict(schema_version="traffic-recovery-dependencies/0.1", planning_only=True,
                     dependency_facts=deepcopy(dependencies),
                     chosen_prefix=[dict(robot_id=s.robot_id, action=s.action, path=list(s.path)) for s in steps],
                     recent_checks=deepcopy(attempts[-8:]), wait_allowed_nodes=sorted(scene.wait_nodes),
                     blocked_nodes=sorted(scene.blocked_nodes), blocked_edges=[list(e) for e in sorted(scene.blocked_edges)],
                     remaining_checks=remaining_checks, remaining_seconds=max(0, remaining_seconds))
        criteria = {key: json.dumps(value, ensure_ascii=False) for key, value in dependencies["offered_moves"].items()}
        instructions = DEFER_INSTRUCTIONS if backtrack_effect == "DEFER" else INSTRUCTIONS
        if "motion_estimation" in dependencies:
            state["schema_version"] = "traffic-recovery-dependencies/0.2"
            instructions += MOTION_INSTRUCTIONS
        if backtrack_effect == "DEFER":
            state["planning_control"] = dict(backtrack_effect="POSTPONE_WITHOUT_DELETION",
                                             pending_branches=deferred_count,
                                             remaining_model_calls=self.limits.max_calls - len(self.calls))
        if can_backtrack:
            criteria["BACKTRACK"] = ("Postpone this branch, keeping its untried moves for later. No robot movement."
                                     if backtrack_effect == "DEFER" else
                                     "Discard this hypothetical branch and consider another sequence. No robot movement.")
        before = getattr(self.selector, "last_inference", None)
        record = dict(call=len(self.calls) + 1, input_style="dependency_relations", request=dict(
            model=getattr(self.selector, "api_model", None), state=state,
            questions={"traffic_recovery_move": dict(type="choice", instructions=instructions, criteria=criteria)}))
        self.calls.append(record)
        started = monotonic()
        status, result = bounded_choice(self.selector, state, criteria,
                                        timeout=min(self.limits.request_seconds, remaining_seconds),
                                        cancelled=self.cancelled, instructions=instructions)
        record.update(status=status, elapsed_seconds=monotonic() - started)
        if status != "RESPONSE":
            if result is not None:
                record["error"] = str(result)
            return status, None
        record["result"] = deepcopy(result)
        inference = getattr(self.selector, "last_inference", None)
        if inference is not before:
            record["inference"] = deepcopy(inference)
        choice = result.get("choice") if isinstance(result, dict) else None
        if not isinstance(choice, str) or choice not in criteria:
            record.update(status="INVALID_MODEL_CHOICE", error="제시하지 않은 계획 동작")
            return "INVALID_MODEL_CHOICE", None
        if choice == "BACKTRACK":
            record["proposed_control"] = "BACKTRACK"
            return "BACKTRACK", None
        record["proposed_move"] = deepcopy(dependencies["offered_moves"][choice])
        return "RESPONSE", int(choice[1:]) - 1
