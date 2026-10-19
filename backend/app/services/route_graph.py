"""Read-only access to the static GeoJSON shown by the frontend."""

from __future__ import annotations

import copy
import json
import math
from pathlib import Path
from typing import Any

from ..config import ROUTE_GRAPH_PATH

_GRAPH_CACHE: tuple[Path, int, dict[str, Any]] | None = None

"""
{
    # Node 좌표 및 타입 정보
    "geometry": {
        "coordinates": [1.4094749689102173, 0.03032200038433075],
        "type": "Point"
    },
    # Node ID 및 속성 정보
    "properties": {
        "frame": "map",
        "id": 13
    },
    "type": "Feature"
},
{
    # Edge 정보
    "geometry": {
        "type": "MultiLineString"
    },
    # Edge ID 및 속성 정보
    "properties": {
        "cost": 0.0,
        "endid": 3,
        "id": 14,
        
        # 이 Edge에 저장된 cost가 Edge Cost Function들에 의해 재계산/대체될 수 있도록 허용
        "overridable": true,
        "startid": 0
    },
    "type": "Feature"
}
"""


# Route Graph GeoJSON 파일 로드 기능
# 경로 그래프를 불러온다.
def load_route_graph() -> dict[str, Any]:
    global _GRAPH_CACHE
    if not ROUTE_GRAPH_PATH.exists():
        raise FileNotFoundError(f"Route Graph 파일 없음: {ROUTE_GRAPH_PATH}")
    resolved = ROUTE_GRAPH_PATH.resolve()
    mtime_ns = resolved.stat().st_mtime_ns
    if _GRAPH_CACHE is not None and _GRAPH_CACHE[0] == resolved and _GRAPH_CACHE[1] == mtime_ns:
        return copy.deepcopy(_GRAPH_CACHE[2])
    with ROUTE_GRAPH_PATH.open("r", encoding="utf-8") as file:
        graph = json.load(file)
    if graph.get("type") != "FeatureCollection":
        raise ValueError("Route Graph는 GeoJSON FeatureCollection 형식이어야 합니다.")
    _GRAPH_CACHE = (resolved, mtime_ns, graph)
    return copy.deepcopy(graph)


# Node Features 추출 기능
# 지도에서 노드 형식의 항목을 추출한다.
def _node_features(graph: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for feature in graph.get("features", []):
        geometry = feature.get("geometry") or {}
        properties = feature.get("properties") or {}
        node_id = properties.get("id")
        coordinates = geometry.get("coordinates")
        if (
            geometry.get("type") == "Point"
            and node_id is not None
            and isinstance(coordinates, list)
            and len(coordinates) >= 2
        ):
            result[str(node_id)] = feature
    return result


# Node 정보 조회 기능 -> 문자열 또는 정수를 Node ID로 조회 가능
# 지정한 노드의 정보를 조회한다.
def get_node(node_id: str | int) -> dict[str, Any]:
    feature = _node_features(load_route_graph()).get(str(node_id))
    if feature is None:
        raise KeyError(f"존재하지 않는 route node: {node_id}")
    coordinates = feature["geometry"]["coordinates"]
    properties = feature.get("properties") or {}
    return {
        "id": properties.get("id"),
        "x": float(coordinates[0]),
        "y": float(coordinates[1]),
        "frame": properties.get("frame", "map"),
        "properties": properties,
    }


# Node 목록 조회 기능 -> ID 기준 정렬, 문자열 ID는 뒤로
# 노드를 목록으로 반환한다.
def list_nodes() -> list[dict[str, Any]]:
    nodes = [get_node(node_id) for node_id in _node_features(load_route_graph())]

    # 노드 정렬에 사용할 키를 만든다.
    def sort_key(node: dict[str, Any]):
        try:
            return 0, int(node["id"])
        except (TypeError, ValueError):
            return 1, str(node["id"])

    return sorted(nodes, key=sort_key)


# 간선 IDS를 찾는다.
def find_edge_ids(graph: dict[str, Any], node_ids: list[str]) -> list[str]:
    # (출발 노드, 도착 노드) -> 엣지 ID
    edge_lookup = {}

    for feature in graph.get("features", []):
        props = feature.get("properties") or {}

        if "startid" not in props and "endid" not in props:
            continue

        if any(props.get(key) is None for key in ("startid", "endid", "id")):
            raise ValueError("엣지의 출발/도착/ID 정보가 누락되었습니다.")

        pair = (str(props["startid"]), str(props["endid"]))
        if pair in edge_lookup:
            raise ValueError(f"동일한 노드에 엣지가 여러 개 있습니다.: {pair}")

        edge_lookup[pair] = str(props["id"])

    edge_ids = []
    for start, end in zip(node_ids, node_ids[1:]):
        pair = (str(start), str(end))

        if pair not in edge_lookup:
            raise ValueError(f"경로에 해당하는 엣지가 없습니다. {start} -> {end}")

        edge_ids.append(edge_lookup[pair])

    return edge_ids


# current_node 를 새로 식별하기 위해 가장 가까운 노드를 찾아 현재 노드 결정
# tolerance_m 은 "이 거리 안에 있으면 노드에 도착했다고 볼 것인가" -> 값은 지도 간격과 위치 측정 정확도에 맞춰 정해야 함
# 현재 노드를 찾는다.
def locate_current_node(nodes, x, y, tolerance_m):
    if not nodes or not all(math.isfinite(v) for v in (x, y)):
        return None

    nearest_id = min(nodes, key=lambda node_id: math.dist((x, y), nodes[node_id]))
    distance = math.dist((x, y), nodes[nearest_id])

    return nearest_id if distance <= tolerance_m else None


# 가장 가까운 노드를 찾는다.
def find_nearest_node(nodes, x: float, y: float):
    if not nodes or not all(math.isfinite(v) for v in (x, y)):
        return None

    nearest_id = min(nodes, key=lambda node_id: math.dist((x, y), nodes[node_id]))

    return nearest_id
