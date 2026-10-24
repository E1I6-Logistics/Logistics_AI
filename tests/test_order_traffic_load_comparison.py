"""주문표가 비교 조건을 지키며 잘못된 수량·시각을 운행 전에 막는지 확인한다."""

import json
from pathlib import Path

import pytest

from tests.order_traffic_load_comparison import CATALOG, build_plan
from tests.order_traffic_strategy_smoke import load_schedule


def test_cases_repeat_identical_items_with_different_load_and_alternate_policy_order(tmp_path):
    catalog = json.loads(CATALOG.read_text())
    plan = build_plan(catalog)
    assert plan == build_plan(catalog)
    assert len(plan) == 6 and len({row["id"] for row in plan}) == 6
    for row in plan:
        path = tmp_path / (row["id"] + ".json")
        path.write_text(json.dumps(dict(orders=row["orders"])))
        loaded = load_schedule(path)
        assert len(loaded) == 12 and all(1 <= sum(items.values()) <= 5 for _, items in loaded)
        if row["case"] == "pickup_ab":
            assert all(items["C"] == items["D"] == 0 for _, items in loaded)
        else:
            assert all(items["A"] + items["B"] > 0 and items["C"] + items["D"] > 0 for _, items in loaded)
    for seed in catalog["seeds"]:
        mixed = next(row for row in plan if row["seed"] == seed and row["case"] == "mixed_pickups")
        fast = next(row for row in plan if row["seed"] == seed and row["case"] == "fast_queue")
        assert [row["items"] for row in mixed["orders"]] == [row["items"] for row in fast["orders"]]
        assert fast["orders"][-1]["at_seconds"] < mixed["orders"][-1]["at_seconds"]
    assert plan[0]["policies"] == list(reversed(plan[3]["policies"]))
    assert plan[0]["orders"] != plan[3]["orders"]


@pytest.mark.parametrize("orders", [
    [],
    [dict(at_seconds=-1, items=dict(A=1, B=0, C=0, D=0))],
    [dict(at_seconds=float("nan"), items=dict(A=1, B=0, C=0, D=0))],
    [dict(at_seconds=True, items=dict(A=1, B=0, C=0, D=0))],
    [dict(at_seconds=0, items=dict(A=6, B=0, C=0, D=0))],
    [dict(at_seconds=0, items=dict(A=True, B=0, C=0, D=0))],
    [dict(at_seconds=0, items=dict(A=1))],
    [dict(at_seconds=1, items=dict(A=1, B=0, C=0, D=0)), dict(at_seconds=0, items=dict(A=1, B=0, C=0, D=0))],
])
def test_invalid_schedule_is_rejected_before_starting_a_server(tmp_path, orders):
    path = tmp_path / "orders.json"
    path.write_text(json.dumps(dict(orders=orders)))
    with pytest.raises(ValueError):
        load_schedule(path)
