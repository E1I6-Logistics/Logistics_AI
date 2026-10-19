"""[테스트 모듈: kev_mock.py - Kev MAPF 오프라인 테스트 대역 및 데모 데이터]
- 역할: 실제 Kev AI 서버나 ROS 없이 단위 테스트를 수행하기 위한 MockKEV와 내장 예제 그래프를 제공한다.
"""

from __future__ import annotations

from backend.app.services.kev_mapf.kev_graph import Graph


class MockKEV:
    """오프라인 데모 및 단위 테스트 전용 대역(Stub)이며, 결과를 실제 모델 추론으로 취급하지 않는다."""

    calls = 0
    model_seconds = 0.0
    forced = 0

    def pick(self, state, candidates, instructions, question_id):
        """실제 추론 없이 `action_wait`가 후보에 있으면 우선 선택하고, 없으면 첫 번째 후보를 반환한다."""
        if len(candidates) == 1:
            self.forced += 1
        else:
            self.calls += 1
        if "action_wait" in candidates:
            return "action_wait"
        return next(iter(candidates))

    def route(self, graph_data, start, goal, *, selector="kev", model=None, traffic=None):
        """오프라인 데모·테스트를 위해 단순 너비 우선 탐색(BFS)으로 연결 경로를 찾는다."""
        graph = Graph(graph_data)
        todo = [[start]]
        seen = {start}
        while todo:
            p = todo.pop(0)
            if p[-1] == goal:
                return p
            for a, b in sorted(graph.edges):
                if a == p[-1] and b not in seen:
                    seen.add(b)
                    todo.append(p + [b])
        raise RuntimeError("demo route not found")


def demo_data() -> tuple[Graph, dict]:
    """데모 및 오프라인 테스트에서 사용할 내장 9-노드 GeoJSON 그래프와 3대의 AMR 예제 작업을 반환한다."""
    points = {
        1: (0, 1),
        2: (1, 1),
        3: (2, 1),
        4: (3, 1),
        5: (0, 0),
        6: (1, 0),
        7: (3, 0),
        8: (4, 1),
        9: (5, 1),
    }
    edges = [
        (1, 2),
        (2, 3),
        (3, 4),
        (5, 6),
        (6, 3),
        (3, 7),
        (4, 8),
        (8, 9),
        (3, 2),
        (2, 1),
    ]
    features = [
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": list(xy)},
            "properties": {"id": n, "wait_allowed": n in (2, 6)},
        }
        for n, xy in points.items()
    ] + [
        {
            "type": "Feature",
            "geometry": {
                "type": "LineString",
                "coordinates": [list(points[a]), list(points[b])],
            },
            "properties": {"id": i + 50, "startid": a, "endid": b, "weight": 1.0},
        }
        for i, (a, b) in enumerate(edges)
    ]
    graph = Graph({"type": "FeatureCollection", "features": features})
    jobs = {
        "robots": [
            {"id": "amr_1", "current_node": 1, "goal_node": 4, "path": [1, 2, 3, 4], "task_priority": 1},
            {"id": "amr_2", "current_node": 5, "goal_node": 7, "path": [5, 6, 3, 7], "task_priority": 2},
            {"id": "amr_3", "current_node": 8, "goal_node": 9, "path": [8, 9], "task_priority": 3},
        ],
        "wait_allowed_nodes": [2, 6],
        "zone_occupancy": {},
    }
    return graph, jobs
