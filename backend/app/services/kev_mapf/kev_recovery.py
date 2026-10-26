"""시뮬레이션의 고정 AI 경로 막힘을 KEV 판단으로 복구한다.

추론은 실행기 잠금 밖에서 수행한다. 어떤 로봇을 재경로할지는 KEV가
선택하며, FMS는 최신 점유와 예약을 검증하고 기존 작업을 보존한다.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
import logging
from time import monotonic

from ..llm_route_preview import preview_llm_route
from ..iterative_route_policy import MAJOR_TASK_NODES
from ..route_selector.kev_selector import KevSelector
from .kev_coordinator import _is_valid_suffix
from .kev_graph import Graph
from .kev_traffic import live_traffic

logger = logging.getLogger(__name__)

RECOVERY_INSTRUCTIONS = (
    "You are the fleet recovery planner. Decide WHICH robot should move and "
    "WHERE it should go using the supplied current nodes, original goals and "
    "remaining paths. Infer which robots block one another. Prefer releasing "
    "a node needed by another robot in a mutual blockage. Moving a follower "
    "that blocks nobody may leave the actual deadlock unresolved. "
    "MOVE_ASIDE selects a temporary neighboring node and preserves the task. "
    "REROUTE follows a new route to the original goal. All supplied actions "
    "are topologically valid and avoid current occupancy, but their usefulness "
    "is YOUR decision. Compare them using the full fleet state. Avoid repeating "
    "unproductive maneuvers shown in previous_decisions. RESUME completes a "
    "temporary maneuver by following a validated path to the original goal. "
    "Prefer RESUME over further displacement when its path is available. "
    "A temporary stop is not task completion. Waiting cannot free "
    "stationary mutual blockers. FMS checks reservations and collision safety "
    "before execution. Return one supplied action ID."
)



class KevRecovery:
    def __init__(self, *, stall_seconds=1.0, retry_seconds=5.0):
        self.stall_seconds = stall_seconds
        self.retry_seconds = retry_seconds
        self._fingerprint = None
        self._since = 0.0
        self._next_attempt = 0.0
        self._running = False

    @staticmethod
    def _capture(store, coordinator):
        # Same lock order as the MAPF dispatcher.
        with coordinator._lock, store._lock:
            return (
                deepcopy(store._robots),
                dict(store._traffic._required_paths),
                deepcopy(coordinator._route_requests),
                deepcopy(store._traffic._protected_resources),
            )

    @staticmethod
    def _progress_signature(snapshot):
        robots, paths, registered, protected = snapshot
        # Reservation retries refresh timestamps without moving a robot. They
        # must neither hide a stall nor invalidate a KEV plan. Commit always
        # validates against the newest reservation table under the store lock.
        fields = ("x", "y", "occupied_node", "occupied_edge", "goal_node",
                  "navigation_id", "stop_requested", "connection_state", "kev_maneuver")
        physical = {
            rid: {**{field: r.get(field) for field in fields},
                  "route_nodes": (r.get("route") or {}).get("node_ids")}
            for rid, r in robots.items()
        }
        return deepcopy((physical, paths, registered, protected))

    @staticmethod
    def _candidates(snapshot, now):
        robots, paths, registered, protected = snapshot
        blocked = {}
        for rid, path in paths.items():
            robot = robots[rid]
            req = registered.get(rid)
            route = robot.get("route") or {}
            maneuver = robot.get("kev_maneuver")
            if (
                not req or req["order"]["selector"] != "kev"
                or not _is_valid_suffix(req["result"]["path"], list(path))
                or str(req["node"]) != str(robot.get("goal_node"))
                or robot["status"] != "WAITING" or robot.get("stop_requested")
                or robot.get("connection_state", "ONLINE") != "ONLINE"
                or robot.get("occupied_node") is None or robot.get("occupied_edge") is not None
                or (route.get("departure_at") is not None and route["departure_at"] > now)
                or rid in protected
                or (maneuver and maneuver["phase"] != "ASIDE")
            ):
                continue
            if maneuver:
                blocked[rid] = []  # A parked maneuver needs a KEV RESUME decision.
                continue
            blockers = [
                other_id for other_id, other in robots.items()
                if other_id != rid and other.get("occupied_node") in path[1:]
                and other.get("status") in ("WAITING", "IDLE")
                and (other.get("route") or {}).get("departure_at") is None
            ]
            if blockers:
                blocked[rid] = blockers
        return blocked

    @staticmethod
    def _aside_candidates(graph, snapshot, blocked):
        """Expose legal free neighboring nodes; KEV decides their usefulness.

        No policy here computes which robot/node combination breaks the cycle.
        """
        robots, paths, _, _ = snapshot
        route_graph = Graph(graph)
        allowed = route_graph.declared_wait_nodes() or (set(route_graph.nodes) - MAJOR_TASK_NODES)
        proposals = {}
        for rid in blocked:
            robot = robots[rid]
            if robot.get("kev_maneuver"):
                continue
            current = int(robot["occupied_node"])
            traffic = live_traffic(rid, graph=graph, snapshots=list(robots.values()))
            forbidden = set(traffic["occupied_nodes"])
            forbidden.update(int(r["goal_node"]) for r in robots.values() if r.get("goal_node") is not None)
            for a, target in route_graph.edges:
                if a != current or target not in allowed or target in forbidden or target == current:
                    continue
                key = f"move_aside_{rid}_{target}"
                proposals[key] = (rid, [str(current), str(target)], str(target))
        return proposals

    @staticmethod
    def _plan(graph, snapshot, blocked):
        """Read-only KEV choice and route generation; never changes fleet state."""
        robots, paths, registered, _ = snapshot
        proposals = {}
        rejected = []
        for rid in sorted(blocked):
            robot = robots[rid]
            action = "resume" if robot.get("kev_maneuver") else "reroute"
            key = f"{action}_{rid}"
            try:
                traffic = live_traffic(rid, graph=graph, snapshots=list(robots.values()))
                # Recovery never restores occupied candidates when no detour exists.
                forbidden = set(traffic["occupied_nodes"])
                if int(robot["goal_node"]) in forbidden:
                    raise ValueError("original goal is currently occupied")
                virtual = Graph(graph).virtual_graph(forbidden, set())
                result = preview_llm_route(
                    virtual, robot["occupied_node"], robot["goal_node"],
                    selector="kev", model=registered[rid]["order"]["model"], traffic=traffic,
                )
                path = [str(n) for n in result["path"]]
                Graph(virtual).check_path(
                    path, start=int(robot["occupied_node"]), goal=int(robot["goal_node"])
                )
                if path == list(paths[rid]):
                    raise ValueError("reroute did not change the blocked path")
                proposals[key] = (rid, path, None)
            except (ValueError, RuntimeError) as exc:
                rejected.append({"action": key, "reason": str(exc)})
        proposals.update(KevRecovery._aside_candidates(graph, snapshot, blocked))
        if not proposals:
            logger.warning("KEV recovery has no feasible move; blocked=%s reasons=%s", blocked, rejected)
            return None
        choices = {
            key: f"{'MOVE_ASIDE' if target is not None else 'RESUME' if key.startswith('resume_') else 'REROUTE'} "
                 f"{rid} via {path}; original goal={robots[rid]['goal_node']}"
            for key, (rid, path, target) in proposals.items()
        }
        choices["wait"] = "Wait without moving; the mutual blockage remains."
        # Raw fleet context is necessary: KEV, rather than a candidate-ranking
        # algorithm, determines which legal action actually resolves the block.
        state = {
            "all_robots_stationary": all(r["status"] in ("WAITING", "IDLE") for r in robots.values()),
            "waiting_will_clear_blockage": False,
            "blocked_by": blocked,
            "robots": [
                {"id": rid, "current_node": r.get("occupied_node"),
                 "original_goal": r.get("goal_node"), "remaining_path": list(paths.get(rid, ())),
                 "temporary_maneuver": r.get("kev_maneuver")}
                for rid, r in sorted(robots.items())
            ],
            "valid_maneuvers": [
                {"id": key, "robot_id": rid, "path": list(map(int, path)),
                 "original_goal": int(robots[rid]["goal_node"]),
                 "directed_path_valid": True, "avoids_current_occupancy": True,
                 "temporary_move_complete": bool(robots[rid].get("kev_maneuver")),
                 "action": "MOVE_ASIDE" if target is not None else "RESUME" if key.startswith("resume_") else "REROUTE"}
                for key, (rid, path, target) in proposals.items()
            ],
            "previous_decisions": {rid: r["kev_recovery"] for rid, r in robots.items() if r.get("kev_recovery")},
            "full_detours_available": any(key.startswith("reroute_") for key in proposals),
            "rejected_attempts": rejected,
        }

        model = registered[next(iter(proposals.values()))[0]]["order"]["model"]
        selector = KevSelector(model=model, timeout_seconds=20)
        decision = selector.select_choice(
            state, choices, RECOVERY_INSTRUCTIONS, question_id="deadlock_recovery"
        )
        key = decision["choice"]
        if key == "wait":
            logger.warning("KEV selected WAIT; blocked=%s valid_actions=%s", blocked, list(proposals))
            return None
        if key not in proposals:
            raise ValueError("KEV returned an unknown recovery action")
        rid, path, target = proposals[key]
        action = "MOVE_ASIDE" if target is not None else "RESUME" if key.startswith("resume_") else "REROUTE"
        return rid, path, {**decision, "action": action, "temporary_goal": target}

    async def run_once(self, store, coordinator, mode_manager, *, now=None):
        if self._running:
            return False
        if mode_manager.mode != "simulation":
            self._fingerprint = None
            return False
        now = monotonic() if now is None else now
        snapshot = self._capture(store, coordinator)
        signature = self._progress_signature(snapshot)
        blocked = self._candidates(snapshot, now)
        if not blocked:
            self._fingerprint = None
            return False
        # Includes all positions, requests, plans and protected resources so
        # cancellation/replacement cannot resurrect an old inference result.
        if signature != self._fingerprint:
            self._fingerprint = signature
            self._since = now
            self._next_attempt = now
            return False
        if now - self._since < self.stall_seconds or now < self._next_attempt:
            return False
        self._running = True
        self._next_attempt = now + self.retry_seconds
        from ..route_graph import load_route_graph

        try:
            graph = load_route_graph()
            proposal = await asyncio.to_thread(self._plan, graph, snapshot, blocked)
            if proposal is None or mode_manager.mode != "simulation" or load_route_graph() != graph:
                return False
            with coordinator._lock, store._lock:
                if self._progress_signature(self._capture(store, coordinator)) != signature:
                    return False
                rid, path, decision = proposal
                if not store._traffic.replace_required_path_after_stop(
                    rid, path, now=monotonic(), graph=graph,
                    temporary_goal=decision.get("temporary_goal"),
                ):
                    return False
                request = coordinator._route_requests[rid]
                request["current_node"] = str(store._robots[rid]["occupied_node"])
                request["result"] = {"path": path}
                store._robots[rid]["kev_recovery"] = {
                    "action": decision["action"], "path": path,
                    "choice": decision["choice"], "confidence": decision.get("confidence"),
                }
                logger.info("KEV recovery applied: robot=%s path=%s", rid, path)
                self._fingerprint = None
                return True
        except Exception:
            # Inference/validation failure never grants movement or picks an
            # algorithmic substitute. A later attempt uses fresh fleet state.
            logger.exception("KEV recovery failed; keeping existing route and occupancy")
            return False
        finally:
            self._running = False


kev_recovery = KevRecovery()
