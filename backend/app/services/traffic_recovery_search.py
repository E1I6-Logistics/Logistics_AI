"""정지 상태 시험용 한도 있는 양보·복귀 탐색. 실제 로봇에 명령하지 않는다.

입력으로 허용한 경로만 조합한다. BFS는 실행 단계 수 순으로 탐색하며 시간 최적화가
아니다. 같은 움직임/검사/한도를 이후 전략 제안 실험에서도 재사용할 수 있다.
"""

from collections import Counter, deque
from copy import deepcopy
from dataclasses import dataclass
import math
from time import monotonic

from .traffic_joint_actions import JointAction, Step, node_id


@dataclass(frozen=True)
class SearchLimits:
    max_checks: int = 128
    max_steps: int = 8
    max_seconds: float = 1.0

    @classmethod
    def parse(cls, options):
        if not isinstance(options, dict) or set(options) - {"max_checks", "max_steps", "max_seconds"}:
            raise ValueError("복구 탐색 한도 설정이 올바르지 않습니다.")
        values = {**cls().__dict__, **options}
        for key, maximum in (("max_checks", 10000), ("max_steps", 32)):
            value = values[key]
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
                raise ValueError(f"{key}는 1~{maximum} 정수여야 합니다.")
        seconds = values["max_seconds"]
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or not 0 < seconds <= 10:
            raise ValueError("max_seconds는 0초 초과 10초 이하여야 합니다.")
        return cls(**values)


@dataclass(frozen=True)
class RecoveryMove:
    """로봇 하나의 출발 상태→도착 상태. 0=처음, 1=양보 중, 2=완료."""

    robot_index: int
    from_stage: int
    to_stage: int
    step: Step


def allowed_moves(scene, robot_ids):
    """완성된 조정안을 받지 않고 로봇별 진행·우회·양보·복귀 경로를 받는다."""
    moves = []
    alternates = scene.config.get("reroute_paths", {})
    yields = scene.config.get("yield_paths", {})
    if not isinstance(alternates, dict) or not isinstance(yields, dict):
        raise ValueError("우회·양보 경로 설정은 로봇별 묶음이어야 합니다.")
    moving = {rid for rid, r in scene.requests.items() if r["current_node"] != r["node"]}
    if not set(alternates) <= moving or not set(yields) <= set(robot_ids):
        raise ValueError("복구 경로의 로봇 대상이 올바르지 않습니다.")
    for index, rid in enumerate(robot_ids):
        request = scene.requests[rid]
        start, goal = request["current_node"], request["node"]
        if start != goal:
            moves.append(RecoveryMove(index, 0, 2, Step(rid, "PROCEED", request["path"])))
        if rid in alternates:
            path = scene.check_path(alternates[rid], start=start, goal=goal)
            if path == request["path"]:
                raise ValueError("우회 경로가 기존 경로와 같습니다.")
            moves.append(RecoveryMove(index, 0, 2, Step(rid, "REROUTE", path)))
        if rid in yields:
            supplied = yields[rid]
            if not isinstance(supplied, dict) or not isinstance(supplied.get("out"), (list, tuple)) or len(supplied["out"]) < 2:
                raise ValueError("양보 이동 경로가 필요합니다.")
            destination = node_id(supplied["out"][-1])
            if destination == start or destination not in scene.wait_nodes:
                raise ValueError("양보 목적지는 별도의 대기 허용 노드여야 합니다.")
            out = scene.check_path(supplied["out"], start=start, goal=destination)
            resume = scene.check_path(supplied.get("resume"), start=destination, goal=goal)
            moves.extend((RecoveryMove(index, 0, 1, Step(rid, "YIELD", out)),
                          RecoveryMove(index, 1, 2, Step(rid, "PROCEED", resume))))
    return tuple(moves)


