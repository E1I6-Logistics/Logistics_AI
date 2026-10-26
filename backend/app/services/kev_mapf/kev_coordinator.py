"""[모듈: kev_coordinator.py - FMS와 KEV-MAPF 사이의 경로 수집 및 조정 모듈]
- 역할:
  1. 시뮬레이션/FMS 내 주행 중인 로봇(Active)과 출발 대기 중인 로봇(Pending)의 경로·상태를 수집 및 동기화한다.
  2. 수집된 경로들을 KEV-MAPF(`plan_mapf_from_route_results`)에 전달하여 충돌 회피 제안을 받아온다.
  3. 분석 전후 FMS 시그니처 비교를 통해 상태 불일치(Stale)를 감지하고, 안전한 출발 후보(`fms_check_candidates`)와 보류 대상(`deferred`)을 분류한다.
  4. REROUTE(우회) 성공 로봇은 변경된 새 경로의 유효성을 확인한 뒤 보류하지 않고 출발 후보(`fms_check_candidates`)에 포함시킨다.
  5. 실제 주행 승인(`fms_execution_approved`)은 항상 False로 유지하며, 최종 예약 및 이동 명령은 FMS `TrafficManager`에 위임한다.
"""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from threading import RLock
from typing import Any
from uuid import uuid4

from .kev_client import KevPlanner
from .kev_adapter import mapf_robot_from_route_result, plan_mapf_from_route_results



def _blocked_response(reason: str, *, issues: dict | None = None, **extra: Any) -> dict[str, Any]:
    """공통 BLOCKED 응답 딕셔너리를 생성한다."""
    res: dict[str, Any] = {"status": "BLOCKED", "reason": reason}
    if issues is not None:
        res["issues"] = issues
    res.update(extra)
    res["fms_execution_approved"] = False
    return res


def _is_valid_suffix(original: list[str], remaining: list[str] | None) -> bool:
    """남은 경로(`remaining`)가 최초 등록 경로(`original`)의 유효한 접미사(Suffix)인지 O(N)으로 검사한다."""
    if not remaining or len(remaining) > len(original):
        return False
    return original[len(original) - len(remaining) :] == remaining


def is_fms_reviewable_advisory(advisory: dict) -> bool:
    """MAPF 분석 결과가 FMS 예약 검토 단계로 진입 가능한지 확인한다."""
    status = advisory.get("status")
    if status == "PROPOSED":
        return True
    if status != "REVIEW_REQUIRED":
        return False

    decisions = advisory.get("decisions")
    warnings = advisory.get("warnings")
    if not isinstance(decisions, list) or not decisions:
        return False
    if not isinstance(warnings, list) or not warnings:
        return False

    for d in decisions:
        if not isinstance(d, dict):
            return False
        action = d.get("action")
        if action == "WAIT":
            if d.get("wait_node_verified") is not True or d.get("wait_node") is None:
                return False
        elif action != "REROUTE":
            return False

    return all(
        isinstance(w, str)
        and ": WAIT node " in w
        and w.endswith("; FMS traffic reservation verification required")
        for w in warnings
    )


