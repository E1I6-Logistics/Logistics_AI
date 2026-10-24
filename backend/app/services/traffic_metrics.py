"""정지 상태·순차 실행 교통 시험의 예상 값과 관측 시간. 통행 허가에는 쓰지 않는다."""

from copy import deepcopy
import math

DEFAULT_TRAFFIC_ESTIMATE_SPEED_MPS = 0.2


def path_distance(path, nodes):
    return sum(math.dist(nodes[a], nodes[b]) for a, b in zip(path, path[1:]))


def estimate_plan(plan, requests, nodes, *, speed_mps=DEFAULT_TRAFFIC_ESTIMATE_SPEED_MPS):
    """실행 시작을 0초로 둔다. 회전·예약 지연·추론·폴링 지연은 예측하지 않는다."""
    if isinstance(speed_mps, bool) or not math.isfinite(speed_mps) or speed_mps <= 0:
        raise ValueError("예상에 사용할 속도는 유한한 양수여야 합니다.")
    rows = {
        rid: dict(
            has_movement_request=r["current_node"] != r["node"],
            prior_wait_seconds=r["waiting_seconds"],
            first_step_start_seconds=None, goal_completion_seconds=None,
            step_distance_m=0.0, last_step_completion_seconds=None,
        ) for rid, r in requests.items()
    }
    steps, elapsed = [], 0.0
    for index, step in enumerate(plan.steps, 1):
        distance = path_distance(step.path, nodes)
        duration = distance / speed_mps
        end = elapsed + duration
        steps.append(dict(
            step=index, robot_id=step.robot_id, action=step.action,
            distance_m=distance, travel_seconds=duration,
            start_seconds=elapsed, completion_seconds=end,
        ))
        row = rows[step.robot_id]
        if row["first_step_start_seconds"] is None:
            row["first_step_start_seconds"] = elapsed
        row["step_distance_m"] += distance
        row["last_step_completion_seconds"] = end
        if step.action != "YIELD" and step.path[-1] == requests[step.robot_id]["node"]:
            row["goal_completion_seconds"] = end
        elapsed = end
    moving = [r for r in rows.values() if r["has_movement_request"]]
    complete = bool(moving) and all(r["goal_completion_seconds"] is not None for r in moving)
    return dict(
        basis="SERIAL_PATH_DISTANCE_AT_CONSTANT_SPEED", speed_mps=speed_mps,
        time_origin="EXECUTION_START", includes_setup_or_inference=False,
        includes_reservation_or_turn_delays=False,
        moving_request_count=len(moving),
        mean_goal_completion_seconds=(sum(r["goal_completion_seconds"] for r in moving) / len(moving)) if complete else None,
        max_turn_wait_seconds=max((r["first_step_start_seconds"] for r in moving), default=None) if complete else None,
        all_steps_completion_seconds=elapsed if plan.steps else None,
        total_distance_m=sum(r["step_distance_m"] for r in rows.values()),
        robots=rows, steps=steps,
    )


class TrafficRunMetrics:
    """한 시계로 잰 상대 시각. 관제는 경과 시간, 격리 시험은 가상 시간을 구분한다."""

    def __init__(self, requests, nodes, *, requested_at, clock_kind):
        self.requests, self.nodes = requests, nodes
        self.requested_at, self.clock_kind = requested_at, clock_kind
        self.execution_at = None
        self.selection_seconds = 0.0
        self.steps = []
        self.robots = {
            rid: dict(
                has_movement_request=r["current_node"] != r["node"],
                prior_wait_seconds=r["waiting_seconds"],
                first_step_started_seconds=None, goal_reached_seconds=None,
                last_step_completed_seconds=None, completed_path_distance_m=0.0,
            ) for rid, r in requests.items()
        }

    def begin_execution(self, now, *, selection_seconds=0.0):
        self.execution_at, self.selection_seconds = now, selection_seconds

    def start_step(self, index, step, now):
        offset = now - self.requested_at
        self.steps.append(dict(
            step=index, robot_id=step.robot_id, action=step.action,
            path=list(step.path), started_seconds=offset,
            completed_seconds=None, active_seconds=None,
        ))
        row = self.robots[step.robot_id]
        if row["first_step_started_seconds"] is None:
            row["first_step_started_seconds"] = offset

    def complete_step(self, step, now):
        offset = now - self.requested_at
        self.steps[-1].update(
            completed_seconds=offset,
            active_seconds=offset - self.steps[-1]["started_seconds"],
        )
        row = self.robots[step.robot_id]
        row["last_step_completed_seconds"] = offset
        row["completed_path_distance_m"] += path_distance(step.path, self.nodes)
        if step.action != "YIELD" and step.path[-1] == self.requests[step.robot_id]["node"]:
            row["goal_reached_seconds"] = offset

    def report(self, now, *, outcome="RUNNING"):
        setup = None if self.execution_at is None else self.execution_at - self.requested_at
        moving = [r for r in self.robots.values() if r["has_movement_request"]]
        completed = [r for r in moving if r["goal_reached_seconds"] is not None]
        all_arrived = bool(moving) and len(completed) == len(moving)
        # 미완료를 0초로 평균에 섞지 않는다. 모든 이동 요청이 끝나야 평균을 확정한다.
        mean = (sum(r["goal_reached_seconds"] for r in moving) / len(moving)) if all_arrived else None
        return dict(
            clock=self.clock_kind, time_origin="SCENARIO_REQUEST_CAPTURE",
            outcome=outcome, elapsed_seconds=now - self.requested_at,
            setup_seconds=setup, selection_seconds=self.selection_seconds,
            execution_elapsed_seconds=None if setup is None else now - self.execution_at,
            moving_request_count=len(moving), completed_request_count=len(completed),
            unfinished_robot_ids=[rid for rid, r in self.robots.items() if r["has_movement_request"] and r["goal_reached_seconds"] is None],
            mean_goal_completion_seconds=None if mean is None else mean - setup,
            mean_request_to_goal_seconds=(sum(r["goal_reached_seconds"] + r["prior_wait_seconds"] for r in moving) / len(moving)) if all_arrived else None,
            max_request_to_first_step_seconds=max((
                r["prior_wait_seconds"] + (r["first_step_started_seconds"] if r["first_step_started_seconds"] is not None else now - self.requested_at)
                for r in moving
            ), default=None),
            max_turn_wait_seconds=None if setup is None else max((
                (r["first_step_started_seconds"] if r["first_step_started_seconds"] is not None else now - self.requested_at) - setup
                for r in moving
            ), default=None),
            # 실제 이동만의 시간/거리라고 주장하지 않는다. 예약 정지는 단계 시간에 포함된다.
            steps=deepcopy(self.steps), robots=deepcopy(self.robots),
        )
