"""단일 시나리오 실행 관리. ROS/Mock 의존성은 해당 모드에서만 로드한다."""

import asyncio
from copy import deepcopy
from functools import wraps
import json
import math
from threading import RLock
from time import monotonic
from uuid import uuid4

from ..config import BASE_DIR, REAL_OCCUPANCY_TOLERANCE_M, REAL_POSE_TIMEOUT_S
from ..schemas.robot import normalize_robot_id
from .command_service import navigate_to_node, stop_robot
from .mode_service import mode_manager
from .pathfinding import DistanceAStar
from .route_graph import load_route_graph, locate_current_node

SCENARIO_PATH = BASE_DIR / "tests/traffic_scenarios.json"


# 설정을 불러온다.
def load_config():
    try:
        config = json.loads(SCENARIO_PATH.read_text(encoding="utf-8"))
        names = set()
        for scenario in config["scenarios"]:
            name = scenario["name"]
            if not isinstance(name, str) or not name or name in names:
                raise ValueError("시나리오 이름이 비어 있거나 중복되었습니다.")
            names.add(name)
            if not isinstance(scenario["description"], str):
                raise ValueError("description은 문자열이어야 합니다.")
            if "order_stream" in scenario:
                from backend.simulation.order_stream_scenario import validate_options

                validate_options(scenario["order_stream"])
                if scenario.get("robots") or scenario.get("traffic_joint_action"):
                    raise ValueError("주문 공급 시험은 초기 배치·교통조정 시험과 별도로 정의하세요.")
                continue
            ids, starts = set(), set()
            for robot in scenario["robots"]:
                robot["id"] = normalize_robot_id(robot["id"])
                robot["start"] = str(robot["start"])
                if robot["goal"] is not None:
                    robot["goal"] = str(robot["goal"])
                if robot["id"] in ids or robot["start"] in starts:
                    raise ValueError(f"{name}: 로봇 또는 시작 노드 중복")
                ids.add(robot["id"])
                starts.add(robot["start"])
            if not ids or not any(r["goal"] is not None for r in scenario["robots"]):
                raise ValueError(f"{name}: 이동 대상이 필요합니다.")
        for mode in ("simulation", "real"):
            timeout = float(config["timeout_seconds"][mode])
            if not math.isfinite(timeout) or timeout <= 0:
                raise ValueError("제한 시간은 유한한 양수여야 합니다.")
        return config
    except (KeyError, TypeError, OSError) as exc:
        raise ValueError(f"시나리오 설정 오류: {exc}") from exc


# 시나리오에 사용할 로봇 상태를 반환한다.
def robot_states(mode):
    if mode == "simulation":
        from .mock_data import mock_fms

        states = mock_fms.robot_snapshots(mode)
        connections = {r["name"]: r["connected"] for r in mock_fms.connections()}
        return {
            r["robot_id"]: dict(
                r,
                connected=connections.get(r["robot_id"], False),
                fresh=True,
                active=r["goal_node"] is not None or r["route"] is not None,
            )
            for r in states
        }
    from .fleet_manager import fleet_manager
    from ..ros2.ros_gateway import ros_gateway

    result = {}
    for robot in fleet_manager.get_all_robots():
        pending = ros_gateway.navigation_active(robot.robot_id)
        with robot._lock:
            received = robot.pose_received_at
            result[robot.robot_id] = dict(
                robot_id=robot.robot_id,
                status=robot.state.value,
                x=robot.x,
                y=robot.y,
                current_node=robot.current_node,
                goal_node=robot.goal_node,
                route=deepcopy(robot.route),
                connected=robot.connected,
                fresh=received is not None and 0 <= monotonic() - received <= REAL_POSE_TIMEOUT_S,
                active=pending or robot.navigation_type is not None,
                order_id=robot.order_id,
            )
    return result


