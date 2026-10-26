"""Apply one KEV fleet decision to its pending cohort, with zone-local WAIT.

Normal progress along an already committed route does not invalidate planning.
Changes to commands, maps, pending request IDs or idle occupancy still do.
"""

import asyncio
from copy import deepcopy
import logging
from time import monotonic
from uuid import uuid4

from .kev_adapter import plan_mapf_from_route_results
from .kev_coordinator import _is_valid_suffix, is_fms_reviewable_advisory
from .kev_graph import Graph
from ..reservation import edge_key, node_key

logger = logging.getLogger("uvicorn.error")


def fleet_snapshot(execution, c):
    with c.transaction(execution):
        robots, _, protected = execution.snapshot()
        active, pending = c.requests_by_robot()
        identity, requests = {}, []
        idle_nodes = []
        for rid, r in robots.items():
            if rid in pending:
                req = pending[rid]
                if (r["status"] != "IDLE" or r.get("goal_node") is not None or r.get("route") is not None
                        or r.get("execution_blocked") or (r.get("stop_requested") and execution.mode != "simulation")
                        or ("execution_epoch" in req and req["execution_epoch"] != r.get("command_epoch"))
                        or r.get("connection_state", "ONLINE") != "ONLINE"
                        or r.get("occupied_edge") is not None or str(r["occupied_node"]) != str(req["current_node"])):
                    return None
                requests.append(deepcopy(req))
                identity[rid] = ("pending", req, r.get("stop_requested"), r.get("connection_state", "ONLINE"), r.get("command_epoch"))
            elif r.get("goal_node") is not None:
                req, route = active.get(rid), r.get("route")
                if (req is None or r.get("kev_maneuver") or not route or r.get("stop_requested")
                        or r.get("connection_state", "ONLINE") != "ONLINE"):
                    return None
                nodes = [str(n) for n in route["node_ids"]]
                remaining = nodes[route["segment_index"]:]
                if not _is_valid_suffix(req["result"]["path"], remaining):
                    return None
                item = deepcopy(req)
                item.update(current_node=remaining[0], result={"path": remaining},
                            status=r["status"], execution_committed=True, occupied_edge=r.get("occupied_edge"))
                requests.append(item)
                # Position, segment index, phase and reservation retry times are
                # expected to change while the same command is being followed.
                identity[rid] = ("active", req, r["navigation_id"], r["goal_node"],
                                 r.get("stop_requested"), r.get("connection_state", "ONLINE"), r.get("command_epoch"))
            else:
                identity[rid] = ("idle", r.get("occupied_node"), r.get("occupied_edge"),
                                 r.get("connection_state", "ONLINE"))
                if r.get("occupied_node") is not None:
                    idle_nodes.append(int(r["occupied_node"]))
        return {"robots": robots, "pending": pending, "requests": requests,
                "identity": identity, "blocked_nodes": idle_nodes,
                "protected": protected}


def wait_resources(info):
    if info.get("kind") == "node":
        return [list(node_key(info["node"]))]
    if info.get("kind") == "edge":
        return [list(edge_key(*info["edge"]))]
    if info.get("kind") == "group":
        return ([list(node_key(n)) for n in info["nodes"]]
                + [list(edge_key(*edge)) for edge in info["edges"]])
    raise ValueError("MAPF WAIT is missing its shared resource")


def dispatch_order(pending_ids, decisions):
    """Topologically apply the priority relations KEV already chose."""
    remaining = set(pending_ids)
    before = {rid: set() for rid in remaining}
    for d in decisions:
        if d["action"] == "WAIT" and d["yield_robot"] in before and d["priority_robot"] in remaining:
            before[d["yield_robot"]].add(d["priority_robot"])
    ordered = []
    while remaining:
        ready = sorted(rid for rid in remaining if not before[rid] & remaining)
        if not ready:
            raise ValueError("KEV priority relations form a cycle")
        ordered.extend(ready)
        remaining.difference_update(ready)
    return ordered


