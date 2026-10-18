"""[모듈: kev_conflict.py - 다중 로봇 충돌 구역 감지 및 데드락 검사]
- 역할: 여러 로봇의 예정 경로에서 공유 노드·간선을 식별하고, 우선순위 그래프의 순환(Cycle) 여부를 검증한다.
- 기능:
  1. 공유 노드 및 양방향 물리 간선 추출 (zone_candidates)
  2. 동일 로봇 집합이 연속 공유하는 노드/간선 그룹화 (group_conflict_zones)
  3. 경로 내 충돌 구역 최초 진입 인덱스 계산 (first_zone_index)
  4. 우선순위 관계 추가 시 순환(데드락) 발생 여부 DFS 검사 (would_cycle)
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any


def zone_candidates(paths: dict[str, list[int]]) -> dict[str, dict[str, Any]]:
    """여러 로봇이 공유할 가능성이 있는 노드(`node:N`)와 양방향 물리 간선(`edge:A:B`)을 찾는다."""
    nodes_by: dict[int, set[str]] = defaultdict(set)
    edges_by: dict[tuple[int, int], set[str]] = defaultdict(set)

    for rid, path in paths.items():
        for n in path:
            nodes_by[n].add(rid)
        for a, b in zip(path, path[1:]):
            edges_by[(a, b) if a <= b else (b, a)].add(rid)

    c: dict[str, dict[str, Any]] = {}
    for node, robots in nodes_by.items():
        if len(robots) >= 2:
            c[f"node:{node}"] = {"kind": "node", "node": node, "robots": sorted(robots)}
    for (a, b), robots in edges_by.items():
        if len(robots) >= 2:
            c[f"edge:{a}:{b}"] = {"kind": "edge", "edge": [a, b], "robots": sorted(robots)}
    return dict(sorted(c.items()))


def group_conflict_zones(zones: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """같은 로봇들이 공유하고 서로 물리적으로 연결된 노드/간선을 하나의 구역으로 묶는다."""
    keys = sorted(zones)
    connected: dict[str, set[str]] = {zid: set() for zid in keys}
    touched = {
        k: ({z["node"]} if z["kind"] == "node" else set(z["edge"]))
        for k, z in zones.items()
    }

    for i, key_a in enumerate(keys):
        a = zones[key_a]
        for key_b in keys[i + 1 :]:
            b = zones[key_b]
            if (
                a["robots"] == b["robots"]
                and not (a["kind"] == "node" and b["kind"] == "node")
                and (touched[key_a] & touched[key_b])
            ):
                connected[key_a].add(key_b)
                connected[key_b].add(key_a)

    visited: set[str] = set()
    grouped: dict[str, dict[str, Any]] = {}

    for key in keys:
        if key in visited:
            continue
        stack = [key]
        members = []
        while stack:
            curr = stack.pop()
            if curr not in visited:
                visited.add(curr)
                members.append(curr)
                stack.extend(sorted(connected[curr] - visited))
        members.sort()

        if len(members) == 1:
            grouped[members[0]] = zones[members[0]]
        else:
            grouped[f"zone:{members[0]}"] = {
                "kind": "group",
                "robots": zones[members[0]]["robots"],
                "nodes": sorted(zones[m]["node"] for m in members if zones[m]["kind"] == "node"),
                "edges": sorted(zones[m]["edge"] for m in members if zones[m]["kind"] == "edge"),
                "resources": members,
            }
    return dict(sorted(grouped.items()))


def first_zone_index(path: list[int], zone: dict[str, Any]) -> int | None:
    """경로에서 공유 구역에 최초 진입하는 인덱스 위치를 찾는다."""
    kind = zone["kind"]
    if kind == "node":
        target = zone["node"]
        return next((i for i, n in enumerate(path) if n == target), None)
    if kind == "edge":
        target_edge = set(zone["edge"])
        return next(
            (i for i, (u, v) in enumerate(zip(path, path[1:])) if {u, v} == target_edge),
            None,
        )
    if kind == "group":
        group_nodes = set(zone["nodes"])
        group_edges = {(a, b) if a <= b else (b, a) for a, b in zone["edges"]}
        node_idx = next((i for i, n in enumerate(path) if n in group_nodes), None)
        edge_idx = next(
            (
                i
                for i, (a, b) in enumerate(zip(path, path[1:]))
                if ((a, b) if a <= b else (b, a)) in group_edges
            ),
            None,
        )
        indices = [idx for idx in (node_idx, edge_idx) if idx is not None]
        return min(indices) if indices else None
    raise ValueError(f"Unknown zone kind: {kind}")


def would_cycle(priorities: set[tuple[str, str]], high: str, low: str) -> bool:
    """기존 우선순위 관계 집합(`priorities`)에 `(high, low)`를 추가했을 때 순환(Deadlock)이 생기는지 DFS로 검사한다."""
    if high == low:
        return True
    outgoing: dict[str, set[str]] = defaultdict(set)
    for a, b in priorities:
        outgoing[a].add(b)
    outgoing[high].add(low)
    stack, seen = [low], set()
    while stack:
        u = stack.pop()
        if u == high:
            return True
        if u not in seen:
            seen.add(u)
            stack.extend(outgoing.get(u, ()))
    return False
