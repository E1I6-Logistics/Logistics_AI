"""고정 jhlee 주문 게이트를 공통 A*·기존 가상 주행기에 연결하는 시험 어댑터.

운영 서비스에는 설치하지 않는다. 별도 시뮬 프로세스에서만 명시적으로 설치한다.
원본 판단 엔진·지시문·상태 검사·Active/Pending·실행 게이트는 변경하지 않는다.
"""

import asyncio
from contextvars import ContextVar
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import RLock
from time import monotonic
from types import ModuleType, SimpleNamespace
from uuid import uuid4

from backend.app.config import BASE_DIR
from backend.app.services import order_traffic_service as records
from backend.app.services.traffic_route_provider import generate_traffic_route
from .traffic_strategy_jhlee import JHLEE_REVISION

POLICY = "JHLEE_NATIVE_GATE_COMMON_ASTAR"
MODULES = ("kev_graph", "kev_conflict", "kev_traffic", "kev_client", "kev_engine",
           "kev_adapter", "kev_coordinator", "kev_dispatcher")
_context = ContextVar("jhlee_order_request", default=None)
_log_lock = RLock()


def audit(event):
    folder = Path(os.environ["FMS_PREVIEW_LOG_DIR"]) / "jhlee"
    folder.mkdir(parents=True, exist_ok=True)
    with _log_lock, (folder / "events.jsonl").open("a", encoding="utf-8") as output:
        output.write(json.dumps(dict(at_monotonic=monotonic(), **deepcopy(event)), ensure_ascii=False) + "\n")


def load_native(*, output=None):
    """검토한 Git 객체의 8파일을 별도 패키지로 읽는다. checkout·병합하지 않는다."""
    namespace = f"backend.app.services._jhlee_orders_{JHLEE_REVISION}"
    package = ModuleType(namespace)
    package.__path__ = []
    sys.modules[namespace] = package
    manifest = {}
    for name in MODULES:
        path = f"backend/app/services/kev_mapf/{name}.py"
        raw = subprocess.run(["git", "show", f"{JHLEE_REVISION}:{path}"], cwd=BASE_DIR,
                             check=True, capture_output=True, timeout=10).stdout
        manifest[path] = hashlib.sha256(raw).hexdigest()
        if output:
            destination = Path(output) / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(raw)
        module = ModuleType(f"{namespace}.{name}")
        # 원본 LiveKEV는 __file__의 상위 경로에서 공통 저장소를 찾는다.
        # 패키지 위치는 기존 저장소를 가리키고 실행 원문·추적 이름은 고정 Git 객체다.
        module.__package__, module.__file__ = namespace, str(Path(BASE_DIR) / path)
        sys.modules[module.__name__] = module
        exec(compile(raw, f"{BASE_DIR}@{JHLEE_REVISION}:{path}", "exec"), module.__dict__)
        setattr(package, name, module)
    return SimpleNamespace(package=package, revision=JHLEE_REVISION, source_sha256=manifest)


