"""FMS와 KEV-MAPF 사이의 경로 수집 및 조정 모듈."""

from copy import deepcopy
from threading import RLock

from .kev_mapf_standalone import mapf_robot_from_route_result


class FmsMapfCoordinator:

    def __init__(self):
        self._lock = RLock()

        # 로봇 ID별 AI 경로 생성 결과 저장
        self._route_requests = {}
        # 아직 FMS에 전달하지 않은 경로
        self._pending_requests = {}

    def retire_completed_simulation_routes(self) -> dict:
        """주행 완료가 확인된 Active 경로만 정리한다.

        FMS의 실제 로봇 상태나 예약은 변경하지 않는다.
        """
        from .mock_data import mock_fms

        before = self._simulation_signature()
        snapshots = mock_fms.robot_snapshots("simulation")
        after = self._simulation_signature()

        if before != after:
            return {
                "status": "BLOCKED",
                "reason": "FMS_STATE_CHANGED_DURING_CHECK",
                "retired_robot_ids": [],
                "fms_execution_approved": False,
            }

        states = {}

        for state in snapshots:
            rid = str(state["robot_id"])

            if rid in states:
                return {
                    "status": "BLOCKED",
                    "reason": "DUPLICATE_ROBOT_STATE",
                    "retired_robot_ids": [],
                    "fms_execution_approved": False,
                }

            states[rid] = state

        retired = []

        with self._lock:
            for rid, request in list(self._route_requests.items()):
                state = states.get(rid)

                if state is None:
                    continue

                # Pending과 Active가 동시에 등록된 경우 정리하지 않음
                if rid in self._pending_requests:
                    continue

                # FMS가 목적지 도착 및 주행 종료를 확인한 경우만 정리
                completed = (
                    state.get("status") == "IDLE"
                    and state.get("connection_state") == "ONLINE"
                    and state.get("occupied_edge") is None
                    and str(state.get("occupied_node"))
                        == str(request["node"])
                    and state.get("goal_node") is None
                    and state.get("route") is None
                )

                if completed:
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

    def classify_mixed_pending_advisory(self, advisory: dict) -> dict:
        """혼합 MAPF 분석 결과에서 Pending 로봇의 출발 후보만 분류한다.

        실행 승인, 예약 변경, 주행 명령은 수행하지 않는다.
        """

        def blocked(reason, issues=None):
            return {
                "status": "BLOCKED",
                "reason": reason,
                "issues": issues or {},
                "fms_check_candidates": [],
                "deferred": {},
                "fms_execution_approved": False,
            }

        # 1. MAPF 결과 유효성 검사
        if (
            not is_fms_reviewable_advisory(advisory)
            or advisory.get("analysis_source") != "MIXED_ACTIVE_PENDING"
            or advisory.get("fms_execution_approved") is not False
        ):
    
            return blocked("MAPF_ADVISORY_NOT_READY")

        # 2. 분석 당시와 현재 상태 비교
        fms_before = self._simulation_signature()
        pending_before = self.pending_snapshot()
        active_before = self.snapshot()

        if (
            fms_before != advisory.get("mixed_fms_at_analysis")
            or pending_before != advisory.get("mixed_pending_at_analysis")
            or active_before != advisory.get("mixed_active_at_analysis")
        ):
            return blocked("MIXED_ANALYSIS_STALE")

        # 3. 현재 경로를 다시 수집해 분석 입력과 비교
        prepared = self.prepare_mixed_simulation_requests()

        if prepared["status"] != "READY":
            return blocked(
                "MIXED_INPUT_NOT_READY",
                prepared["issues"],
            )

        if prepared["requests"] != advisory.get(
            "mixed_requests_at_analysis"
        ):
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

        # 4. MAPF가 경로 자체를 변경했다면 별도 검증 필요
        required_paths = advisory.get("required_paths")

        if (
            not isinstance(required_paths, dict)
            or set(required_paths) != all_ids
        ):
            return blocked("MAPF_REQUIRED_PATHS_INVALID")

        for request in prepared["requests"]:
            rid = request["order"]["robot_id"]

            expected = [
                str(n) for n in request["result"]["path"]
            ]

            actual = required_paths.get(rid)

            if (
                not isinstance(actual, list)
                or [str(n) for n in actual] != expected
            ):
                return blocked("MAPF_PATH_CHANGE_NEEDS_REVIEW")

        # 5. WAIT / HOLD / REROUTE 분류
        decisions = advisory.get("decisions")

        if not isinstance(decisions, list):
            return blocked("MAPF_DECISIONS_INVALID")

        deferred = {}

        for decision in decisions:
            if not isinstance(decision, dict):
                return blocked("MAPF_DECISIONS_INVALID")

            action = decision.get("action")
            yield_robot = decision.get("yield_robot")

            if (
                action not in ("WAIT", "HOLD", "REROUTE")
                or yield_robot not in all_ids
            ):
                return blocked("UNSUPPORTED_MAPF_DECISION")

            # 이미 주행 중인 로봇은 임의로 WAIT 처리하지 않는다.
            if yield_robot in active_ids:
                return blocked("ACTIVE_ROBOT_ACTION_REQUIRES_STOP")

            deferred.setdefault(yield_robot, []).append(action)

        # 6. 분류 중 상태가 변했으면 결과 폐기
        if (
            fms_before != self._simulation_signature()
            or pending_before != self.pending_snapshot()
            or active_before != self.snapshot()
        ):
            return blocked("STATE_CHANGED_DURING_CLASSIFICATION")
        
        candidates = sorted(pending_ids - set(deferred))

        if advisory.get("status") == "REVIEW_REQUIRED":
            requests_by_id = {
                str(req["order"]["robot_id"]): req
                for req in prepared["requests"]
            }

            # Pending 로봇을 현재 위치에서 대기시키는 경우만 처리
            for decision in decisions:
                rid = decision["yield_robot"]

                if str(decision["wait_node"]) != str(
                    requests_by_id[rid]["current_node"]
                ):
                    return blocked("WAIT_NODE_REQUIRES_MOVEMENT")

            parked_nodes = {
                str(requests_by_id[rid]["current_node"])
                for rid in deferred
            }

            # 이미 움직이는 로봇의 남은 경로가 대기 위치를 통과하면 차단
            for rid in active_ids:
                active_path = {
                    str(n) for n in requests_by_id[rid]["result"]["path"]
                }
                if active_path & parked_nodes:
                    return blocked("ACTIVE_ROUTE_OVERLAPS_WAIT_NODE")

            # 대기 중인 로봇의 현재 위치를 지나가는 후보는 제외
            candidates = [
                rid for rid in candidates
                if not (
                    {str(n) for n in requests_by_id[rid]["result"]["path"]}
                    & parked_nodes
                )
            ]

        return {
            "status": "REVIEW_ONLY",
            "active_robot_ids": sorted(active_ids),
            "fms_check_candidates": candidates,
            "deferred": deferred,
            "fms_execution_approved": False,
        }

    def evaluate_mixed_simulation_advisory(
        self,
        graph: dict,
        *,
        planner=None,
        config: dict | None = None,
    ) -> dict:
        """Active + Pending 경로를 통합하여 KEV-MAPF 분석한다.

        FMS 이동 명령 또는 예약을 변경하지 않는다.
        """

        from .kev_mapf_standalone import plan_mapf_from_route_results

        def blocked(reason, issues=None):
            return {
                "status": "BLOCKED",
                "reason": reason,
                "issues": issues or {},
                "decisions": [],
                "analysis_source": "MIXED_ACTIVE_PENDING",
                "fms_execution_approved": False,
            }

        # 1. 분석 전 실제 FMS 및 Coordinator 상태 기록
        fms_before = self._simulation_signature()
        pending_before = self.pending_snapshot()
        active_before = self.snapshot()

        # 2. Active의 남은 경로와 Pending 경로 통합
        prepared = self.prepare_mixed_simulation_requests()

        # 입력 수집 도중 상태가 변경되었는지 확인
        if (
            fms_before != self._simulation_signature()
            or pending_before != self.pending_snapshot()
            or active_before != self.snapshot()
        ):
            return blocked("STATE_CHANGED_DURING_COLLECTION")

        if prepared["status"] != "READY":
            return blocked(
                "MIXED_ROUTE_COLLECTION_FAILED",
                prepared["issues"],
            )

        # 3. KEV-MAPF 통합 분석
        result = plan_mapf_from_route_results(
            graph,
            prepared["requests"],
            planner=planner,
            config=config,
        )

        # 4. 분석 중 FMS 또는 경로 저장소 변경 시 결과 폐기
        if (
            fms_before != self._simulation_signature()
            or pending_before != self.pending_snapshot()
            or active_before != self.snapshot()
        ):
            return blocked("FMS_STATE_CHANGED_DURING_MAPF")

        # 5. 실행 권한은 FMS에만 있음
        if result.get("fms_execution_approved") is not False:
            raise RuntimeError(
                "KEV-MAPF는 FMS 이동을 직접 승인할 수 없습니다."
            )

        result["analysis_source"] = "MIXED_ACTIVE_PENDING"
        result["active_robot_ids"] = prepared["active_robot_ids"]
        result["pending_robot_ids"] = prepared["pending_robot_ids"]
        result["mixed_requests_at_analysis"] = deepcopy(prepared["requests"])
        result["mixed_pending_at_analysis"] = deepcopy(pending_before)
        result["mixed_active_at_analysis"] = deepcopy(active_before)
        result["mixed_fms_at_analysis"] = deepcopy(fms_before)

        return result

    def prepare_mixed_simulation_requests(self) -> dict:
        """Active의 실제 남은 경로와 Pending 경로를 통합한다.

        조회 및 검증 전용이다. 이동 명령이나 예약은 변경하지 않는다.
        """

        pending = self.pending_snapshot()
        active = self.snapshot()

        before = self._simulation_signature()
        live = self.inspect_simulation_state()

        requests = []
        issues = {}
        seen = set()

        pending_ids = [
            str(item["order"]["robot_id"]) for item in pending
        ]
        active_ids = [
            str(item["order"]["robot_id"]) for item in active
        ]

        for source, items in (
            ("pending", pending),
            ("active", active),
        ):
            for item in items:
                rid = str(item["order"]["robot_id"])

                if rid in seen:
                    issues[rid] = "DUPLICATE_OR_OVERLAPPING_REQUEST"
                    continue

                seen.add(rid)

                state = live.get(rid)
                signature = before.get(rid)

                if state is None or signature is None:
                    issues[rid] = "ROBOT_NOT_FOUND"
                    continue

                if signature["connection_state"] != "ONLINE":
                    issues[rid] = "ROBOT_OFFLINE"
                    continue

                # Active 주행 중 정지 요청은 차단한다.
                # Pending은 아래 IDLE/route/goal 검사로 정지 완료를 검증한다.
                if source == "active" and signature["stop_requested"]:
                    issues[rid] = "STOP_REQUESTED"
                    continue

                if source == "pending":
                    # Pending 로봇은 아직 출발하지 않은 상태여야 한다.
                    if (
                        state["status"] != "IDLE"
                        or state["goal_node"] is not None
                        or state["occupied_edge"] is not None
                        or state["issue"] != "NO_ACTIVE_ROUTE"
                    ):
                        issues[rid] = "PENDING_ROBOT_NOT_IDLE"
                        continue

                    if str(state["occupied_node"]) != str(
                        item["current_node"]
                    ):
                        issues[rid] = "PENDING_START_CHANGED"
                        continue

                    requests.append(deepcopy(item))
                    continue

                # Active 로봇은 이미 FMS가 실행 중인 경로를 사용한다.
                if state["status"] not in ("NAVIGATING", "WAITING"):
                    issues[rid] = "ACTIVE_ROBOT_NOT_RUNNING"
                    continue

                if state["issue"] is not None:
                    issues[rid] = state["issue"]
                    continue

                if str(state["goal_node"]) != str(item["node"]):
                    issues[rid] = "ACTIVE_GOAL_CHANGED"
                    continue

                remaining = state["remaining_path"]
                original = [
                    str(n) for n in item["result"]["path"]
                ]

                # 기존 등록 경로의 유효한 뒷부분인지 검사한다.
                if not any(
                    original[i:] == remaining
                    for i in range(len(original))
                ):
                    issues[rid] = "ACTIVE_ROUTE_CHANGED"
                    continue

                updated = deepcopy(item)
                updated["current_node"] = str(
                    state["occupied_node"]
                )
                updated["result"] = {
                    "path": list(remaining)
                }

                requests.append(updated)

        # 분석에서 누락된 주행 로봇이나 경로 위의 정지 로봇 확인
        planned_nodes = {
            str(node)
            for request in requests
            for node in request["result"]["path"]
        }

        for rid, state in live.items():
            if rid in seen:
                continue

            if (
                state["goal_node"] is not None
                or state["occupied_edge"] is not None
                or state["status"] in ("NAVIGATING", "WAITING")
            ):
                issues[rid] = "UNMODELED_ACTIVE_ROBOT"
            elif (
                state["occupied_node"] is not None
                and str(state["occupied_node"]) in planned_nodes
            ):
                issues[rid] = "UNMODELED_IDLE_OCCUPANCY"

        # 분석 입력을 만드는 동안 상태가 바뀌었다면 폐기
        if (
            before != self._simulation_signature()
            or pending != self.pending_snapshot()
            or active != self.snapshot()
        ):
            issues["_global"] = "STATE_CHANGED_DURING_COLLECTION"

        return {
            "status": (
                "READY"
                if len(requests) >= 2 and not issues
                else "BLOCKED"
            ),
            "requests": requests,
            "issues": issues,
            "active_robot_ids": sorted(active_ids),
            "pending_robot_ids": sorted(pending_ids),
            "fms_execution_approved": False,
        }

    def inspect_pending_fms_candidates(self, advisory: dict) -> dict:
        """MAPF 실행 후보에 대해 실제 FMS 상태를 검사한다.

        조회 전용이다. 예약 및 주행 명령은 실행하지 않는다.
        """
        from .mock_data import mock_fms

        def blocked(reason, issues=None):
            return {
                "status": "BLOCKED",
                "reason": reason,
                "issues": issues or {},
                "fms_check_candidates": [],
                "fms_execution_approved": False,
            }

        # MAPF 판단과 Pending 경로 유효성 재검사
        pending_before = self.pending_snapshot()
        policy = self.classify_pending_advisory(advisory)

        if (
            policy["status"] != "REVIEW_ONLY"
            or pending_before != self.pending_snapshot()
        ):
            return blocked("MAPF_POLICY_OR_PENDING_CHANGED")

        # FMS의 실제 현재 상태 조회
        states_before = deepcopy(
            mock_fms.robot_snapshots("simulation")
        )

        states = {}
        issues = {}

        for state in states_before:
            rid = str(state["robot_id"])

            if rid in states:
                issues[rid] = "DUPLICATE_ROBOT_STATE"

            states[rid] = state

        pending_ids = {
            str(item["order"]["robot_id"])
            for item in pending_before
        }

        # Pending에 포함되지 않은 로봇이 주행 중인지 검사
        for rid, state in states.items():
            if rid in pending_ids:
                continue

            if (
                state.get("goal_node") is not None
                or state.get("route") is not None
                or state.get("occupied_edge") is not None
                or state.get("status") in ("NAVIGATING", "WAITING")
            ):
                issues[rid] = "UNMODELED_ACTIVE_ROBOT"

        # Pending에 등록된 모든 로봇의 현재 상태 검사
        for request in pending_before:
            rid = str(request["order"]["robot_id"])
            start_node = str(request["current_node"])

            state = states.get(rid)

            if state is None:
                issues[rid] = "ROBOT_NOT_FOUND"
                continue

            if state.get("connection_state") != "ONLINE":
                issues[rid] = "ROBOT_OFFLINE"
            elif state.get("stop_requested"):
                issues[rid] = "STOP_REQUESTED"
            elif (
                state.get("status") != "IDLE"
                or state.get("goal_node") is not None
                or state.get("route") is not None
                or state.get("occupied_edge") is not None
            ):
                issues[rid] = "ROBOT_NOT_IDLE"
            elif str(state.get("occupied_node")) != start_node:
                issues[rid] = "START_POSITION_CHANGED"

        # 검사 도중 FMS 또는 Pending이 변경되면 결과 폐기
        states_after = deepcopy(
            mock_fms.robot_snapshots("simulation")
        )

        # updated_at은 조회할 때마다 변경되는 메타데이터이므로
        # 실제 상태 변경 비교에서 제외한다.
        def stable_states(snapshots):
            return sorted(
                [
                    {
                        key: value
                        for key, value in state.items()
                        if key != "updated_at"
                    }
                    for state in snapshots
                ],
                key=lambda state: str(state["robot_id"]),
            )

        if (
            stable_states(states_before) != stable_states(states_after)
            or pending_before != self.pending_snapshot()
        ):
            return blocked("STATE_CHANGED_DURING_PREFLIGHT")

        if issues:
            return blocked("FMS_PREFLIGHT_FAILED", issues)

        return {
            "status": "REVIEW_ONLY",
            "fms_check_candidates": policy["fms_check_candidates"],
            "deferred": policy["deferred"],
            "issues": {},
            "fms_execution_approved": False,
        }

    def evaluate_pending_advisory(
        self,
        graph: dict,
        *,
        planner=None,
        config: dict | None = None,
    ) -> dict:
        """FMS 주행 요청 전, 대기 중인 3대의 경로를 MAPF로 분석한다."""

        from .kev_mapf_standalone import plan_mapf_from_route_results

        # 분석 시작 전 Pending 경로 복사
        before = self.pending_snapshot()

        # 현재 단계에서는 AMR 3대가 모두 등록됐을 때만 분석
        if len(before) != 3:
            return {
                "status": "NOT_READY",
                "reason": "WAITING_FOR_THREE_ROUTES",
                "pending_robot_count": len(before),
                "decisions": [],
                "fms_execution_approved": False,
            }

        # 아직 실행하지 않은 경로로 충돌 분석
        result = plan_mapf_from_route_results(
            graph,
            before,
            planner=planner,
            config=config,
        )

        # KEV 분석 중 Pending 경로가 변경되면 결과 폐기
        after = self.pending_snapshot()

        if before != after:
            return {
                "status": "BLOCKED",
                "reason": "PENDING_ROUTES_CHANGED_DURING_MAPF",
                "decisions": [],
                "fms_execution_approved": False,
            }

        if result["fms_execution_approved"] is not False:
            raise RuntimeError(
                "MAPF가 직접 FMS 이동을 승인할 수 없습니다."
            )

        result["analysis_source"] = "PENDING_ROUTES"

        # 분석 당시의 경로를 저장해 이후 변경 여부를 비교
        result["pending_requests_at_analysis"] = before

        return result

    def _simulation_signature(self) -> dict:
        """FMS 실행 상태의 변경 여부를 확인하기 위한 스냅샷."""

        from .mock_data import mock_fms

        snapshots = mock_fms.robot_snapshots("simulation")
        signature = {}

        for robot in snapshots:
            rid = str(robot["robot_id"])
            route = robot.get("route")

            signature[rid] = {
                "status": robot.get("status"),
                "connection_state": robot.get("connection_state"),
                "stop_requested": robot.get("stop_requested"),
                "occupied_node": robot.get("occupied_node"),
                "occupied_edge": robot.get("occupied_edge"),
                "goal_node": robot.get("goal_node"),
                "navigation_id": robot.get("navigation_id"),
                "route_nodes": (
                    deepcopy(route.get("node_ids"))
                    if isinstance(route, dict) else None
                ),
                "segment_index": (
                    route.get("segment_index")
                    if isinstance(route, dict) else None
                ),
                "route_phase": (
                    route.get("phase")
                    if isinstance(route, dict) else None
                ),
                "route_departure_at": (
                    route.get("departure_at")
                    if isinstance(route, dict) else None
                ),
                "segment_departures": (
                    deepcopy(route.get("segment_departures"))
                    if isinstance(route, dict) else None
                ),
            }

        return signature

    def evaluate_synced_simulation_advisory(
        self,
        graph: dict,
        *,
        planner=None,
        config: dict | None = None,
    ) -> dict:
        """FMS의 최신 경로를 이용한 MAPF 분석. 상태 변경 시 결과 폐기."""

        from .kev_mapf_standalone import plan_mapf_from_route_results

        # 1. 분석 시작 전 FMS 상태 기록
        before = self._simulation_signature()

        # 2. 현재 위치와 남은 경로 동기화
        prepared = self.prepare_simulation_requests()

        # 동기화 과정에서 상태가 바뀌었는지 확인
        if before != self._simulation_signature():
            return {
                "status": "BLOCKED",
                "reason": "FMS_STATE_CHANGED_DURING_SYNC",
                "decisions": [],
                "fms_route_synced": False,
                "fms_execution_approved": False,
            }

        if prepared["status"] != "READY":
            return {
                "status": "BLOCKED",
                "reason": "FMS_ROUTE_SYNC_FAILED",
                "issues": prepared["issues"],
                "decisions": [],
                "fms_route_synced": False,
                "fms_execution_approved": False,
            }

        # 3. KEV-MAPF 분석
        result = plan_mapf_from_route_results(
            graph,
            prepared["requests"],
            planner=planner,
            config=config,
        )

        # 4. 분석 도중 로봇 상태가 변경됐는지 확인
        after = self._simulation_signature()

        if before != after:
            return {
                "status": "BLOCKED",
                "reason": "FMS_STATE_CHANGED_DURING_MAPF",
                "decisions": [],
                "fms_route_synced": False,
                "fms_execution_approved": False,
            }

        # 5. MAPF 결과는 여전히 판단 전용
        if result["fms_execution_approved"] is not False:
            raise RuntimeError("MAPF가 직접 이동을 승인할 수 없습니다.")

        result["fms_route_synced"] = True

        return result
    
    def prepare_simulation_requests(self) -> dict:
        """FMS의 현재 위치와 남은 경로를 검증해 MAPF 입력을 준비한다."""

        registered = self.snapshot()
        live_states = self.inspect_simulation_state()

        requests = []
        issues = {}

        registered_ids = {
            item["order"]["robot_id"] for item in registered
        }

        # 미등록 로봇이 주행 중이면 분석 보류
        for rid, state in live_states.items():
            if rid not in registered_ids:
                if (
                    state["goal_node"] is not None
                    or state["occupied_edge"] is not None
                ):
                    issues[rid] = "UNREGISTERED_ACTIVE_ROBOT"

        for item in registered:
            rid = item["order"]["robot_id"]
            state = live_states.get(rid)

            if state is None:
                issues[rid] = "ROBOT_NOT_FOUND"
                continue

            if state["issue"] is not None:
                issues[rid] = state["issue"]
                continue

            if state["status"] not in ("NAVIGATING", "WAITING"):
                issues[rid] = "ROBOT_NOT_ACTIVE"
                continue

            remaining = state["remaining_path"]
            original = [str(n) for n in item["result"]["path"]]

            if str(state["goal_node"]) != str(item["node"]):
                issues[rid] = "GOAL_CHANGED"
                continue

            # 현재 경로가 최초 경로의 연속된 뒷부분인지 검사
            valid_suffix = any(
                original[i:] == remaining
                for i in range(len(original))
            )

            if not valid_suffix:
                issues[rid] = "ROUTE_CHANGED"
                continue

            updated = deepcopy(item)
            updated["current_node"] = str(state["occupied_node"])
            updated["result"] = {"path": list(remaining)}

            requests.append(updated)

        return {
            "status": (
                "READY" if len(requests) >= 2 and not issues
                else "BLOCKED"
            ),
            "requests": requests,
            "issues": issues,
        }
    def inspect_simulation_state(self) -> dict:
        """FMS 시뮬레이션의 실제 점유와 남은 경로를 읽는다.

        상태 조회 전용이다. 경로 예약이나 이동 명령은 실행하지 않는다.
        """
        from .mock_data import mock_fms

        snapshots = mock_fms.robot_snapshots("simulation")
        states = {}

        for robot in snapshots:
            robot_id = str(robot["robot_id"])
            occupied_node = robot.get("occupied_node")
            occupied_edge = robot.get("occupied_edge")
            route = robot.get("route")

            remaining_path = None
            issue = None

            if occupied_edge is not None:
                issue = "MID_EDGE"

            elif not isinstance(route, dict):
                issue = "NO_ACTIVE_ROUTE"

            else:
                nodes = route.get("node_ids")
                index = route.get("segment_index")

                if (
                    not isinstance(nodes, list)
                    or not isinstance(index, int)
                    or isinstance(index, bool)
                    or not 0 <= index < len(nodes)
                ):
                    issue = "INVALID_ROUTE"

                elif (
                    occupied_node is None
                    or str(nodes[index]) != str(occupied_node)
                ):
                    issue = "POSITION_ROUTE_MISMATCH"

                else:
                    remaining_path = [
                        str(n) for n in nodes[index:]
                    ]

            states[robot_id] = {
                "robot_id": robot_id,
                "status": robot.get("status"),
                "occupied_node": occupied_node,
                "occupied_edge": occupied_edge,
                "goal_node": robot.get("goal_node"),
                "remaining_path": remaining_path,
                "issue": issue,
            }
        return states

    def register_preview(
        self,
        order: dict,
        current_node: int | str,
        goal_node: int | str,
        result: dict,
    ) -> dict:
        """dispatcher의 result['path']를 MAPF 입력으로 등록한다."""

        robot = mapf_robot_from_route_result(
            robot_id=order["robot_id"],
            current_node=current_node,
            goal_node=goal_node,
            result=result,
        )

        robot_id = robot["id"]

        request = {
            "order": {
                "robot_id": robot_id,
                "selector": order["selector"],
                "model": order["model"],
            },
            "current_node": robot["current_node"],
            "node": robot["goal_node"],
            "result": {
                "path": [str(n) for n in robot["path"]],
            },
        }

        with self._lock:
            self._route_requests[robot_id] = deepcopy(request)

        return deepcopy(request)

    def snapshot(self) -> list[dict]:
        """현재 등록된 로봇 경로를 복사해서 반환한다."""

        with self._lock:
            return deepcopy(list(self._route_requests.values()))

    def evaluate_advisory(
        self,
        graph: dict,
        *,
        planner=None,
        config: dict | None = None,
    ) -> dict:
        """등록된 AMR 경로를 KEV-MAPF로 분석한다."""

        from .kev_mapf_standalone import plan_mapf_from_route_results

        requests = self.snapshot()

        if len(requests) < 2:
            raise ValueError(
                "MAPF 분석에는 최소 2대의 로봇 경로가 필요합니다."
            )

        result = plan_mapf_from_route_results(
            graph,
            requests,
            planner=planner,
            config=config,
        )

        if result["fms_execution_approved"] is not False:
            raise RuntimeError("MAPF 실행 승인 상태가 올바르지 않습니다.")

        return result
    
    def remove(self, robot_id: str) -> None:
        """작업이 종료되거나 취소된 로봇의 경로를 제거한다."""

        with self._lock:
            self._route_requests.pop(str(robot_id), None)
    def stage_preview(
        self,
        order: dict,
        current_node: int | str,
        goal_node: int | str,
        result: dict,
    ) -> dict:
        """실제 주행 요청 전에 AI 경로를 임시 등록한다."""

        robot = mapf_robot_from_route_result(
            robot_id=order["robot_id"],
            current_node=current_node,
            goal_node=goal_node,
            result=result,
        )

        robot_id = robot["id"]

        request = {
            "order": {
                "robot_id": robot_id,
                "selector": order["selector"],
                "model": order["model"],
            },
            "current_node": robot["current_node"],
            "node": robot["goal_node"],
            "result": {
                "path": [str(n) for n in robot["path"]],
            },
        }

        with self._lock:
            self._pending_requests[robot_id] = deepcopy(request)

        return deepcopy(request)

    def pending_snapshot(self) -> list[dict]:
        """아직 실행되지 않은 경로를 복사하여 반환한다."""

        with self._lock:
            return deepcopy(list(self._pending_requests.values()))

    def discard_pending(self, robot_id: str) -> None:
        """취소되거나 무효화된 임시 경로를 삭제한다."""

        with self._lock:
            self._pending_requests.pop(str(robot_id), None)
    def classify_pending_advisory(self, advisory: dict) -> dict:
        """MAPF 판단을 FMS 검증 후보와 실행 보류 대상으로 분류한다."""

        pending = self.pending_snapshot()

        robot_ids = {
            item["order"]["robot_id"]
            for item in pending
        }

        def blocked(reason):
            return {
                "status": "BLOCKED",
                "reason": reason,
                "fms_check_candidates": [],
                "deferred": {},
                "fms_execution_approved": False,
            }

        # 1. 분석 당시 Pending 경로와 현재 경로를 먼저 비교
        if pending != advisory.get("pending_requests_at_analysis"):
            return blocked("PENDING_ROUTES_CHANGED")

        # 2. MAPF 분석 결과 유효성 검사
        if (
            advisory.get("status") != "PROPOSED"
            or advisory.get("analysis_source") != "PENDING_ROUTES"
            or advisory.get("fms_execution_approved") is not False
            or advisory.get("robot_count") != len(robot_ids)
        ):
            return blocked("MAPF_ADVISORY_NOT_READY")

        # 3. 로봇별 WAIT / HOLD / REROUTE 분류
        deferred = {}

        for decision in advisory.get("decisions", []):
            action = decision.get("action")
            yield_robot = decision.get("yield_robot")

            if (
                action not in ("WAIT", "HOLD", "REROUTE")
                or yield_robot not in robot_ids
            ):
                return blocked("UNSUPPORTED_MAPF_DECISION")

            deferred.setdefault(yield_robot, []).append(action)

        candidates = sorted(robot_ids - set(deferred))

        return {
            "status": "REVIEW_ONLY",
            "fms_check_candidates": candidates,
            "deferred": deferred,
            "fms_execution_approved": False,
        }
def is_fms_reviewable_advisory(advisory: dict) -> bool:
    status = advisory.get("status")

    # 기존 Choice 방식 동작 유지
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

    # 물리적으로 인증된 WAIT만 허용
    if any(
        not isinstance(d, dict)
        or d.get("action") != "WAIT"
        or d.get("wait_node_verified") is not True
        or d.get("wait_node") is None
        for d in decisions
    ):
        return False

    # 현재 코드에서 확인된 경로 중첩 경고만 허용
    return all(
        isinstance(w, str)
        and ": WAIT node " in w
        and w.endswith(
            "; FMS traffic reservation verification required"
        )
        for w in warnings
    )
fms_mapf_coordinator = FmsMapfCoordinator()
