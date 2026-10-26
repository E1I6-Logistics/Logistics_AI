"""Opt-in control of the running simulation FMS with real KEV inference.

Repositions simulated robots, sends goals/orders and records completed movement.
Never switches to real mode. Run only with permission to control this simulation.
"""

import argparse
import asyncio
import json
import math
from pathlib import Path
import time

import httpx

ROOT = Path(__file__).resolve().parents[1]
CASES = {
    "parallel_disjoint": ([0, 1, 6], [10, 9, 11]),
    "parallel_staggered": ([0, 1, 6], [10, 9, 11]),
    "three_way_n7": ([5, 6, 3], [0, 1, 2]),
    "three_way_n7_rotated": ([6, 3, 5], [1, 2, 0]),
    "three_way_n7_inflight": ([5, 6, 3], [0, 1, 2]),
    "manual_startup": ([0, 1, 2], [5, 6, 3]),
    "photo_n5_n7_n9": ([5, 7, 9], [3, 1, 6]),
    "photo_n8_n9_n7": ([8, 9, 7], [6, 5, 2]),
}


def concurrency(samples):
    peak, overlap, triple_overlap = 0, 0.0, 0.0
    previous = None
    for sample in samples:
        if previous:
            older = {r["robot_id"]: r for r in previous["robots"]}
            moving = sum(
                r.get("status") == "NAVIGATING" and older[r["robot_id"]].get("status") == "NAVIGATING" and
                math.dist((r["x"], r["y"]), (older[r["robot_id"]]["x"], older[r["robot_id"]]["y"])) > 1e-5
                for r in sample["robots"]
            )
            peak = max(peak, moving)
            if moving >= 2:
                overlap += sample["elapsed"]-previous["elapsed"]
            if moving >= 3:
                triple_overlap += sample["elapsed"]-previous["elapsed"]
        previous = sample
    return {"max_simultaneously_moving": peak, "overlapping_motion_seconds": round(overlap, 2),
            "three_robot_motion_seconds": round(triple_overlap, 2)}


async def reposition(client, starts, nodes):
    for i in range(1, 4):
        r = await client.post("/api/command/stop", json={"robot_id": f"robot{i}"})
        r.raise_for_status()
    for i, node in enumerate(starts, 1):
        x, y = nodes[str(node)]
        r = await client.post("/api/command/goal", json={
            "robot_id": f"robot{i}", "target_x": x, "target_y": y,
        })
        r.raise_for_status()


async def manual_case(client, name, starts, goals, nodes, timeout):
    await reposition(client, starts, nodes)
    begun = time.monotonic()
    async def goal(i, node):
        r = await client.post("/api/command/goal-node", json={
            "robot_id": f"robot{i}", "node_id": node,
            "driving_mode": "llm", "selector": "kev", "model": "kev-08b",
        })
        result = {"robot_id": f"robot{i}", "http_status": r.status_code, "response": r.json()}
        print(name, "dispatch", result["robot_id"], r.status_code,
              result["response"].get("mapf", {}).get("analysis_source"), flush=True)
        return result
    in_flight_submission = None
    if name in ("parallel_staggered", "three_way_n7_inflight"):
        first = 2 if name == "three_way_n7_inflight" else 1
        tasks = [asyncio.create_task(goal(first, goals[first-1]))]
        for _ in range(200):
            fleet = (await client.get("/api/robots")).json()
            moving = next(r for r in fleet if r["robot_id"] == f"robot{first}")
            if moving["occupied_edge"] is not None:
                in_flight_submission = {k: moving[k] for k in ("robot_id", "occupied_edge", "goal_node", "status")}
                break
            await asyncio.sleep(.05)
        assert in_flight_submission, "The first robot never started"
        tasks.extend(asyncio.create_task(goal(i, node)) for i, node in enumerate(goals, 1) if i != first)
    else:
        tasks = [asyncio.create_task(goal(i, node)) for i, node in enumerate(goals, 1)]
    samples, minimum = [], math.inf
    first_junction_entry, junction_violations = {}, []
    while time.monotonic() - begun < timeout:
        robots = (await client.get("/api/robots")).json()
        minimum = min(minimum, *(math.dist((a["x"], a["y"]), (b["x"], b["y"]))
                                 for i, a in enumerate(robots) for b in robots[i+1:]))
        if name.startswith("three_way_n7"):
            inside = [r["robot_id"] for r in robots if r["occupied_node"] == "7" or math.dist((r["x"], r["y"]), nodes["7"]) <= .03]
            for rid in inside:
                first_junction_entry.setdefault(rid, round(time.monotonic()-begun, 3))
            if len(inside) > 1:
                junction_violations.append({"elapsed": round(time.monotonic()-begun, 3), "robots": inside})
        samples.append({"elapsed": round(time.monotonic()-begun, 2), "robots": [
            {k: r.get(k) for k in ("robot_id", "x", "y", "status", "occupied_node", "occupied_edge", "goal_node", "kev_maneuver", "kev_recovery", "mapf_wait_for")}
            for r in robots
        ]})
        if all(t.done() for t in tasks) and all(r["goal_node"] is None for r in robots):
            break
        await asyncio.sleep(.05 if name.startswith("three_way_n7") else .2)
    timed_out = any(not t.done() for t in tasks) or any(r["goal_node"] is not None for r in robots)
    if timed_out:
        for i in range(1, 4):
            await client.post("/api/command/stop", json={"robot_id": f"robot{i}"})
    responses = await asyncio.gather(*tasks, return_exceptions=True)
    responses = [r if isinstance(r, dict) else {"error": str(r)} for r in responses]
    actual = {r["robot_id"]: r["occupied_node"] for r in robots}
    expected = {f"robot{i}": str(node) for i, node in enumerate(goals, 1)}
    completed = (not timed_out and actual == expected and all(r["goal_node"] is None for r in robots)
                 and minimum >= .12 - 1e-4
                 and all(r.get("http_status") == 200 and r["response"].get("mapf", {}).get("enabled") for r in responses))
    parallel = concurrency(samples)
    if name in ("parallel_disjoint", "parallel_staggered"):
        completed = completed and parallel["max_simultaneously_moving"] == 3 and parallel["overlapping_motion_seconds"] >= .5
    shared_three_way = []
    junction_priority_respected = True
    if name.startswith("three_way_n7"):
        for response in responses:
            for d in response.get("response", {}).get("mapf", {}).get("decisions", []):
                info = d.get("shared_resource_details", {})
                if set(info.get("robots", [])) == {"robot1", "robot2", "robot3"} and (info.get("node") == 7 or 7 in info.get("nodes", [])):
                    shared_three_way.append(d)
        junction_priority_respected = all(
            first_junction_entry.get(d["priority_robot"], math.inf) < first_junction_entry.get(d["yield_robot"], -math.inf)
            for d in shared_three_way if d["action"] == "WAIT"
        )
        completed = completed and bool(shared_three_way) and len(first_junction_entry) == 3 and not junction_violations and junction_priority_respected
    print(name, "completed=", completed, "final=", actual, flush=True)
    return {"case": name, "completed": completed, "elapsed_seconds": round(time.monotonic()-begun, 2),
            "expected": expected, "actual": actual, "minimum_sampled_separation_m": minimum,
            "responses": responses, "samples": samples, "in_flight_submission": in_flight_submission,
            "first_n7_entry_seconds": first_junction_entry, "junction_violations": junction_violations,
            "three_robot_shared_zone_decisions": shared_three_way, "junction_priority_respected": junction_priority_respected, **parallel}


