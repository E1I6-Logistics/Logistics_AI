"""[모듈: kev_graph.py - MAPF 그래프 래퍼 및 경로 검증기]
- 역할: GeoJSON FeatureCollection을 파싱하여 노드·간선 가중치 및 AI용 compact 그래프를 구축한다.
- 기능:
  1. 노드 ID 정수 정규화 (_node)
  2. 경로의 시작·끝점 일치, 유향 간선 존재 여부, 유향 간선 재사용 금지 및 노드 방문 상한 검증 (Graph.check_path)
  3. 충돌 구간 제외 가상 그래프 생성 (Graph.virtual_graph)
  4. 물리적 대기 가능 노드 조회 (Graph.declared_wait_nodes)
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from backend.app.services import route_service


def _node(v: Any) -> int:
    """노드 ID를 정수(int)로 정규화하며, bool이나 실수(float) 같은 손실 변환 입력은 거부한다."""
    if isinstance(v, bool) or not isinstance(v, (int, str)):
        raise ValueError("node ID must be an integer or integer string (not bool/float)")
    return int(v)


def _node_sort(s: str) -> tuple[int, int | str]:
    """노드 ID 문자열을 숫자 우선, 문자열 차순으로 정렬하기 위한 키를 생성한다."""
    try:
        return 0, int(s)
    except ValueError:
        return 1, s


class Graph:
    """기존 경로 서비스(`route_service`)를 재사용하는 읽기 전용 유향 GeoJSON 그래프 래퍼."""

    def __init__(self, data: dict[str, Any]):
        if data.get("type") != "FeatureCollection":
            raise ValueError("--graph must be GeoJSON FeatureCollection")
        self.data = data
        self.nodes, self.edge_records = route_service.build_route_inputs(
            data, require_stored_weight=True
        )
        self.node_props = {
            _node(key): feature["properties"]
            for key, feature in route_service.node_lookup(data).items()
        }
        self.edges = route_service.build_edge_weight_lookup(self.nodes, self.edge_records)
        self.compact = route_service.build_compact_adjacency_graph(
            data, "kev-mapf-in-memory", require_stored_weight=True
        )

    @classmethod
    def load(cls, path: Path) -> Graph:
        """지정된 파일 경로에서 GeoJSON을 읽어 `Graph` 인스턴스를 생성한다."""
        return cls(json.loads(path.read_text(encoding="utf-8")))

    def check_path(
        self, path: list[int], *, start: int | None = None, goal: int | None = None
    ) -> None:
        """경로의 시작·끝점 일치, 유향 간선 존재 여부, 유향 간선 재사용 금지 및 노드 방문 상한(최대 2회)을 검증한다."""
        normalized = [_node(n) for n in path]
        if not normalized:
            raise ValueError("path must be nonempty")
        route_service.validate_and_calculate_path_distance(
            self.nodes,
            self.edge_records,
            normalized,
            normalized[0] if start is None else start,
            normalized[-1] if goal is None else goal,
        )
        steps = list(zip(normalized, normalized[1:]))
        if len(set(steps)) != len(steps) or any(n > 2 for n in Counter(normalized).values()):
            raise ValueError("path violates directed-edge reuse or node visit limit")

    def virtual_graph(
        self, forbidden_nodes: set[int], forbidden_edges: set[tuple[int, int]]
    ) -> dict[str, Any]:
        """제외할 충돌 노드·간선을 제거한 임시 GeoJSON을 메모리에 생성하고 원본 데이터는 보존한다."""
        features = []
        for f in self.data.get("features", []):
            p = f.get("properties") or {}
            if (f.get("geometry") or {}).get("type") == "Point":
                if p.get("id") is not None and _node(p["id"]) in forbidden_nodes:
                    continue
            elif p.get("startid") is not None and p.get("endid") is not None:
                a, b = _node(p["startid"]), _node(p["endid"])
                if a in forbidden_nodes or b in forbidden_nodes or (a, b) in forbidden_edges:
                    continue
            features.append(f)
        return {"type": "FeatureCollection", "features": features}

    def declared_wait_nodes(self) -> set[int]:
        """GeoJSON 노드 속성에서 `wait_allowed=True` 또는 `safe_wait=True`로 선언된 대기 가능 노드 집합을 반환한다."""
        return {
            n
            for n, p in self.node_props.items()
            if p.get("wait_allowed") is True or p.get("safe_wait") is True
        }
