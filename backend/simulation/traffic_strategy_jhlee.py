"""고정된 jhlee 판단 엔진을 별도 이름으로 읽는 비교 어댑터. 병합하지 않는다.

판단 엔진과 지시문은 원본 그대로 쓰며, 경로 생성만 세 방식 공통 A*로 교체한다.
원본의 AI 경로 생성·실행 게이트 전체를 평가하는 도구는 아니다.
"""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from types import ModuleType, SimpleNamespace

from backend.app.config import BASE_DIR
from backend.app.services.traffic_joint_actions import Step
from .traffic_strategy_search import LimitReached

JHLEE_REVISION = "d0ccc7de3980aa64afa79a7c43487c6372c88650"
MODULES = ("kev_graph", "kev_conflict", "kev_traffic", "kev_client", "kev_engine")


def load_jhlee(*, repo=BASE_DIR, revision=JHLEE_REVISION, source_output=None):
    """명시한 Git 커밋의 파일만 실행하고 원본 해시를 기록한다. 작업 파일은 쓰지 않는다."""
    if revision != JHLEE_REVISION:
        raise ValueError("비교 버전을 바꾸려면 실험 계획과 고정 커밋을 함께 검토하세요.")
    namespace = f"_traffic_strategy_jhlee_{revision}"
    package = ModuleType(namespace)
    package.__path__ = []
    sys.modules[namespace] = package
    manifest = {}
    for name in MODULES:
        path = f"backend/app/services/kev_mapf/{name}.py"
        raw = subprocess.run(["git", "show", f"{revision}:{path}"], cwd=repo,
                             check=True, capture_output=True, timeout=10).stdout
        manifest[path] = hashlib.sha256(raw).hexdigest()
        if source_output is not None:
            destination = Path(source_output) / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(raw)
        module = ModuleType(f"{namespace}.{name}")
        module.__package__ = namespace
        module.__file__ = f"{repo}@{revision}:{path}"
        sys.modules[module.__name__] = module
        exec(compile(raw, module.__file__, "exec"), module.__dict__)
        setattr(package, name, module)
    return SimpleNamespace(
        Graph=package.kev_graph.Graph, conflicts=package.kev_conflict,
        KevMAPF=package.kev_engine.KevMAPF,
        provenance=dict(revision=revision, source_sha256=manifest,
                        integration="ORIGINAL_ENGINE_COMMON_LOCAL_ROUTE_SERVICE_AND_ASTAR_ADAPTER"),
    )


class SharedJhleeClient:
    def __init__(self, selector, budget):
        self.selector, self.budget = selector, budget
        self.warnings = []

    @property
    def calls(self):
        return self.budget.model_calls

    @property
    def forced(self):
        return self.budget.forced

    @property
    def model_seconds(self):
        return sum(record["elapsed_seconds"] for record in self.budget.calls)

    def pick(self, state, candidates, instructions, question_id):
        return self.budget.choose(self.selector, state, candidates, instructions, question_id)

    def route(self, graph_data, start, goal, **kwargs):
        return self.budget.route(graph_data, start, goal, **kwargs)


def advisory_plan(scene, advisory, *, max_steps):
    """우선순위를 한 번 정렬한다. 실패한 제안을 코드 탐색으로 몰래 수리하지 않는다.

    이번 공통 동작은 현재 노드에서 기다리기와 목적지까지 이동하기뿐이다.
    순차 실행은 보수적인 공통 시험 규칙이며 동시 주행 성능 비교가 아니다.
    """
    if advisory.get("status") not in ("PROPOSED", "REVIEW_REQUIRED"):
        raise ValueError("NATIVE_PROPOSAL_BLOCKED")
    for decision in advisory["decisions"]:
        if decision["action"] == "WAIT":
            rid = decision["yield_robot"]
            if str(decision["wait_node"]) != scene.requests[rid]["current_node"]:
                raise ValueError("WAIT_REQUIRES_NONCOMMON_MOVEMENT")
    pending = {rid for rid, r in scene.requests.items() if r["current_node"] != r["node"]}
    if len(pending) > max_steps:
        raise ValueError("STEP_LIMIT")
    relations = {tuple(pair) for pair in advisory["global_priority_relations"]}
    steps = []
    while pending:
        eligible = [rid for rid in pending if not any(low == rid and high in pending for high, low in relations)]
        if not eligible:
            raise ValueError("PRIORITY_CYCLE")
        rid = min(eligible, key=lambda rid: (scene.requests[rid]["request_order"], rid))
        request = scene.requests[rid]
        path = scene.check_path(advisory["paths"][rid], start=request["current_node"], goal=request["node"])
        steps.append(Step(rid, "PROCEED" if path == request["path"] else "REROUTE", path))
        pending.remove(rid)
    return scene._plan("JHLEE_PLAN", "STRATEGY_ADVISORY", steps)


def run_jhlee(scene, budget, source, selector):
    robots = {
        rid: dict(id=rid, current_node=int(r["current_node"]), goal_node=int(r["node"]),
                  path=[int(n) for n in r["path"]], source="common_astar", status="IDLE",
                  task_priority=0, selector="algorithm", model="common_astar")
        for rid, r in scene.requests.items()
    }
    settings = dict(zone_occupancy=deepcopy(scene.config["zone_occupancy"]),
                    wait_allowed_nodes=[int(n) for n in scene.wait_nodes],
                    blocked_nodes=[int(n) for n in scene.blocked_nodes],
                    priority_mode="choice", allow_proactive_hold=False)
    engine = source.KevMAPF(source.Graph(scene.graph), robots, SharedJhleeClient(selector, budget),
                           settings, rounds=16, reroutes=budget.limits.max_route_calls)
    advisory, plan, reason = None, None, "PROPOSAL_VALIDATED"
    try:
        advisory = engine.resolve()
        budget.guard_time()
        candidate = advisory_plan(scene, advisory, max_steps=budget.limits.max_steps)
        rejection = budget.check(scene, candidate, complete=True)
        if rejection:
            reason = f"PREFLIGHT_REJECTED: {rejection}"
        else:
            plan = candidate
    except LimitReached as exc:
        reason = str(exc)
    except (ValueError, KeyError) as exc:
        reason = str(exc)
    return plan, dict(stop_reason=reason, native_advisory=advisory,
                     native_engine_limits=dict(rounds=16, max_reroutes=budget.limits.max_route_calls),
                     native_partial_decisions=deepcopy(engine.decisions),
                     native_partial_priority_relations=sorted([list(pair) for pair in engine.priorities]),
                     valid_complete_plans=int(plan is not None), optimality_claim=False,
                     first_solution_seconds=None if plan is None else budget.clock() - budget.started,
                     **budget.report())