async def order_case(client, nodes, timeout):
    await reposition(client, [0, 1, 2], nodes)
    created = []
    for index, items in enumerate([{"A": 0, "B": 1, "C": 0, "D": 0},
                                    {"A": 0, "B": 0, "C": 1, "D": 0},
                                    {"A": 0, "B": 1, "C": 1, "D": 0}], 1):
        r = await client.post("/api/orders", json={
            "order_name": f"MAPF-VERIFY-{time.time_ns()}-{index}", "items": items,
            "total_quantity": sum(items.values()), "operation_mode": "ai",
            "selector": "kev", "model": "kev-08b",
        })
        r.raise_for_status()
        created.append(r.json())
    ids = {o["order_id"] for o in created}
    begun, samples = time.monotonic(), []
    while time.monotonic()-begun < timeout:
        orders = [o for o in (await client.get("/api/orders")).json()["orders"] if o["order_id"] in ids]
        robots = (await client.get("/api/robots")).json()
        samples.append({"elapsed": round(time.monotonic()-begun, 2), "orders": orders,
                        "robots": [{k: r.get(k) for k in ("robot_id", "x", "y", "status", "occupied_node", "goal_node")} for r in robots]})
        if (all(o["status"] == "COMPLETED" and o["phase"] == "COMPLETED" for o in orders)
                and all(r["goal_node"] is None and r["occupied_node"] == str(i) for i, r in enumerate(robots))):
            break
        if any(o["status"] in ("FAILED", "CANCELLED") for o in orders):
            break
        await asyncio.sleep(.5)
    completed = (len(orders) == 3 and all(o["status"] == "COMPLETED" and o["phase"] == "COMPLETED" for o in orders)
                 and all(r["goal_node"] is None and r["occupied_node"] == str(i) for i, r in enumerate(robots)))
    print("orders", "completed=", completed, [(o["order_name"], o["status"]) for o in orders], flush=True)
    return {"case": "orders", "completed": completed, "created": created, "final_orders": orders, "samples": samples, **concurrency(samples)}


async def main(args):
    graph = json.loads((ROOT / "routes/test.geojson").read_text())
    nodes = {f["properties"]["id"]: f["geometry"]["coordinates"] for f in graph["features"] if f["geometry"]["type"] == "Point"}
    nodes = {str(k): v for k, v in nodes.items()}
    async with httpx.AsyncClient(base_url=args.url, timeout=args.timeout+30) as client:
        mode = (await client.get("/api/mode")).json()
        assert mode["mode"] == "simulation" and mode["operation_mode"] == "ai" and mode["simulation_mapf_enabled"]
        results = []
        for name in args.cases:
            result = await order_case(client, nodes, args.timeout) if name == "orders" else await manual_case(client, name, *CASES[name], nodes, args.timeout)
            results.append(result)
            args.output.write_text(json.dumps({"mode": mode, "real_robot_commands_sent": False, "results": results}, ensure_ascii=False, indent=2)+"\n")
            if not result["completed"]:
                raise SystemExit("FMS validation failed; inspect saved responses and backend log")
            if name == "orders" and result["max_simultaneously_moving"] < 2:
                raise SystemExit("Orders completed, but no parallel movement was observed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--output", type=Path, default=Path("/tmp/kev-mapf-fms-validation.json"))
    parser.add_argument("--cases", nargs="+", choices=[*CASES, "orders"], default=[*CASES, "orders"])
    asyncio.run(main(parser.parse_args()))
