"""원본 jhlee 주문 게이트의 연결·취소·기록 경계를 실제 모델 없이 검사한다."""

import asyncio
from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace

import pytest

from backend.app.services import llm_route_preview
from backend.simulation import order_traffic_jhlee as adapter
from tests.test_order_traffic_service import env, order, place
from tests.order_traffic_strategy_smoke import order_schedule
from tests.order_traffic_policy_comparison import remaining_policies


@pytest.fixture
def native(env, monkeypatch):
    events = []
    monkeypatch.setattr(adapter, "audit", events.append)
    source = adapter.load_native()
    monkeypatch.setattr(llm_route_preview, "preview_llm_route", adapter.common_astar_preview)
    return source, events


def test_loads_eight_fixed_native_files_and_keeps_source_hashes(tmp_path):
    source = adapter.load_native(output=tmp_path)
    assert len(source.source_sha256) == 8
    for path, digest in source.source_sha256.items():
        assert hashlib.sha256((tmp_path / path).read_bytes()).hexdigest() == digest
    assert source.package.kev_dispatcher.fms_mapf_coordinator is source.package.kev_coordinator.fms_mapf_coordinator
    assert source.package.kev_dispatcher.dispatch_simulation_mapf.__module__.startswith("backend.app.services._jhlee_orders_")


def test_common_astar_respects_original_virtual_graph_exclusions(native, env):
    source, events = native
    _, _, graph = env
    before = deepcopy(graph)
    virtual = source.package.kev_graph.Graph(graph).virtual_graph({7}, set())
    path = adapter.common_astar_preview(virtual, "9", "11", traffic={})["path"]
    assert "7" not in path and path[0] == "9" and path[-1] == "11"
    assert graph == before and events[-1]["status"] == "GENERATED"


def test_common_astar_preserves_traffic_occupancy_avoidance_without_mutating_it(native, env):
    _, events = native
    _, _, graph = env
    traffic = dict(occupied_nodes={6: "robot2"}, robot_paths={})
    before = deepcopy(traffic)
    result = adapter.common_astar_preview(graph, "5", "3", traffic=traffic)
    assert result["path"] == ["5", "7", "11", "3"]
    assert traffic == before and not events[-1]["traffic_fallback"]


def test_traffic_avoidance_cannot_restore_edges_removed_by_native_reroute(native, env):
    source, events = native
    _, _, graph = env
    virtual = source.package.kev_graph.Graph(graph).virtual_graph({7}, set())
    result = adapter.common_astar_preview(virtual, "5", "3", traffic=dict(occupied_nodes={6: "robot2"}))
    assert result["path"] == ["5", "6", "11", "3"]
    assert "7" not in result["path"] and events[-1]["traffic_fallback"]


def test_original_default_planner_can_resolve_the_shared_repository(native, env):
    source, _ = native
    _, _, graph = env
    request = dict(order=dict(order(), robot_id="robot1"), current_node="0", node="5",
                   result=dict(path=["0", "10", "8", "7", "5"]))
    # 단일 로봇의 충돌 없는 입력이므로 실제 HTTP 호출 없이 원본 LiveKEV까지 초기화한다.
    result = source.package.kev_adapter.plan_mapf_from_route_results(graph, [request])
    assert result["required_paths"]["robot1"] == request["result"]["path"]
    assert result["metrics"]["kev_choice_calls"] == 0
    assert result["fms_execution_approved"] is False


def test_single_pending_uses_original_gate_and_fms_without_model(native, env, monkeypatch):
    source, events = native
    store, _, _ = env
    monkeypatch.setenv("FMS_SIM_MAPF_MAX_WAIT", "1")
    def unexpected(*args, **kwargs):
        raise AssertionError("단독 Pending은 모델을 호출하면 안 됩니다.")
    monkeypatch.setattr(source.package.kev_coordinator.fms_mapf_coordinator, "evaluate_mixed_simulation_advisory", unexpected)
    row = dict(order(), order_id="single", phase="MOVING_TO_PICKUP")
    response = asyncio.run(asyncio.wait_for(source.package.kev_dispatcher.dispatch_simulation_mapf(row, "5", "0"), 2))
    assert response["success"]
    assert store._robots["robot1"]["goal_node"] == "5"
    assert source.package.kev_coordinator.fms_mapf_coordinator.pending_snapshot() == []
    assert source.package.kev_coordinator.fms_mapf_coordinator.snapshot()[0]["result"]["path"][-1] == "5"


def test_cancelled_native_prepare_does_not_register_or_move(native, env):
    source, _ = native
    store, _, _ = env
    with pytest.raises(ValueError, match="주문 단계"):
        asyncio.run(source.package.kev_dispatcher.dispatch_simulation_mapf(dict(order(), order_id="cancel", phase="MOVING_TO_PICKUP"), "5", "0", valid=lambda: False))
    assert store._robots["robot1"]["route"] is None
    assert not source.package.kev_coordinator.fms_mapf_coordinator.pending_snapshot()


def test_removed_pending_cannot_be_dispatched_from_prepared_data(native):
    source, _ = native
    coordinator = source.package.kev_coordinator.fms_mapf_coordinator
    row = dict(order(), order_id="stale", phase="MOVING_TO_PICKUP")
    prepared = asyncio.run(source.package.kev_dispatcher.prepare_order_route(row, "5", "0"))
    coordinator.discard_pending("robot1")
    with pytest.raises(ValueError, match="Pending"):
        source.package.kev_dispatcher.validate_prepared_order_route(
            row, "5", "0", prepared)


