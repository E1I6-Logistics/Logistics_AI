"""KEV 계획과 실제·모의 실행 사이의 공통 계약. 판단은 하지 않는다."""

from contextlib import contextmanager


class MapfExecution:
    def __init__(self, traffic, *, mode, response, refresh=None, validate=None, publish=None):
        self.mode = mode
        self._traffic = traffic
        self._response = response
        self._refresh = refresh
        self._validate = validate
        self._publish = publish

    @contextmanager
    def transaction(self):
        """실행 상태 갱신부터 계획 반영까지 같은 실행기 잠금을 유지한다."""
        with self._traffic.lock:
            if self._refresh is not None:
                self._refresh()
            yield self

    def snapshot(self):
        return self._traffic.planning_snapshot()

    def _published_responses(self, goals):
        if self._publish is not None:
            self._publish(goals)
        robots, _, _ = self.snapshot()
        return {rid: dict(self._response(rid, "goal-node", {"node_id": goal}),
                          route=robots[rid]["route"]) for rid, goal in goals.items()}

    def commit_routes(self, graph, pending, paths, waits, ordered, *, now):
        """검증된 cohort 전체를 예약하거나 아무 요청도 반영하지 않는다."""
        with self.transaction():
            if self._validate is not None:
                self._validate(pending)
            if not self._traffic.commit_required_routes(
                graph, pending, paths, waits, ordered, now=now,
            ):
                return None
            return self._published_responses({rid: req["node"] for rid, req in pending.items()})

    def recover(self, robot_id, path, decision, graph, *, now, initial_goal=None):
        with self.transaction():
            if initial_goal is not None and self._validate is not None:
                self._validate([robot_id])
            before, _, _ = self.snapshot()
            goal = initial_goal if initial_goal is not None else before[robot_id]["goal_node"]
            if not self._traffic.recover_required_path(
                robot_id, path, decision, graph, now=now, initial_goal=initial_goal,
            ):
                return None
            return self._published_responses({robot_id: goal})[robot_id]
