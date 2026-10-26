"""Opt-in real KEV validation in an isolated simulated fleet, never live robots.

Run: ~/venv/robot/bin/python tools/verify_kev_recovery.py --output /tmp/kev-validation.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app.services.kev_mapf.kev_coordinator import FmsMapfCoordinator
from backend.app.services.kev_mapf.kev_graph import Graph
from backend.app.services.kev_mapf.kev_recovery import KevRecovery
from backend.app.services.mock_data import MockFmsStore
from backend.app.services.reservation import ReservationTable
from backend.app.services.route_selector.kev_selector import KevSelector


# Inputs only: no expected recovery robot, holding node or path is prescribed.
CASES = {
    "photo_n5_n7_n9": [[5, 7, 11, 3], [7, 9, 1], [9, 7, 11, 6]],
    "photo_n8_n9_n7": [[8, 7, 11, 6], [9, 7, 5], [7, 9, 2]],
    "three_robot_cycle": [[7, 9, 1], [8, 7, 5], [9, 10, 8, 12, 4]],
}


async def verify_case(name, routes, graph):
    store = MockFmsStore(ReservationTable())
    coordinator = FmsMapfCoordinator()
    store._robots.clear()
    nodes = Graph(graph).nodes
    goals = {}
    for index, raw_path in enumerate(routes, 1):
        rid = f"robot{index}"
        path = list(map(str, raw_path))
        x, y = nodes[raw_path[0]]
        store._robots[rid] = dict(
            robot_id=rid, x=x, y=y, yaw=0, status="WAITING",
            current_node=path[0], occupied_node=path[0], occupied_edge=None,
            goal_node=path[-1], navigation_id=f"{name}-{rid}", request_order=index,
            task_priority=0, stop_requested=False, connection_state="ONLINE",
            route={"node_ids": path, "segment_index": 0, "phase": "waiting", "departure_at": None},
        )
        store._traffic._required_paths[rid] = tuple(path)
        store._traffic._pending[rid] = None
        coordinator.register_preview(
            {"robot_id": rid, "selector": "kev", "model": "kev-08b"},
            path[0], path[-1], {"path": path},
        )
        goals[rid] = path[-1]
    clock = [100.0]
    store._last_tick = clock[0]
    worker = KevRecovery()
    mode = SimpleNamespace(mode="simulation")
    decisions = []
    select_choice = KevSelector.select_choice

    def trace(selector, state, candidates, instructions, *, question_id="decision"):
        result = select_choice(selector, state, candidates, instructions, question_id=question_id)
        if question_id == "deadlock_recovery":
            decisions.append({
                "simulated_seconds": round(clock[0]-100, 1), "state": state,
                "criteria": candidates, "instructions": instructions,
                "choice": result["choice"], "confidence": result.get("confidence"),
                "probabilities": result.get("probabilities"),
            })
        return result

    minimum = math.inf
    with patch("backend.app.services.route_graph.load_route_graph", return_value=graph), patch(
        "backend.app.services.mock_data.load_route_graph", return_value=graph
    ), patch("backend.app.services.kev_mapf.kev_recovery.monotonic", side_effect=lambda: clock[0]), patch.object(
        KevSelector, "select_choice", new=trace
    ):
        for tick in range(1, 1001):
            clock[0] = 100 + tick*.1
            store.advance_simulation(.1, now=clock[0])
            robots = list(store._robots.values())
            minimum = min(minimum, *(math.dist((a["x"], a["y"]), (b["x"], b["y"]))
                                   for i, a in enumerate(robots) for b in robots[i+1:]))
            if tick % 2 == 0:
                await worker.run_once(store, coordinator, mode, now=clock[0])
            if all(r["goal_node"] is None for r in robots):
                break
    actual = {rid: r["occupied_node"] for rid, r in store._robots.items()}
    completed = actual == goals and all(r["goal_node"] is None for r in store._robots.values())
    result = {
        "case": name, "completed": completed, "goals": goals, "actual_nodes": actual,
        "simulated_seconds": round(clock[0]-100, 1), "minimum_sampled_separation_m": minimum,
        "kev_decisions": decisions,
    }
    print(f"{name}: completed={completed} elapsed={result['simulated_seconds']}s "
          f"decisions={len(decisions)} final={actual}", flush=True)
    return result


async def main(output):
    graph = json.loads((ROOT / "routes/test.geojson").read_text())
    results = [await verify_case(name, paths, graph) for name, paths in CASES.items()]
    output.write_text(json.dumps({"live_robot_commands_sent": False, "results": results},
                                ensure_ascii=False, indent=2) + "\n")
    if not all(r["completed"] and r["minimum_sampled_separation_m"] >= .12-1e-4 for r in results):
        raise SystemExit("KEV validation failed; inspect the saved decision trace")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("/tmp/kev-validation.json"))
    asyncio.run(main(parser.parse_args().output))