def test_native_rejects_proposal_that_would_stop_an_existing_active_robot(native, monkeypatch):
    source, _ = native
    coordinator = source.package.kev_coordinator.fms_mapf_coordinator
    pending = dict(order={"robot_id": "robot1"}, current_node="0", node="5", result={"path": ["0", "10", "8", "7", "5"]})
    active = dict(order={"robot_id": "robot2"}, current_node="1", node="6", result={"path": ["1", "9", "7", "11", "6"]})
    requests = [pending, active]
    monkeypatch.setattr(coordinator, "_state_triplet", lambda: ({}, [pending], [active]))
    monkeypatch.setattr(coordinator, "prepare_mixed_simulation_requests", lambda: dict(status="READY", requests=requests,
        active_robot_ids=["robot2"], pending_robot_ids=["robot1"]))
    proposal = dict(status="PROPOSED", analysis_source="MIXED_ACTIVE_PENDING", fms_execution_approved=False,
        mixed_fms_at_analysis={}, mixed_pending_at_analysis=[pending], mixed_active_at_analysis=[active],
        mixed_requests_at_analysis=requests, robot_count=2, pending_robot_ids=["robot1"], active_robot_ids=["robot2"],
        decisions=[dict(action="WAIT", yield_robot="robot2", wait_node=1)])
    result = coordinator.classify_mixed_pending_advisory(proposal)
    assert result["status"] == "BLOCKED" and result["reason"] == "ACTIVE_ROBOT_ACTION_REQUIRES_STOP"


def test_native_collection_blocks_clear_return_when_another_pending_path_crosses_unmodeled_idle_robot(native, env):
    source, _ = native
    store, _, graph = env
    for rid, node in (("robot1", "4"), ("robot2", "6"), ("robot3", "5")):
        place(store, rid, node, graph)
    coordinator = source.package.kev_coordinator.fms_mapf_coordinator
    clear_return = ["4", "12", "8", "10", "0"]
    coordinator.stage_preview(dict(order(), order_id="return"), "4", "0", dict(path=clear_return))
    coordinator.stage_preview(dict(order(), order_id="work", robot_id="robot3"), "5", "3", dict(path=["5", "6", "11", "3"]))
    result = coordinator.prepare_mixed_simulation_requests()
    assert "6" not in clear_return
    assert result["status"] == "BLOCKED"
    assert result["issues"] == {"robot2": "UNMODELED_IDLE_OCCUPANCY"}
    assert result["pending_robot_ids"] == ["robot1", "robot3"]
    assert all(store._robots[rid]["route"] is None for rid in store._robots)


def test_transport_records_exact_multi_question_payload_response_and_context(monkeypatch):
    traces = []
    monkeypatch.setattr(adapter.records, "_save_selection_record", traces.append)
    reply = dict(answers=dict(priority_score_robot1=dict(type="score", score=.4)))
    selector = SimpleNamespace(_transport=lambda *args: reply, _runtime=dict(device="cuda"))
    adapter.record_selector(selector)
    context = dict(order_id="test", robot_id="robot1", phase="MOVING_TO_PICKUP", goal_node="5", calls=0)
    token = adapter._context.set(context)
    payload = dict(state={"robots": []}, questions={"priority_score_robot1": {"type": "score"}})
    try:
        assert selector._transport("http://example.invalid", payload, {}, 2) == reply
    finally:
        adapter._context.reset(token)
    assert context["calls"] == 1
    assert traces[0]["request"] == payload
    assert traces[1]["inference"]["request"] == payload and traces[1]["inference"]["response"] == reply
    assert not traces[1]["fms_execution_approved"] and traces[1]["exact_transport_request_saved"]


def test_transport_error_is_recorded_without_success(monkeypatch):
    traces = []
    monkeypatch.setattr(adapter.records, "_save_selection_record", traces.append)
    def fail(*args):
        raise TimeoutError("late")
    selector = SimpleNamespace(_transport=fail, _runtime={})
    adapter.record_selector(selector)
    token = adapter._context.set(dict(order_id="t", robot_id="robot1", phase="RETURNING", goal_node="0", calls=0))
    try:
        with pytest.raises(TimeoutError):
            selector._transport("unused", {}, {}, 2)
    finally:
        adapter._context.reset(token)
    assert [t["event"] for t in traces] == ["REQUEST", "ERROR"]


def test_install_requires_explicit_preview_and_identified_process(monkeypatch):
    monkeypatch.delenv("FMS_PREVIEW_RUN_ID", raising=False)
    with pytest.raises(ValueError, match="시뮬 전용"):
        adapter.install(preview_only=True)


def test_stage_schedules_are_prefixes_and_custom_resume_retains_policy_set():
    assert order_schedule("single") == order_schedule("six")[:1]
    assert order_schedule("overlap") == order_schedule("six")[:2]
    report = dict(policy_order=["MIN_EXPECTED_TIME", "KEV_STRATEGY_ORDER", adapter.POLICY],
                  trials=[dict(policy="MIN_EXPECTED_TIME", state="COMPLETED", summary=dict(all_returns_completed=True))])
    assert remaining_policies(report) == ("KEV_STRATEGY_ORDER", adapter.POLICY)
