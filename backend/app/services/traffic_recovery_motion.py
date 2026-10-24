"""검사 전 이동의 거리·시간 정보. 미래 순서 탐색이나 통행 허가를 수행하지 않는다."""

import math

from .traffic_metrics import DEFAULT_TRAFFIC_ESTIMATE_SPEED_MPS, estimate_plan, path_distance


def add_motion_estimates(facts, scene, robot_ids, stages, steps, choices, moves, *,
                         speed_mps=DEFAULT_TRAFFIC_ESTIMATE_SPEED_MPS):
    """새로 만든 관계 정보에 추정값을 붙인다. scene 입력·주행·예약은 바꾸지 않는다.

    평균 도착 하한: 완료한 요청은 누적 완료 시각, 미완료 요청은 이번 동작 종료
    뒤 자기 남은 최단 공급 경로를 곧바로 간다고 가정한다. 로봇 간 선후 관계와
    예약 지연을 무시하므로 전체 실행 시간 예측이나 해결 가능 판정이 아니다.
    """
    if isinstance(speed_mps, bool) or not math.isfinite(speed_mps) or speed_mps <= 0:
        raise ValueError("거리·시간 추정 속도는 유한한 양수여야 합니다.")
    nodes, requests = scene.planner.nodes, scene.requests

    def distance(path):
        value = path_distance(path, nodes)
        if not math.isfinite(value) or value < 0:
            raise ValueError("지도 좌표에서 유효한 이동 거리를 계산할 수 없습니다.")
        return value

    lengths = {i: distance(move.step.path) for i, move in enumerate(moves)}

    def remaining_distance(rid, stage):
        request = requests[rid]
        if stage == 2 or (stage == 0 and request["current_node"] == request["node"]):
            return 0.0
        options = []
        for index, move in enumerate(moves):
            if move.step.robot_id != rid or move.from_stage != stage:
                continue
            value = lengths[index]
            if move.to_stage == 1:
                restores = [lengths[j] for j, m in enumerate(moves)
                            if m.step.robot_id == rid and m.from_stage == 1 and m.to_stage == 2]
                if not restores:
                    continue
                value += min(restores)
            options.append(value)
        return min(options) if options else None

    prefix = estimate_plan(scene._plan("ESTIMATED_PREFIX", "ESTIMATE_ONLY", steps),
                           requests, nodes, speed_mps=speed_mps)
    elapsed = sum(row["travel_seconds"] for row in prefix["steps"])
    facts["motion_estimation"] = dict(
        schema_version="recovery-motion/0.1", basis="GRAPH_NODE_COORDINATES_CONSTANT_SPEED",
        speed_mps=speed_mps, time_origin="HYPOTHETICAL_EXECUTION_START",
        prefix_travel_seconds=elapsed, includes_inference=False,
        includes_turns_or_reservation_delays=False, assumes_serial_steps=True,
        completion_values_are_optimistic_lower_bounds=True,
        mean_lower_bound_relaxes_future_serial_order=True,
        lower_bounds_ignore_other_robots_future_moves=True,
        not_a_reservation_or_complete_plan_verdict=True,
    )
    for robot in facts["robots"]:
        robot["remaining_supplied_path_estimates"] = [
            dict(path=list(path), distance_m=distance(path), travel_seconds=distance(path) / speed_mps)
            for path in robot["remaining_supplied_paths"]
        ]

    current = dict(zip(robot_ids, stages))
    moving = [rid for rid in robot_ids if requests[rid]["current_node"] != requests[rid]["node"]]
    for index, _ in choices:
        move = moves[index]
        next_stages = {**current, move.step.robot_id: move.to_stage}
        duration = lengths[index] / speed_mps
        end = elapsed + duration
        completions = {}
        for rid in moving:
            completed = prefix["robots"][rid]["goal_completion_seconds"]
            if rid == move.step.robot_id and move.to_stage == 2:
                completed = end
            if completed is not None:
                completions[rid] = completed
            elif next_stages[rid] == 2:
                # 단계를 완료했다고 표시했더라도 그 시각을 모르면 0으로 채우지 않는다.
                completions[rid] = None
            else:
                remaining = remaining_distance(rid, next_stages[rid])
                completions[rid] = None if remaining is None else end + remaining / speed_mps
        remaining = [remaining_distance(rid, next_stages[rid]) for rid in robot_ids]
        known_mean = bool(completions) and all(v is not None for v in completions.values())
        facts["offered_moves"][f"M{index + 1}"]["estimated_motion"] = dict(
            distance_m=lengths[index], travel_seconds=duration,
            start_seconds=elapsed, completion_seconds=end,
            goal_completion_lower_bound_seconds=completions,
            mean_goal_completion_lower_bound_seconds=(sum(completions.values()) / len(completions)) if known_mean else None,
            all_returns_completion_lower_bound_seconds=(end + sum(remaining) / speed_mps)
                if all(v is not None for v in remaining) else None,
            moving_request_count=len(moving),
        )
    return facts