class FmsMapfCoordinator:
    """Active 및 Pending 로봇 경로를 관리하고 KEV-MAPF 판단과 FMS 상태를 조율하는 코디네이터."""

    def __init__(self, store) -> None:
        self._store = store
        self._lock = RLock()
        self._route_requests: dict[str, dict] = {}
        self._pending_requests: dict[str, dict] = {}
        from .kev_parallel_admission import ParallelAdmission
        self.admission = ParallelAdmission(self)

    @property
    def store(self):
        return self._store

    @contextmanager
    def transaction(self, execution):
        # Registry precedes execution in every commit, including ROS callbacks.
        with self._lock, execution.transaction():
            yield

    def requests_by_robot(self):
        with self._lock:
            return deepcopy(self._route_requests), deepcopy(self._pending_requests)

    def pending_matches(self, robot_id, request):
        with self._lock:
            return self._pending_requests.get(robot_id) == request

    def update_active_path(self, robot_id, current_node, path):
        with self._lock:
            request = self._route_requests[robot_id]
            request["current_node"] = str(current_node)
            request["result"] = {"path": list(path)}

    def finish_request(self, robot_id, request):
        with self._lock:
            self.admission.finish(request["request_id"])
            if self._pending_requests.get(robot_id) == request:
                self._pending_requests.pop(robot_id)

    # -------------------------------------------------------------------------
    # 1. 내부 상태 스냅샷 및 등록/삭제 관리
    # -------------------------------------------------------------------------

    def _build_request(
        self, order: dict, current_node: int | str, goal_node: int | str, result: dict
    ) -> tuple[str, dict]:
        """주문과 경로 결과를 정규화된 MAPF 요청 포맷으로 변환한다."""
        robot = mapf_robot_from_route_result(
            robot_id=order["robot_id"],
            current_node=current_node,
            goal_node=goal_node,
            result=result,
        )
        rid = robot["id"]
        req = {
            "order": {
                "robot_id": rid,
                "selector": order["selector"],
                "model": order["model"],
            },
            "current_node": robot["current_node"],
            "node": robot["goal_node"],
            "result": {"path": [str(n) for n in robot["path"]]},
        }
        return rid, req

    def register_preview(
        self, order: dict, current_node: int | str, goal_node: int | str, result: dict
    ) -> dict:
        """주행 시작된 로봇의 경로를 Active 목록에 등록한다."""
        rid, req = self._build_request(order, current_node, goal_node, result)
        with self._lock:
            self._route_requests[rid] = deepcopy(req)
        return deepcopy(req)

    def stage_preview(
        self, order: dict, current_node: int | str, goal_node: int | str, result: dict,
        *, execution_epoch=None,
    ) -> dict:
        """실제 주행 요청 전 AI가 생성한 경로를 Pending 목록에 임시 등록한다."""
        rid, req = self._build_request(order, current_node, goal_node, result)
        # Identical replacement routes still belong to different requests.
        req["request_id"] = uuid4().hex
        if execution_epoch is not None:
            req["execution_epoch"] = deepcopy(execution_epoch)
        with self._lock:
            self._pending_requests[rid] = deepcopy(req)
        return deepcopy(req)

    def snapshot(self) -> list[dict]:
        """현재 등록된 Active 로봇 경로 목록의 복사본을 반환한다."""
        with self._lock:
            return deepcopy(list(self._route_requests.values()))

    def pending_snapshot(self) -> list[dict]:
        """현재 대기 중인 Pending 로봇 경로 목록의 복사본을 반환한다."""
        with self._lock:
            return deepcopy(list(self._pending_requests.values()))

    def remove(self, robot_id: str) -> None:
        """작업이 종료되거나 취소된 로봇의 Active 경로를 제거한다."""
        with self._lock:
            self._route_requests.pop(str(robot_id), None)

    def discard_pending(self, robot_id: str) -> None:
        """취소되거나 주행 전환된 로봇의 Pending 경로를 삭제한다."""
        with self._lock:
            self._pending_requests.pop(str(robot_id), None)

    def state_triplet(self) -> tuple[dict, list[dict], list[dict]]:
        """현재 시점의 (FMS 시그니처, Pending 스냅샷, Active 스냅샷) 3중 상태를 캡처한다."""
        return self.state_signature(), self.pending_snapshot(), self.snapshot()

    # -------------------------------------------------------------------------
    # 2. FMS 시뮬레이션 상태 조회 및 완료 경로 정리
    # -------------------------------------------------------------------------

    def state_signature(self) -> dict:
        """FMS 실행 상태의 변경 여부를 확인하기 위한 스냅샷 시그니처를 생성한다."""
        store = self.store

        signature = {}
        for robot in store.robot_snapshots(getattr(store, "mode", "simulation")):
            rid = str(robot["robot_id"])
            route = robot.get("route")
            is_dict = isinstance(route, dict)
            signature[rid] = {
                "status": robot.get("status"),
                "connection_state": robot.get("connection_state"),
                "stop_requested": robot.get("stop_requested"),
                "occupied_node": robot.get("occupied_node"),
                "occupied_edge": robot.get("occupied_edge"),
                "goal_node": robot.get("goal_node"),
                "navigation_id": robot.get("navigation_id"),
                "route_nodes": deepcopy(route.get("node_ids")) if is_dict else None,
                "segment_index": route.get("segment_index") if is_dict else None,
                "route_phase": route.get("phase") if is_dict else None,
                "route_departure_at": route.get("departure_at") if is_dict else None,
                "segment_departures": deepcopy(route.get("segment_departures")) if is_dict else None,
            }
        return signature

    def inspect_state(self) -> dict:
        """FMS 시뮬레이션의 실제 점유와 남은 경로(`remaining_path`)를 조회한다."""
        store = self.store

        states = {}
        for robot in store.robot_snapshots(getattr(store, "mode", "simulation")):
            rid = str(robot["robot_id"])
            occupied_node = robot.get("occupied_node")
            occupied_edge = robot.get("occupied_edge")
            route = robot.get("route")
            remaining_path = None
            issue = None

            if robot.get("kev_maneuver"):
                # Temporary route endpoint differs from the business goal.
                # Defer admission until KEV has installed the resume route.
                issue = "KEV_MOVE_ASIDE_ACTIVE"
            elif occupied_edge is not None:
                issue = "MID_EDGE"
            elif not isinstance(route, dict):
                issue = "NO_ACTIVE_ROUTE"
            else:
                nodes = route.get("node_ids")
                idx = route.get("segment_index")
                if (
                    not isinstance(nodes, list)
                    or not isinstance(idx, int)
                    or isinstance(idx, bool)
                    or not (0 <= idx < len(nodes))
                ):
                    issue = "INVALID_ROUTE"
                elif occupied_node is None or str(nodes[idx]) != str(occupied_node):
                    issue = "POSITION_ROUTE_MISMATCH"
                else:
                    remaining_path = [str(n) for n in nodes[idx:]]

            states[rid] = {
                "robot_id": rid,
                "status": robot.get("status"),
                "occupied_node": occupied_node,
                "occupied_edge": occupied_edge,
                "goal_node": robot.get("goal_node"),
                "remaining_path": remaining_path,
                "issue": issue,
            }
        return states

    def retire_completed_routes(self) -> dict:
        """주행 완료가 확인된 Active 경로만 정리한다."""
        store = self.store

        before = self.state_signature()
        snapshots = store.robot_snapshots(getattr(store, "mode", "simulation"))
        if before != self.state_signature():
            return _blocked_response("FMS_STATE_CHANGED_DURING_CHECK", retired_robot_ids=[])

        states = {}
        for state in snapshots:
            rid = str(state["robot_id"])
            if rid in states:
                return _blocked_response("DUPLICATE_ROBOT_STATE", retired_robot_ids=[])
            states[rid] = state

        retired = []
        with self._lock:
            for rid, req in list(self._route_requests.items()):
                state = states.get(rid)
                if state is None or rid in self._pending_requests:
                    continue
                if (
                    state.get("status") == "IDLE"
                    and state.get("connection_state") == "ONLINE"
                    and state.get("occupied_edge") is None
                    and str(state.get("occupied_node")) == str(req["node"])
                    and state.get("goal_node") is None
                    and state.get("route") is None
                ):
                    self._route_requests.pop(rid, None)
                    retired.append(rid)

            active_ids = sorted(self._route_requests)
            pending_ids = sorted(self._pending_requests)

        return {
            "status": "RECONCILED",
            "retired_robot_ids": sorted(retired),
            "active_robot_ids": active_ids,
            "pending_robot_ids": pending_ids,
            "fms_execution_approved": False,
        }

    # -------------------------------------------------------------------------
    # 3. 혼합(Active + Pending) 경로 준비 및 KEV-MAPF 평가·분류
    # -------------------------------------------------------------------------

    def prepare_mixed_requests(self) -> dict:
        """Active의 실제 남은 경로와 Pending 경로를 통합하여 MAPF 요청 목록을 구성한다."""
        fms_before, pending, active = self.state_triplet()
        live = self.inspect_state()

        requests = []
        issues = {}
        seen = set()
        pending_ids = [str(item["order"]["robot_id"]) for item in pending]
        active_ids = [str(item["order"]["robot_id"]) for item in active]

        for source, items in (("pending", pending), ("active", active)):
            for item in items:
                rid = str(item["order"]["robot_id"])
                if rid in seen:
                    issues[rid] = "DUPLICATE_OR_OVERLAPPING_REQUEST"
                    continue
                seen.add(rid)

                state = live.get(rid)
                sig = fms_before.get(rid)
                if state is None or sig is None:
                    issues[rid] = "ROBOT_NOT_FOUND"
                    continue
                if sig["connection_state"] != "ONLINE":
                    issues[rid] = "ROBOT_OFFLINE"
                    continue
                if source == "active" and sig["stop_requested"]:
                    issues[rid] = "STOP_REQUESTED"
                    continue

                if source == "pending":
                    if (
                        state["status"] != "IDLE"
                        or state["goal_node"] is not None
                        or state["occupied_edge"] is not None
                        or state["issue"] != "NO_ACTIVE_ROUTE"
                    ):
                        issues[rid] = "PENDING_ROBOT_NOT_IDLE"
                    elif str(state["occupied_node"]) != str(item["current_node"]):
                        issues[rid] = "PENDING_START_CHANGED"
                    else:
                        requests.append(deepcopy(item))
                    continue

                if state["status"] not in ("NAVIGATING", "WAITING"):
                    issues[rid] = "ACTIVE_ROBOT_NOT_RUNNING"
                elif state["issue"] is not None:
                    issues[rid] = state["issue"]
                elif str(state["goal_node"]) != str(item["node"]):
                    issues[rid] = "ACTIVE_GOAL_CHANGED"
                elif not _is_valid_suffix(
                    [str(n) for n in item["result"]["path"]], state["remaining_path"]
                ):
                    issues[rid] = "ACTIVE_ROUTE_CHANGED"
                else:
                    updated = deepcopy(item)
                    updated["current_node"] = str(state["occupied_node"])
                    updated["result"] = {"path": list(state["remaining_path"])}
                    requests.append(updated)

        planned_nodes = {str(n) for req in requests for n in req["result"]["path"]}
        for rid, state in live.items():
            if rid in seen:
                continue
            if (
                state["goal_node"] is not None
                or state["occupied_edge"] is not None
                or state["status"] in ("NAVIGATING", "WAITING")
            ):
                issues[rid] = "UNMODELED_ACTIVE_ROBOT"
            elif state["occupied_node"] is not None and str(state["occupied_node"]) in planned_nodes:
                issues[rid] = "UNMODELED_IDLE_OCCUPANCY"

        if (fms_before, pending, active) != self.state_triplet():
            issues["_global"] = "STATE_CHANGED_DURING_COLLECTION"

        return {
            "status": "READY" if len(requests) >= 2 and not issues else "BLOCKED",
            "requests": requests,
            "issues": issues,
            "active_robot_ids": sorted(active_ids),
            "pending_robot_ids": sorted(pending_ids),
            "fms_execution_approved": False,
        }

    def evaluate_mixed_advisory(
        self, graph: dict, *, planner: KevPlanner | None = None, config: dict | None = None
    ) -> dict:
        """Active + Pending 경로를 통합하여 KEV-MAPF 분석을 수행한다."""
        fms_before, pending_before, active_before = self.state_triplet()
        prepared = self.prepare_mixed_requests()

        if (fms_before, pending_before, active_before) != self.state_triplet():
            return _blocked_response(
                "STATE_CHANGED_DURING_COLLECTION",
                issues={},
                decisions=[],
                analysis_source="MIXED_ACTIVE_PENDING",
            )
        if prepared["status"] != "READY":
            return _blocked_response(
                "MIXED_ROUTE_COLLECTION_FAILED",
                issues=prepared["issues"],
                decisions=[],
                analysis_source="MIXED_ACTIVE_PENDING",
            )

        result = plan_mapf_from_route_results(
            graph, prepared["requests"], planner=planner, config=config
        )

        if (fms_before, pending_before, active_before) != self.state_triplet():
            return _blocked_response(
                "FMS_STATE_CHANGED_DURING_MAPF",
                issues={},
                decisions=[],
                analysis_source="MIXED_ACTIVE_PENDING",
            )
        if result.get("fms_execution_approved") is not False:
            raise RuntimeError("KEV-MAPF는 FMS 이동을 직접 승인할 수 없습니다.")

        result.update(
            analysis_source="MIXED_ACTIVE_PENDING",
            active_robot_ids=prepared["active_robot_ids"],
            pending_robot_ids=prepared["pending_robot_ids"],
            mixed_requests_at_analysis=deepcopy(prepared["requests"]),
            mixed_pending_at_analysis=deepcopy(pending_before),
            mixed_active_at_analysis=deepcopy(active_before),
            mixed_fms_at_analysis=deepcopy(fms_before),
        )
        return result

    def classify_mixed_pending_advisory(self, advisory: dict) -> dict:
        """혼합 MAPF 분석 결과에서 Pending 로봇의 출발 후보(`fms_check_candidates`), 우회 대상(`rerouted`), 보류 대상(`deferred`)을 분류한다."""
        def blocked(reason: str, issues: dict | None = None) -> dict:
            return _blocked_response(
                reason, issues=issues or {}, fms_check_candidates=[], deferred={}, rerouted={}
            )

        if (
            not is_fms_reviewable_advisory(advisory)
            or advisory.get("analysis_source") != "MIXED_ACTIVE_PENDING"
            or advisory.get("fms_execution_approved") is not False
        ):
            return blocked("MAPF_ADVISORY_NOT_READY")

        state_before = self.state_triplet()
        if state_before != (
            advisory.get("mixed_fms_at_analysis"),
            advisory.get("mixed_pending_at_analysis"),
            advisory.get("mixed_active_at_analysis"),
        ):
            return blocked("MIXED_ANALYSIS_STALE")

        prepared = self.prepare_mixed_requests()
        if prepared["status"] != "READY":
            return blocked("MIXED_INPUT_NOT_READY", prepared["issues"])
        if prepared["requests"] != advisory.get("mixed_requests_at_analysis"):
            return blocked("MIXED_INPUT_CHANGED")

        pending_ids = set(prepared["pending_robot_ids"])
        active_ids = set(prepared["active_robot_ids"])
        all_ids = pending_ids | active_ids

        if (
            pending_ids & active_ids
            or advisory.get("robot_count") != len(all_ids)
            or set(advisory.get("pending_robot_ids", [])) != pending_ids
            or set(advisory.get("active_robot_ids", [])) != active_ids
        ):
            return blocked("MIXED_ROBOT_SET_MISMATCH")

        decisions = advisory.get("decisions")
        if not isinstance(decisions, list):
            return blocked("MAPF_DECISIONS_INVALID")

        deferred: dict[str, list[str]] = {}
        rerouted: dict[str, list[str]] = {}
        for d in decisions:
            if not isinstance(d, dict):
                return blocked("MAPF_DECISIONS_INVALID")
            action, yield_robot = d.get("action"), d.get("yield_robot")
            if action not in ("WAIT", "HOLD", "REROUTE") or yield_robot not in all_ids:
                return blocked("UNSUPPORTED_MAPF_DECISION")
            if yield_robot in active_ids:
                return blocked("ACTIVE_ROBOT_ACTION_REQUIRES_STOP")

            if action == "REROUTE":
                new_path = d.get("new_path")
                if not isinstance(new_path, list) or not new_path:
                    return blocked("MAPF_REROUTE_PATH_INVALID")
                rerouted[yield_robot] = [str(n) for n in new_path]
            else:
                deferred.setdefault(yield_robot, []).append(action)

        required_paths = advisory.get("required_paths")
        if not isinstance(required_paths, dict) or set(required_paths) != all_ids:
            return blocked("MAPF_REQUIRED_PATHS_INVALID")

        for req in prepared["requests"]:
            rid = req["order"]["robot_id"]
            expected = [str(n) for n in req["result"]["path"]]
            actual = required_paths.get(rid)
            if not isinstance(actual, list) or not actual:
                return blocked("MAPF_REQUIRED_PATHS_INVALID")
            actual_str = [str(n) for n in actual]
            if rid in rerouted:
                if (
                    actual_str != rerouted[rid]
                    or actual_str[0] != str(req["current_node"])
                    or actual_str[-1] != str(req["node"])
                ):
                    return blocked("MAPF_REROUTE_PATH_MISMATCH")
            elif actual_str != expected:
                return blocked("MAPF_PATH_CHANGE_NEEDS_REVIEW")

        if state_before != self.state_triplet():
            return blocked("STATE_CHANGED_DURING_CLASSIFICATION")

        candidates = sorted(pending_ids - set(deferred))
        if advisory.get("status") == "REVIEW_REQUIRED":
            reqs_by_id = {str(r["order"]["robot_id"]): r for r in prepared["requests"]}
            for d in decisions:
                if d.get("action") != "WAIT":
                    continue
                rid = d["yield_robot"]
                if str(d["wait_node"]) != str(reqs_by_id[rid]["current_node"]):
                    return blocked("WAIT_NODE_REQUIRES_MOVEMENT")

            parked_nodes = {str(reqs_by_id[rid]["current_node"]) for rid in deferred}
            for rid in active_ids:
                if {str(n) for n in required_paths[rid]} & parked_nodes:
                    return blocked("ACTIVE_ROUTE_OVERLAPS_WAIT_NODE")

            candidates = [
                rid
                for rid in candidates
                if not ({str(n) for n in required_paths[rid]} & parked_nodes)
            ]

        return {
            "status": "REVIEW_ONLY",
            "active_robot_ids": sorted(active_ids),
            "fms_check_candidates": candidates,
            "deferred": deferred,
            "rerouted": rerouted,
            "fms_execution_approved": False,
        }