class ParallelAdmission:
    """같은 cohort의 추론을 공유하고 각 요청에 결과를 한 번만 전달한다."""

    def __init__(self, coordinator):
        self._coordinator = coordinator
        self._lock = None
        self._outcomes = {}
        self._validators = {}
        self._attempt = None

    def register(self, request_id, valid):
        self._validators[request_id] = valid

    def finish(self, request_id):
        self._validators.pop(request_id, None)
        self._outcomes.pop(request_id, None)

    def outcome_snapshot(self):
        return deepcopy(self._outcomes)

    def apply(self, execution, graph, snapshot, advisory):
        c = self._coordinator
        if not is_fms_reviewable_advisory(advisory):
            return False
        pending = snapshot["pending"]
        paths = advisory["required_paths"]
        requests = {req["order"]["robot_id"]: req for req in snapshot["requests"]}
        if set(paths) != set(requests):
            return False
        waits = {rid: [] for rid in pending}
        for d in advisory["decisions"]:
            rid = d.get("yield_robot")
            if rid not in pending or d["action"] not in ("WAIT", "REROUTE"):
                return False  # Never change an active robot's approved command.
            if d["action"] == "WAIT":
                if str(d["wait_node"]) != str(pending[rid]["current_node"]):
                    return False
                high = d["priority_robot"]
                if str(d["wait_node"]) in paths[high]:
                    return False  # A parked robot must not obstruct the priority path.
                waits[rid].append({"robot_id": high, "resources": wait_resources(d["shared_resource_details"])})
        for rid, req in requests.items():
            if rid not in pending and paths[rid] != req["result"]["path"]:
                return False
            Graph(graph).check_path(paths[rid], start=int(req["current_node"]), goal=int(req["node"]))

        with c.transaction(execution):
            latest = fleet_snapshot(execution, c)
            if latest is None or latest["identity"] != snapshot["identity"] or latest["protected"] != snapshot["protected"]:
                return False
            for req in pending.values():
                valid = self._validators.get(req["request_id"])
                try:
                    if valid is not None and not valid():
                        return False
                except Exception:
                    return False
            responses = execution.commit_routes(
                graph, pending, paths, waits, dispatch_order(pending, advisory["decisions"]), now=monotonic(),
            )
            if responses is None:
                return False
            plan_id = uuid4().hex
            for rid, req in pending.items():
                c.register_preview(req["order"], req["current_node"], req["node"], {"path": paths[rid]})
                c.discard_pending(rid)
                response = responses[rid]
                response.update(llm_preview={"path": paths[rid]}, mapf={
                    "enabled": True, "analysis_source": "MIXED_ACTIVE_PENDING_PARALLEL",
                    "plan_id": plan_id, "robot_count": len(requests), "decisions": advisory["decisions"],
                    "input_paths": {member: req["result"]["path"] for member, req in requests.items()},
                    "priority_mode": advisory["priority_mode"], "priority_scores": advisory["priority_scores"],
                    "metrics": advisory.get("metrics", {}), "zone_wait_for": waits[rid],
                })
                self._outcomes[req["request_id"]] = response
            logger.info("KEV MAPF parallel cohort: plan=%s robots=%s decisions=%s metrics=%s",
                        plan_id, sorted(pending), advisory["decisions"], advisory.get("metrics"))
            return True


    async def admit(self, execution, graph, prepared, *, config):
        c = self._coordinator
        if self._lock is None:
            self._lock = asyncio.Lock()
        request_id = prepared["request"]["request_id"]
        if request_id in self._outcomes:
            return self._outcomes.pop(request_id)
        async with self._lock:
            if request_id in self._outcomes:
                return self._outcomes.pop(request_id)
            snapshot = fleet_snapshot(execution, c)
            if snapshot is None or not snapshot["pending"]:
                return None
            key = (snapshot["identity"], snapshot["protected"], graph)
            cached = self._attempt
            if cached and cached[0] == key and monotonic() < cached[1]:
                return None
            settings = {**config, "wait_allowed_nodes": [int(req["current_node"]) for req in snapshot["pending"].values()],
                        "blocked_nodes": snapshot["blocked_nodes"]}
            advisory = await asyncio.to_thread(plan_mapf_from_route_results, graph, snapshot["requests"], config=settings)
            from ..route_graph import load_route_graph
            from ..mode_service import mode_manager
            if mode_manager.mode != execution.mode or load_route_graph() != graph:
                return None
            self._attempt = (deepcopy(key), monotonic()+1.0)
            applied = self.apply(execution, graph, snapshot, advisory)
            if applied:
                return self._outcomes.pop(request_id, None)
            return None