class ScenarioManager:
    # ScenarioManager의 초기 상태를 설정한다.
    def __init__(self):
        self.lock = RLock()
        self._task = None
        self._status = {"state": "idle", "name": None, "run_id": None, "robots": [], "message": ""}

    # 작업이 진행 중인지 확인한다.
    @property
    def active(self):
        return self._status["state"] in ("running", "stopping")

    @property
    def blocks_order_processing(self):
        return self.active and self._status.get("kind") != "order_stream"

    # 현재 상태를 반환한다.
    def status(self):
        with self.lock:
            status = deepcopy(self._status)
        if status.get("kind") == "order_stream":
            from .order_manager import order_manager
            from backend.simulation.order_stream_scenario import refresh_status_counts

            # 공급 중지 후에도 접수 주문은 계속 처리된다. 조회 결과만 최신화하고
            # 공급 작업·실패 원인·기존 내부 상태는 변경하지 않는다.
            refresh_status_counts(status, order_manager.list_orders())
            if status["state"] != "running":
                counts = status["order_stream"]
                status["message"] = (
                    f"{status['message']} · 등록 {counts['generated']} · 대기 {counts['pending']} · "
                    f"처리 중 {counts['in_progress']} · 작업 완료 {counts['completed']} · "
                    f"복귀 중 {counts['returning']} · 실패 {counts['failed']} · 취소 {counts['cancelled']}"
                )
        return status

    # 가용을 확인하고 준비한다.
    def ensure_available(self, robot_id=None, *, allow_order_stream=False):
        with self.lock:
            if allow_order_stream and self._status.get("kind") == "order_stream":
                return
            if self.active and (robot_id is None or normalize_robot_id(robot_id) in self._status["robot_ids"]):
                raise ValueError("시나리오 실행 중입니다. 완료 또는 정지 후 다시 시도하세요.")

    # 명령을 보호한다.
    def protect_command(self, method):
        # 보호된 작업을 실행한다.
        @wraps(method)
        def guarded(owner, robot_id, *args, **kwargs):
            with self.lock:
                self.ensure_available(robot_id)
                return method(owner, robot_id, *args, **kwargs)

        return guarded

    # 관련을 시작한다.
    def start(self, name, *, route_source=None):
        with self.lock:
            self.ensure_available()
            from .order_manager import order_manager

            if (
                any(row["status"] in ("PENDING", "IN_PROGRESS") for row in order_manager.list_orders())
                or order_manager.returning
                or order_manager.recovering
            ):
                raise ValueError("주문 또는 스테이션 복귀 중에는 시나리오를 시작할 수 없습니다.")
            config = load_config()
            scenario = next((s for s in config["scenarios"] if s["name"] == name), None)
            if scenario is None:
                raise ValueError(f"알 수 없는 시나리오: {name}")
            scenario = deepcopy(scenario)
            if route_source is not None:
                from backend.simulation.traffic_joint_action_scenario import route_source_choices

                options = scenario.get("traffic_joint_action", {})
                if route_source not in route_source_choices(options):
                    raise ValueError("이 시나리오에서는 요청한 경로 생성 방식을 선택할 수 없습니다.")
                options["route_source"] = route_source
            mode = mode_manager.mode
            if "order_stream" in scenario:
                if mode != "simulation" or mode_manager.operation_mode != "ai":
                    raise ValueError("자동 주문 공급은 시뮬·AI 모드에서 실행하세요.")
                from .route_model_catalog import require_enabled_model
                from backend.simulation.order_stream_scenario import run_order_stream

                require_enabled_model("kev", scenario["order_stream"]["model"])
                self._status = dict(
                    state="running", kind="order_stream", run_id=uuid4().hex,
                    name=name, description=scenario["description"], mode=mode,
                    operation_mode="ai", robot_ids=list(robot_states(mode)), robots=[],
                    elapsed=0, message="자동 주문 공급 시작 중",
                    order_stream=dict(
                        **deepcopy(scenario["order_stream"]), order_ids=[], generated=0,
                        pending=0, in_progress=0, completed=0, failed=0, cancelled=0, returning=0,
                    ),
                )
                self._task = asyncio.create_task(run_order_stream(self, deepcopy(scenario["order_stream"])))
                return self.status()
            if scenario.get("traffic_joint_action") and mode != "simulation":
                raise ValueError("교통조정 테스트는 시뮬레이션 모드에서만 실행할 수 있습니다.")
            if scenario.get("traffic_joint_action"):
                from backend.simulation.traffic_joint_action_scenario import resolve_route_source, traffic_selector

                resolved_source = resolve_route_source(scenario["traffic_joint_action"], mode_manager.operation_mode)
                from .mock_data import mock_fms

                # 초기 배치의 stop_robot이 남은 예약을 지워 입력 문제를 숨기지 않도록 한다.
                with mock_fms._lock:
                    if mock_fms._traffic._reservations.snapshot() or mock_fms._traffic._protected_resources:
                        raise ValueError("활성 예약/보호 자원이 있어 교통조정 테스트를 보류합니다.")
            graph = load_route_graph()
            expected = json.loads((BASE_DIR / "routes/test.geojson").read_text())
            if graph != expected:
                raise ValueError("서버 그래프가 routes/test.geojson과 다릅니다.")
            planner = DistanceAStar(graph)
            states = robot_states(mode)
            specs = scenario["robots"]
            errors = []
            # 추가 로봇의 양보/주문 주행이 테스트에 섞이지 않도록 기존 전체 Fleet 계약 유지.
            configured = {r["id"] for r in specs}
            extra = {rid for rid, r in states.items() if r["connected"]} - configured
            if extra:
                errors.append(f"JSON에 없는 연결 로봇: {', '.join(sorted(extra))}")
            for spec in specs:
                rid, start, goal = spec["id"], spec["start"], spec["goal"]
                if start not in planner.nodes or (goal is not None and goal not in planner.nodes):
                    errors.append(f"{rid}: 존재하지 않는 시작/목적지 노드")
                    continue
                if goal is not None:
                    try:
                        planner.plan(start, goal)
                    except ValueError:
                        errors.append(f"{rid}: {start} → {goal} 연결 경로 없음")
                row = states.get(rid)
                if row is None or not row["connected"]:
                    errors.append(f"{rid}: 연결되지 않음 (필요 시작점 N{start})")
                    continue
                if not row["fresh"] or any(v is None or not math.isfinite(v) for v in (row["x"], row["y"])):
                    errors.append(f"{rid}: 최신 위치 없음 (필요 시작점 N{start})")
                    continue
                if row["status"] != "IDLE" or row["active"] or row.get("order_id"):
                    errors.append(f"{rid}: 작업 가능 상태가 아님 ({row['status']})")
                if mode == "real":
                    node = locate_current_node(planner.nodes, row["x"], row["y"], REAL_OCCUPANCY_TOLERANCE_M)
                    if node != start:
                        distance = math.dist((row["x"], row["y"]), planner.nodes[start])
                        errors.append(
                            f"{rid}: 시작 위치 불일치 — N{start}까지 {distance:.3f}m "
                            f"(허용 {REAL_OCCUPANCY_TOLERANCE_M:.3f}m)"
                        )
            if errors:
                raise ValueError("실행 거부:\n" + "\n".join(errors))
            self._status = dict(
                state="running",
                run_id=uuid4().hex,
                name=name,
                description=scenario["description"],
                mode=mode,
                operation_mode=mode_manager.operation_mode,
                robot_ids=[r["id"] for r in specs],
                robots=[],
                elapsed=0.0,
                message="시작 위치 준비 및 명령 전송 중",
            )
            if scenario.get("traffic_joint_action"):
                self._status.update(route_source=resolved_source, traffic_selector=traffic_selector(scenario["traffic_joint_action"]))
            self._task = asyncio.create_task(
                self._run(scenario, graph, planner.nodes, float(config["timeout_seconds"][mode]))
            )
            return self.status()

    # 관련을 실행한다.
    async def _run(self, scenario, graph, nodes, timeout):
        mode = self._status["mode"]
        specs = scenario["robots"]
        goals = {r["id"]: r["goal"] for r in specs if r["goal"] is not None}
        started = monotonic()
        touched = False
        try:
            if self._status["state"] == "stopping":
                raise ValueError(self._status["message"])
            if mode == "simulation":
                from backend.simulation.scenarios import prepare

                touched = True
                prepare(specs, nodes)
            if scenario.get("traffic_joint_action"):
                from backend.simulation.traffic_joint_action_scenario import run_joint_action_scenario

                await run_joint_action_scenario(self, scenario, graph, started, timeout)
                return
            required_paths = {}
            operation_mode = self._status.get("operation_mode", mode_manager.operation_mode)
            if operation_mode == "ai":
                from .llm_route_preview import preview_llm_route

                def signature():
                    return {
                        rid: {key: row.get(key) for key in (
                            "status", "occupied_node", "occupied_edge", "current_node",
                            "navigation_id", "route", "goal_node", "connected", "fresh", "active",
                        )}
                        for rid, row in robot_states(mode).items()
                    }

                before_routes = signature()
                results = {}
                # 모든 경로를 생성·검사한 뒤 주행한다. 모델 실패를 A* 경로로 대체하지 않는다.
                for rid, goal in goals.items():
                    self._status["message"] = f"KEV 경로 생성 중: {rid} → N{goal}"
                    start = before_routes[rid].get("occupied_node") or before_routes[rid]["current_node"]
                    result = await asyncio.to_thread(
                        preview_llm_route, graph, start, goal,
                        selector="kev", model=scenario.get("model", "kev-08b"),
                    )
                    if (
                        self._status["state"] != "running" or mode_manager.mode != mode
                        or mode_manager.operation_mode != operation_mode or load_route_graph() != graph
                        or before_routes != signature() or monotonic() - started >= timeout
                    ):
                        raise ValueError("KEV 경로 생성 중 시나리오·상태가 변경되었거나 제한 시간을 초과했습니다.")
                    required_paths[rid] = result["path"]
                    results[rid] = result
                self._status["route_generation"] = dict(source="KEV", model=scenario.get("model", "kev-08b"), robots=results)
            for rid, goal in goals.items():
                if mode_manager.mode != mode:
                    raise ValueError("운용 모드가 변경되었습니다.")
                touched = True
                if rid in required_paths:
                    navigate_to_node(rid, goal, mode, required_path=required_paths[rid])
                else:
                    navigate_to_node(rid, goal, mode)
            self._status["message"] = "주행 및 도착 후 동작 완료 대기 중"
            while True:
                if self._status["state"] == "stopping":
                    raise ValueError(self._status["message"])
                if mode_manager.mode != mode or load_route_graph() != graph:
                    raise ValueError("실행 중 운용 모드 또는 그래프가 변경되었습니다.")
                states = robot_states(mode)
                rows = []
                for spec in specs:
                    rid = spec["id"]
                    row = states.get(rid)
                    if row is None or not row["connected"] or not row["fresh"]:
                        raise ValueError(f"{rid}: 연결 또는 최신 위치 확인 실패")
                    if row["status"] in ("PAUSED", "EMERGENCY_STOP", "OFFLINE"):
                        raise ValueError(f"{rid}: {row['status']}")
                    rows.append(
                        dict(robot_id=rid, status=row["status"], current_node=row["current_node"], goal=spec["goal"])
                    )
                self._status.update(robots=rows, elapsed=round(monotonic() - started, 2))
                done = all(
                    states[rid]["status"] == "IDLE"
                    and not states[rid]["active"]
                    and states[rid]["route"] is None
                    and str(states[rid]["current_node"]) == goal
                    for rid, goal in goals.items()
                )
                # 양보 중인 비이동 대상도 종료되어야 다음 테스트와 충돌하지 않는다.
                done &= all(row["status"] == "IDLE" and not states[row["robot_id"]]["active"] for row in rows)
                if done:
                    self._status.update(state="completed", message="모든 로봇의 주행 및 후속 동작 완료")
                    return
                if monotonic() - started >= timeout:
                    raise ValueError(f"{timeout:g}초 내 미완료 (교착 확정 아님)")
                await asyncio.sleep(0.1)
        except (Exception, asyncio.CancelledError) as exc:
            reason = str(exc) or "서버 종료로 중단"
            self._status.update(state="stopping", message=reason)
            errors = []
            if touched:
                for spec in specs:
                    try:
                        stop_robot(spec["id"], mode)
                    except Exception as stop_error:
                        errors.append(f"{spec['id']} 정지 요청 실패: {stop_error}")
            self._status.update(state="failed", message="\n".join([reason, *errors]))

    # 실행 중인 시나리오를 중단한다.
    def interrupt(self, robot_id=None):
        with self.lock:
            if (self.active or self._status.get("kind") == "order_stream") and (
                robot_id is None or normalize_robot_id(robot_id) in self._status["robot_ids"]
            ):
                if self._status.get("kind") == "order_stream":
                    from .order_manager import order_manager

                    self.stop_order_supply(message="비상정지 요청으로 자동 주문 공급 중지")
                    # 이 시험의 미배정 주문만 취소해 정지 직후 자동 재배차를 방지한다.
                    for oid in self._status["order_stream"]["order_ids"]:
                        row = order_manager.get_order(oid)
                        if row and row["status"] == "PENDING":
                            order_manager.cancel_order(oid, return_to_station=False)
                    return
                self._status.update(state="stopping", message="정지 요청으로 시나리오 중단")

    def stop_order_supply(self, *, message=None):
        with self.lock:
            if self._status.get("kind") != "order_stream":
                raise ValueError("실행 중인 자동 주문 공급 시험이 없습니다.")
            if self.active:
                self._status.update(
                    state="completed",
                    message=message or "주문 공급 중지 · 이미 접수된 주문은 계속 처리됩니다.",
                )
                if self._task and not self._task.done():
                    self._task.cancel()
            return self.status()

    # 관련을 종료한다.
    async def shutdown(self):
        if self._status.get("kind") == "order_stream":
            self.stop_order_supply(message="서버 종료로 자동 주문 공급 중지")
            if self._task:
                await asyncio.gather(self._task, return_exceptions=True)
            return
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                # 실행 coroutine이 시작하기 전에 취소되면 전송한 명령이 없다.
                self._status.update(state="failed", message="실행 시작 전 서버 종료")


scenario_manager = ScenarioManager()