def search_recovery_plan(scene, options, *, strategy="BREADTH_FIRST", selector=None,
                         guidance_options=None, cancelled=None, clock=monotonic,
                         branch_policy="DISCARD", guidance_budget_policy="STOP") -> tuple[JointAction | None, dict]:
    """최종 도착·복귀까지 검사한 첫 계획과 탐색 기록을 반환한다.

    시간 한도는 검사 사이에서 확인한다. 이미 진행 중인 동기 예약 검사 하나를
    강제 중단하지 않으며, 검사 후 한도가 지났으면 그 결과로 출발하지 않는다.
    """
    limits = SearchLimits.parse(options)
    relation_mode = strategy in ("DEPENDENCY_DFS", "KEV_DEPENDENCY")
    if strategy not in ("BREADTH_FIRST", "DEPTH_FIRST", "KEV_GUIDED", "DEPENDENCY_DFS", "KEV_DEPENDENCY"):
        raise ValueError("알 수 없는 복구 탐색 방식입니다.")
    if branch_policy not in ("DISCARD", "DEFER") or guidance_budget_policy not in ("STOP", "CODE_CONTINUE"):
        raise ValueError("계획 분기·모델 한도 처리 설정이 올바르지 않습니다.")
    if branch_policy == "DEFER" and not relation_mode:
        raise ValueError("분기 보관은 관계 기반 시험에서만 사용할 수 있습니다.")
    if guidance_budget_policy == "CODE_CONTINUE" and (strategy != "KEV_DEPENDENCY" or branch_policy != "DEFER"):
        raise ValueError("모델 한도 뒤 코드 검토는 분기를 보관하는 KEV 관계 시험에서만 사용할 수 있습니다.")
    cancelled = cancelled or (lambda: False)
    guidance = None
    if strategy == "KEV_GUIDED":
        from .traffic_recovery_guidance import RecoveryGuidance

        guidance = RecoveryGuidance(selector, {} if guidance_options is None else guidance_options, cancelled=cancelled)
    elif strategy == "KEV_DEPENDENCY":
        from .traffic_recovery_dependencies import DependencyGuidance

        guidance = DependencyGuidance(selector, {} if guidance_options is None else guidance_options, cancelled=cancelled)
    started = clock()
    robot_ids = tuple(sorted(scene.requests, key=lambda rid: (scene.requests[rid]["request_order"], rid)))
    moves = allowed_moves(scene, robot_ids)
    initial = (0,) * len(robot_ids)
    # DFS와 KEV는 같은 깊이 우선 실행기를 사용하고 다음 검사 동작 선택만 다르다.
    # BFS도 같은 이동·예약 검사·한도를 쓰되 대기열의 앞쪽 상태부터 확장한다.
    def frame(state, steps):
        return dict(state=state, steps=steps, remaining=list(range(len(moves))), started=False, deferred_once=False)

    queue = deque([frame(initial, ())])
    deferred = deque()
    deferred_total = resumed_total = code_after_model_budget = 0
    visited = {initial}
    checks, expanded, duplicates, peak = 0, 0, 0, 1
    depth_cutoff = False
    failures, attempts = Counter(), []
    forced_moves = 0
    backtracks, dependency_decisions = [], []
    plan_id = {"BREADTH_FIRST": "RECOVERY_BFS", "DEPTH_FIRST": "RECOVERY_DFS", "KEV_GUIDED": "RECOVERY_KEV",
               "DEPENDENCY_DFS": "RECOVERY_DEPENDENCY_DFS", "KEV_DEPENDENCY": "RECOVERY_KEV_DEPENDENCY"}[strategy]

    def finish(status, plan=None):
        return plan, dict(
            algorithm=("KEV_DEPENDENCY_DEPTH_FIRST" if relation_mode else "KEV_GUIDED_DEPTH_FIRST") if guidance else strategy, status=status, checks=checks,
            expanded_states=expanded, visited_states=len(visited), duplicate_states_skipped=duplicates,
            peak_frontier=peak, elapsed_seconds=max(0, clock() - started),
            limits=limits.__dict__, rejected_checks=dict(failures), attempts=attempts,
            trace_truncated=checks > len(attempts), found_steps=len(plan.steps) if plan else None,
            scope="SUPPLIED_PATHS_ONE_YIELD_PER_ROBOT_STOPPED_FLEET",
            objective=("FEWEST_STEPS_FIRST_WITHIN_LIMITS_NOT_MINIMUM_TIME" if strategy == "BREADTH_FIRST"
                       else "FIND_VALID_COMPLETE_PLAN_WITHIN_LIMITS_NOT_MINIMUM_TIME"),
            guidance_source=("KEV" if getattr(selector, "name", None) == "kev" else "TEST_DOUBLE") if guidance else None,
            motion_estimates_enabled=bool(relation_mode and scene.config.get("recovery_motion_estimates", False)),
            model_calls=len(guidance.calls) if guidance else 0,
            guidance_limits=guidance.limits.__dict__ if guidance else None,
            guidance_calls=deepcopy(guidance.calls) if guidance else [],
            forced_moves_without_model=forced_moves,
            dependency_mode=relation_mode, voluntary_backtracks=deepcopy(backtracks),
            dependency_decisions=deepcopy(dependency_decisions),
            duplicate_pruning="DISABLED_FOR_VOLUNTARILY_ABANDONED_BRANCHES" if relation_mode else "GLOBAL_VISITED_STAGES",
            branch_policy=branch_policy, guidance_budget_policy=guidance_budget_policy,
            deferred_branches=deferred_total, resumed_branches=resumed_total,
            pending_deferred_branches=len(deferred),
            code_after_model_budget_decisions=code_after_model_budget,
        )

    def exhausted():
        if cancelled():
            return "CANCELLED"
        if clock() - started >= limits.max_seconds:
            return "TIME_LIMIT"
        if checks >= limits.max_checks:
            return "CHECK_LIMIT"
        return None

    def check(plan, *, complete):
        nonlocal checks
        checks += 1
        reason = scene._preflight(plan, require_goal_restoration=complete)
        if reason:
            failures[reason] += 1
        attempts.append(dict(check=checks, complete_plan=complete,
                             steps=[dict(robot_id=s.robot_id, action=s.action, path=list(s.path)) for s in plan.steps],
                             result=reason or "VALID"))
        if len(attempts) > 128:
            del attempts[0]
        return reason

    def all_arrived(state):
        return all(stage == 2 or (stage == 0 and scene.requests[rid]["current_node"] == scene.requests[rid]["node"])
                   for rid, stage in zip(robot_ids, state))

    if all_arrived(initial):
        return finish("ALREADY_AT_GOALS")
    while queue or deferred:
        if status := exhausted():
            return finish(status)
        if not queue:
            queue.append(deferred.popleft())
            resumed_total += 1
        item = queue[0] if strategy == "BREADTH_FIRST" else queue[-1]
        state, steps = item["state"], item["steps"]
        if not item["started"]:
            expanded += 1
            item["started"] = True
        if len(steps) >= limits.max_steps:
            depth_cutoff = True
            if strategy == "BREADTH_FIRST":
                queue.popleft()
            else:
                queue.pop()
            continue
        choices = []
        for index in list(item["remaining"]):
            move = moves[index]
            if state[move.robot_index] != move.from_stage:
                item["remaining"].remove(index)
                continue
            next_state = list(state)
            next_state[move.robot_index] = move.to_stage
            next_state = tuple(next_state)
            if next_state in visited and not relation_mode:
                duplicates += 1
                item["remaining"].remove(index)
                continue
            choices.append((index, next_state))
        if not choices:
            if strategy == "BREADTH_FIRST":
                queue.popleft()
            else:
                queue.pop()
            continue
        selected = choices[0][0]
        # 보관했다가 재개한 같은 분기를 다시 미루면 진전 없는 순환이 생긴다.
        can_backtrack = relation_mode and bool(steps) and not (branch_policy == "DEFER" and item["deferred_once"])
        dependencies = None
        if relation_mode:
            from .traffic_recovery_dependencies import dependency_code_choice, dependency_snapshot

            dependencies = dependency_snapshot(scene, robot_ids, state, choices, moves, chosen_steps=steps)
        proposed_by_model = bool(guidance and (len(choices) > 1 or can_backtrack))
        control = None
        continued_after_budget = False
        if proposed_by_model:
            extra = dict(dependencies=dependencies, can_backtrack=can_backtrack) if relation_mode else {}
            if relation_mode and branch_policy == "DEFER":
                extra.update(backtrack_effect="DEFER", deferred_count=len(deferred))
            status, selected = guidance.choose(
                scene, robot_ids, state, steps, choices, moves, attempts=attempts,
                remaining_seconds=limits.max_seconds - (clock() - started), remaining_checks=limits.max_checks - checks,
                **extra,
            )
            if status == "BACKTRACK" and can_backtrack:
                control = "BACKTRACK"
            elif status == "MODEL_CALL_LIMIT" and guidance_budget_policy == "CODE_CONTINUE":
                proposed_by_model = False
                continued_after_budget = True
                code_after_model_budget += 1
            elif status != "RESPONSE":
                return finish(status)
        elif guidance:
            forced_moves += 1
        if relation_mode and (not guidance or continued_after_budget):
            choice = dependency_code_choice(dependencies, can_backtrack=can_backtrack)
            if choice == "BACKTRACK":
                control = choice
            else:
                selected = int(choice[1:]) - 1
        if status := exhausted():
            return finish(status)
        if relation_mode:
            dependency_decisions.append(dict(prefix=[dict(robot_id=s.robot_id, action=s.action, path=list(s.path)) for s in steps],
                                             facts=dependencies, choice=control or f"M{selected + 1}",
                                             source="CODE_AFTER_MODEL_BUDGET" if continued_after_budget else
                                             "MODEL" if proposed_by_model else "FORCED" if guidance else "CODE_RULE"))
            if len(dependency_decisions) > 128:
                del dependency_decisions[0]
        if control == "BACKTRACK":
            backtracks.append(dict(depth=len(steps), after_check=checks, source="MODEL" if proposed_by_model else "CODE_RULE",
                                   effect=branch_policy))
            if len(backtracks) > 128:
                del backtracks[0]
            queue.pop()
            if branch_policy == "DEFER":
                item["deferred_once"] = True
                deferred.append(item)
                deferred_total += 1
                peak = max(peak, len(queue) + len(deferred))
            continue
        item["remaining"].remove(selected)
        next_state = next(value for index, value in choices if index == selected)
        next_steps = (*steps, moves[selected].step)
        plan = scene._plan(plan_id, "RECOVERY_SEARCH", next_steps)
        complete = all_arrived(next_state)
        reason = check(plan, complete=complete)
        if proposed_by_model:
            guidance.calls[-1].update(check_number=checks, check_result=reason or "VALID")
        # 한도 내에 끝난 유효 검사만 기록·실행에 사용할 수 있다.
        if status := exhausted():
            # 정확히 마지막 허용 검사에서 완성한 경우도 사용할 수 있다.
            if status != "CHECK_LIMIT" or reason or not complete:
                return finish(status)
        if reason:
            continue
        visited.add(next_state)
        if complete:
            return finish("FOUND", plan)
        queue.append(frame(next_state, next_steps))
        peak = max(peak, len(queue) + len(deferred))
    # 모델이 아직 가능한 분기를 포기했다면 전체 해법 부재를 증명한 것이 아니다.
    return finish("STEP_LIMIT" if depth_cutoff else "BRANCHES_ABANDONED" if branch_policy == "DISCARD" and relation_mode and guidance and backtracks else "EXHAUSTED")