def common_astar_preview(graph, start, goal, **kwargs):
    """초기 경로와 원본 엔진이 잘라낸 우회용 그래프에 같은 A*를 적용한다.

    원본의 점유 회피 의도를 유지해 목적지 외 타 로봇 점유 노드를 피할 수 있으면
    피한다. 우회 시 원본 virtual_graph의 제외 노드·엣지는 계속 유지한다.
    """
    started = monotonic()
    event = dict(kind="COMMON_ASTAR", start=str(start), goal=str(goal),
                 traffic_supplied=kwargs.get("traffic") is not None,
                 graph_sha256=hashlib.sha256(json.dumps(graph, sort_keys=True).encode()).hexdigest())
    try:
        blocked = sorted({str(n) for n in (kwargs.get("traffic") or {}).get("occupied_nodes", {})} - {str(start), str(goal)})
        event.update(occupied_nodes_to_avoid=blocked, traffic_fallback=False,
                     route_adapter_profile="OCCUPANCY_AVOID_IF_REACHABLE")
        try:
            result = generate_traffic_route(graph, start, goal, source="EXISTING_SHORTEST_PATH", blocked_nodes=blocked)
        except ValueError:
            if not blocked:
                raise
            # 원본 iterative_route_policy도 점유 회피로 도달 불가하면 기존 후보로 돌아간다.
            # 물리 진입 허가는 여기서 하지 않고 원본 게이트·공통 FMS가 계속 검사한다.
            event["traffic_fallback"] = True
            result = generate_traffic_route(graph, start, goal, source="EXISTING_SHORTEST_PATH")
        event.update(path=result["path"], status="GENERATED")
        return result
    except Exception as exc:
        event.update(status="REJECTED", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        audit(dict(**event, elapsed_seconds=monotonic() - started, context=_context.get()))


def record_selector(selector):
    """choice·score 지시문을 바꾸지 않고 실제 전송 본문·응답을 기록한다."""
    transport = selector._transport

    def post(url, payload, headers, timeout):
        context = _context.get()
        if context is None:
            raise RuntimeError("주문 문맥 없이 모델을 호출하지 않습니다.")
        with _log_lock:
            context["calls"] += 1
            call = context["calls"]
        identity = dict(request_id=uuid4().hex, order_id=context["order_id"], robot_id=context["robot_id"],
                        phase=context["phase"], goal_node=context["goal_node"], call=call,
                        role=POLICY, fms_execution_approved=False)
        records._save_selection_record(dict(**identity, event="REQUEST", at_monotonic=monotonic(), request=deepcopy(payload)))
        started = monotonic()
        try:
            response = transport(url, payload, headers, timeout)
        except Exception as exc:
            records._save_selection_record(dict(**identity, event="ERROR", at_monotonic=monotonic(),
                elapsed_seconds=monotonic()-started, error=f"{type(exc).__name__}: {exc}"))
            raise
        elapsed = monotonic()-started
        records._save_selection_record(dict(**identity, event="RESPONSE", at_monotonic=monotonic(),
            elapsed_seconds=elapsed, inference=dict(request=deepcopy(payload), response=deepcopy(response),
            runtime=deepcopy(selector._runtime)), exact_transport_request_saved=True))
        return response

    selector._transport = post
    return selector


def install(*, preview_only=False):
    """호출 프로세스의 시뮬 주문 실행기만 교체한다. 실기체 요청은 거부한다."""
    if not preview_only or not os.getenv("FMS_PREVIEW_RUN_ID") or not os.getenv("FMS_PREVIEW_LOG_DIR"):
        raise ValueError("식별자·기록 경로를 가진 별도 시뮬 전용 서버에서만 설치하세요.")
    from backend.app.services import llm_route_preview, simulation_order_runner
    from backend.app.services.mode_service import mode_manager
    from backend.app.services.route_selector.kev_selector import KevSelector

    source = load_native(output=Path(os.environ["FMS_PREVIEW_LOG_DIR"]) / "source_jhlee")
    package = source.package
    native = package.kev_dispatcher
    original_live = package.kev_client.LiveKEV

    class RecordedLive(original_live):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.selector = record_selector(KevSelector(model=self.model, timeout_seconds=2))

    package.kev_adapter.LiveKEV = RecordedLive
    # 이 프로세스의 모든 경로 생성은 공통 A*다. 원본 함수 본문은 수정하지 않는다.
    llm_route_preview.preview_llm_route = common_astar_preview
    coordinator = package.kev_coordinator.fms_mapf_coordinator
    prepare = coordinator.prepare_mixed_simulation_requests
    evaluate = coordinator.evaluate_mixed_simulation_advisory
    last_issue = [None, 0.0]

    def observed_prepare():
        result = prepare()
        key = json.dumps(result.get("issues", {}), sort_keys=True)
        now = monotonic()
        if result.get("issues") and (key != last_issue[0] or now-last_issue[1] >= 1):
            audit(dict(kind="COLLECTION_DEFERRED", issues=result["issues"], context=_context.get()))
            last_issue[:] = [key, now]
        return result

    def observed_evaluate(*args, **kwargs):
        result = evaluate(*args, **kwargs)
        audit(dict(kind="NATIVE_ADVISORY", advisory=result, context=_context.get()))
        return result

    coordinator.prepare_mixed_simulation_requests = observed_prepare
    coordinator.evaluate_mixed_simulation_advisory = observed_evaluate
    original_dispatch = simulation_order_runner.dispatch_order_route

    async def dispatch(order, goal, start, valid=None):
        if order["mode"] != "simulation" or mode_manager.mode != "simulation":
            raise ValueError("jhlee 시험 어댑터는 실기체를 지원하지 않습니다.")
        if order.get("operation_mode") == "algorithm":
            # 실패 복귀는 기존 코드 경로를 보존한다. 이를 KEV 성공으로 세지 않는다.
            audit(dict(kind="ALGORITHM_RECOVERY", order_id=order["order_id"], robot_id=order["robot_id"]))
            return await original_dispatch(order, goal, start, valid=valid)
        context = dict(order_id=order["order_id"], robot_id=order["robot_id"], phase=order["phase"],
                       goal_node=str(goal), calls=0)
        token = _context.set(context)
        started = monotonic()
        try:
            # 원본 게이트의 0.5초 진입 대기·동기화·보류·실행 검사를 유지한다.
            response = await native.dispatch_simulation_mapf(order, goal, start, valid=valid)
            registered = next(r for r in coordinator.snapshot() if r["order"]["robot_id"] == order["robot_id"])
            event = dict(order_id=order["order_id"], robot_id=order["robot_id"], phase=order["phase"],
                state="APPLIED", goal_node=str(goal), path=registered["result"]["path"],
                selected_by=POLICY, selection_policy=POLICY, kev_calls=context["calls"], candidates={},
                route_source="EXISTING_SHORTEST_PATH", coordination_seconds=monotonic()-started,
                native_revision=source.revision, note="원본 주문 게이트 통과 후 공통 FMS 주행 요청. 별도 순차 계획으로 변환하지 않음.")
            order["traffic_coordination"] = event
            records._save(event)
            return response
        except BaseException as exc:
            audit(dict(kind="DISPATCH_STOPPED", context=context, error=f"{type(exc).__name__}: {exc}"))
            raise
        finally:
            _context.reset(token)

    simulation_order_runner.dispatch_order_route = dispatch
    provenance = dict(policy=POLICY, revision=source.revision, source_sha256=source.source_sha256,
        route_source="COMMON_ASTAR", original_gate_preserved=True, original_engine_and_prompts_preserved=True,
        route_adapter_profile="OCCUPANCY_AVOID_IF_REACHABLE",
        max_wait_seconds=float(os.getenv("FMS_SIM_MAPF_MAX_WAIT", "60")), priority_mode=os.getenv("FMS_MAPF_PRIORITY_MODE", "score"),
        max_reroutes=int(os.getenv("FMS_MAPF_MAX_REROUTES", "2")))
    (Path(os.environ["FMS_PREVIEW_LOG_DIR"]) / "jhlee-provenance.json").write_text(json.dumps(provenance, ensure_ascii=False, indent=2)+"\n")
    return provenance
