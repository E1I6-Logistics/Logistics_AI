"""출발 전 MAPF가 보류한 요청도 KEV의 대피·재개 선택에 포함한다.

MAPF 본체를 통과하지 못한 경로를 그대로 실행하지 않는다. KEV가 별도로
선택한 합법적인 경로만 FMS 예약 검사를 거쳐 실행한다.
"""

from copy import deepcopy
import logging
from time import monotonic

from .kev_recovery import KevRecovery, KevRecoveryPlanner

logger = logging.getLogger("uvicorn.error")


def admission_snapshot(store, coordinator):
    with coordinator.transaction(store.mapf):
        robots, paths, protected = store.mapf.snapshot()
        registered, pending = coordinator.requests_by_robot()
        if registered.keys() & pending.keys():
            return None
        registered.update(pending)
        for rid, req in pending.items():
            r = robots[rid]
            if (r["status"] != "IDLE" or r.get("goal_node") is not None
                    or r.get("route") is not None or r.get("occupied_edge") is not None
                    or r.get("execution_blocked") or (r.get("stop_requested") and store.mapf.mode != "simulation")
                    or ("execution_epoch" in req and req["execution_epoch"] != r.get("command_epoch"))
                    or r.get("connection_state", "ONLINE") != "ONLINE"):
                return None
            # Planning context only; actual FMS state remains IDLE until commit.
            r["mapf_pending"] = True
            r["goal_node"] = req["node"]
            paths[rid] = tuple(req["result"]["path"])
        return robots, paths, registered, protected


def plan_admission_recovery(graph, snapshot, *, planner=None):
    robots, paths, registered, protected = snapshot
    eligible = {
        rid: [other for other, r in robots.items()
              if other != rid and r.get("occupied_node") in paths[rid][1:]]
        for rid, r in robots.items()
        if rid in registered and rid in paths and rid not in protected
        and registered[rid]["order"]["selector"] == "kev"
        and r.get("occupied_node") is not None and r.get("occupied_edge") is None
        and r.get("connection_state", "ONLINE") == "ONLINE"
        and not r.get("execution_blocked")
        and (r.get("mapf_pending") or (
            r["status"] == "WAITING" and not r.get("stop_requested")
            and (not r.get("kev_maneuver") or r["kev_maneuver"]["phase"] == "ASIDE")
        ))
    }
    return (planner or KevRecoveryPlanner()).plan(graph, snapshot, eligible) if eligible else None


def apply_admission_recovery(store, coordinator, graph, snapshot, proposal, *, robot_id, prepared, valid):
    if proposal is None:
        return None
    rid, path, decision = proposal
    # Each pending request owns its HTTP/order task. Only that task starts it.
    # Active resume/recovery can be applied by either monitor after revalidation.
    if snapshot[0][rid].get("mapf_pending") and rid != robot_id:
        return None
    with coordinator.transaction(store.mapf):
        if valid is not None and not valid():
            return None
        latest = admission_snapshot(store, coordinator)
        if latest is None or KevRecovery.progress_signature(latest) != KevRecovery.progress_signature(snapshot):
            return None
        if not coordinator.pending_matches(robot_id, prepared["request"]):
            return None
        was_pending = bool(snapshot[0][rid].get("mapf_pending"))
        req = snapshot[2][rid]
        response = store.mapf.recover(
            rid, path, decision, graph, now=monotonic(), initial_goal=req["node"] if was_pending else None,
        )
        if response is None:
            return None
        if was_pending:
            coordinator.register_preview(req["order"], req["current_node"], req["node"], {"path": path})
            coordinator.discard_pending(rid)
        else:
            coordinator.update_active_path(rid, snapshot[0][rid]["occupied_node"], path)
        logger.info("KEV MAPF admission recovery: robot=%s action=%s path=%s", rid, decision["action"], path)
        if was_pending:
            return dict(response, llm_preview={**prepared["result"], "path": path}, mapf={
                "enabled": True, "analysis_source": "KEV_PENDING_RECOVERY",
                "robot_count": len(snapshot[2]), "decisions": [decision],
                "business_goal": req["node"],
            })
        return None
