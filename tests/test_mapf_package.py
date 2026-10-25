"""폴더 이동 후 기존 호출·설정·로그와 새 모듈이 서로 분리되지 않는지 확인한다."""

from importlib import import_module
import json

import pytest


@pytest.mark.parametrize("name", [
    "order_traffic_service", "order_traffic_strategy", "traffic_route_provider",
])
def test_legacy_import_and_package_import_share_one_module(name):
    legacy = import_module("backend.app.services." + name)
    current = import_module("backend.app.services.mapf." + name)
    assert legacy is current


def test_legacy_log_override_controls_actual_record_writers(monkeypatch, tmp_path):
    from backend.app.config import BASE_DIR
    from backend.app.services import order_traffic_service as legacy
    from backend.app.services.mapf import order_traffic_service as current

    assert current.LOG_DIR == BASE_DIR / "logs" / "order-traffic"
    monkeypatch.setattr(legacy, "LOG_DIR", tmp_path)
    current._save({"state": "APPLIED"})
    current._save_selection_record({"event": "REQUEST"})
    assert json.loads((tmp_path / "decisions.jsonl").read_text()) == {"state": "APPLIED"}
    assert json.loads((tmp_path / "selection-inferences.jsonl").read_text()) == {"event": "REQUEST"}


def test_policy_override_is_shared_with_strategy_and_legacy_callers(monkeypatch):
    from backend.app.services import order_traffic_service as legacy
    from backend.app.services.mapf import order_traffic_service as current
    from backend.app.services.mapf import order_traffic_strategy as strategy

    monkeypatch.setattr(current, "_simulation_policy_override", None)
    monkeypatch.setenv("FMS_TRAFFIC_SELECTION_POLICY", "KEV")
    legacy.set_simulation_policy("MIN_EXPECTED_TIME")
    assert current.selection_policy() == "MIN_EXPECTED_TIME"
    assert strategy.common is current
    current.set_simulation_policy("KEV_STRATEGY_ORDER")
    assert legacy.selection_policy() == "KEV_STRATEGY_ORDER"


def test_joint_action_legacy_helper_reuses_shared_fingerprint():
    from backend.app.services.mapf.common import fingerprint
    from backend.app.services.traffic_joint_actions import fingerprint as legacy

    assert legacy is fingerprint
    assert fingerprint({"a": 1, "b": ["노드"]}) == legacy({"b": ["노드"], "a": 1})


def test_future_comparison_source_capture_includes_executed_implementations():
    from pathlib import Path

    from tests.order_traffic_policy_comparison import ROOT, SOURCE_PATHS
    from backend.app.services.mapf import common, order_traffic_service, order_traffic_strategy, traffic_route_provider

    for module in (common, order_traffic_service, order_traffic_strategy, traffic_route_provider):
        assert Path(module.__file__).relative_to(ROOT).as_posix() in SOURCE_PATHS
