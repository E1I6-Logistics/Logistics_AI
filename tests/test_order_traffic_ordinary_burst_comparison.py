"""접수 속도만 바꾸고 사용자 코드·지도와 시험 사본을 분리하는지 확인한다."""

from copy import deepcopy

import pytest

from tests.order_traffic_ordinary_burst_comparison import build_plan, ordinary_items, freeze_workspace, frozen_unchanged
from tests.order_traffic_native_mode_comparison import ARMS


@pytest.mark.parametrize("seed", [42, 137])
def test_normal_and_burst_preserve_order_contents_and_change_only_arrival_times(seed):
    normal, burst = build_plan([seed])
    assert normal["item_sequence_sha256"] == burst["item_sequence_sha256"]
    assert [row["items"] for row in normal["orders"]] == [row["items"] for row in burst["orders"]]
    assert [row["at_seconds"] for row in normal["orders"]] == list(range(0, 144, 12))
    assert [row["at_seconds"] for row in burst["orders"]] == [0, 12, 24]+[36]*9
    before = deepcopy(burst["orders"])
    normal["orders"][0]["items"]["A"] += 1
    assert burst["orders"] == before


def test_generated_orders_include_both_single_pickup_and_two_pickup_and_respect_capacity():
    items = ordinary_items(42)
    assert items == ordinary_items(42) and items != ordinary_items(137)
    assert len(items) == 12
    both = lambda row: bool(row["A"]+row["B"]) and bool(row["C"]+row["D"])
    assert sum(both(row) for row in items) == 4
    assert sum(bool(row["A"]+row["B"]) and not both(row) for row in items) == 4
    assert sum(bool(row["C"]+row["D"]) and not both(row) for row in items) == 4
    assert all(set(row) == set("ABCD") and 1 <= sum(row.values()) <= 5 for row in items)


def test_policy_order_is_balanced_across_demand_patterns():
    plan = build_plan([42, 137])
    assert [row["arms"] for row in plan] == [list(ARMS), list(reversed(ARMS)), list(reversed(ARMS)), list(ARMS)]


def test_user_edits_do_not_change_frozen_code_or_map_and_copy_changes_are_detected(tmp_path):
    source, frozen = tmp_path/"user", tmp_path/"isolated"
    for name, text in [("backend/app/config.py", "old config"), ("tests/runner.py", "old test"),
                       ("routes/test.geojson", "old map"), ("maps/map.yaml", "old image config"),
                       ("backend/__pycache__/unused.pyc", "skip")]:
        path = source/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    hashes = freeze_workspace(source, frozen)
    assert len(hashes) == 4
    (source/"backend/app/config.py").write_text("user changes config")
    (source/"routes/test.geojson").write_text("user changes map")
    assert frozen_unchanged(frozen, hashes)
    assert (frozen/"routes/test.geojson").read_text() == "old map"
    (frozen/"routes/test.geojson").write_text("unexpected change")
    assert not frozen_unchanged(frozen, hashes)
