"""화면용 비교 조건과 검사 전용 보관의 경계, 한도 뒤 무출발을 검증한다."""

from copy import deepcopy

from backend.app.services.scenario_service import load_config
from backend.app.services.traffic_joint_actions import TrafficSituation
from backend.simulation.traffic_recovery_benchmark import build_case
from backend.simulation.traffic_joint_actions import JointActionSimulation
from tests.test_traffic_recovery_deferred import PostponeSelector


COMPARISON_NAMES = (
    "dependency_code_two_robot_yield",
    "dependency_kev_two_robot_yield",
    "guided_deferred_two_robot_yield",
)


def test_public_comparison_uses_identical_movements_limits_and_branch_retention():
    items = {s["name"]: s for s in load_config()["scenarios"]}
    common = []
    for name in COMPARISON_NAMES:
        scenario = deepcopy(items[name])
        options = scenario["traffic_joint_action"]
        assert scenario["catalog_status"] == "ACTIVE"
        assert scenario["test_group"] == "여러 대 양보 비교"
        assert options["recovery_branch_policy"] == "DEFER"
        common.append((scenario["robots"], {
            k: options[k] for k in (
                "paths", "yield_paths", "wait_allowed_nodes", "recovery_search", "timeout_seconds",
                "recovery_motion_estimates",
            )
        }))
    assert common[0] == common[1] == common[2]
    kev = items[COMPARISON_NAMES[1]]["traffic_joint_action"]
    hybrid = items[COMPARISON_NAMES[2]]["traffic_joint_action"]
    assert kev["recovery_guidance"] == hybrid["recovery_guidance"]
    assert kev["recovery_guidance_budget_policy"] == "STOP"
    assert hybrid["recovery_guidance_budget_policy"] == "CODE_CONTINUE"


def test_old_cli_names_remain_available_without_cluttering_active_catalog():
    config = load_config()
    active = [s for s in config["scenarios"] if s["test_group"] != "개발용 보관"]
    archived = [s for s in config["scenarios"] if s["test_group"] == "개발용 보관"]
    assert len(active) == config["catalog_review"]["active_count"] == 9
    assert len(archived) == config["catalog_review"]["archived_count"] == 14
    assert all(s["catalog_status"] == "ARCHIVED" and s["archive_reason"] for s in archived)
    assert {s["name"] for s in archived} >= {
        "fixed_two_robot_yield", "search_two_robot_yield", "guided_two_robot_yield",
        "dfs_two_robot_yield", "cycle", "bottleneck", "kev_joint_yield", "kev_joint_reroute",
    }
    assert sum("order_stream" in s for s in active) == 1
    assert all("traffic_joint_action" in s for s in active if "order_stream" not in s)


def test_published_kev_only_scenario_cannot_execute_partial_plan_after_model_budget():
    import json
    from pathlib import Path
    from backend.app.config import BASE_DIR

    scenario = next(s for s in load_config()["scenarios"] if s["name"] == COMPARISON_NAMES[1])
    options = scenario["traffic_joint_action"]
    spec = dict(id=scenario["name"], wait_allowed_nodes=options["wait_allowed_nodes"],
                yield_paths=options["yield_paths"], reroute_paths={}, robots={})
    for robot in scenario["robots"]:
        rid, start = robot["id"], robot["start"]
        spec["robots"][rid] = dict(start=start, goal=robot["goal"] or start,
                                   path=options["paths"].get(rid, [start]))
    graph = json.loads((Path(BASE_DIR) / "routes/test.geojson").read_text())
    value = build_case(spec, graph, options["recovery_search"])
    value["config"].update({k: options[k] for k in (
        "recovery_strategy", "recovery_guidance", "recovery_branch_policy", "recovery_guidance_budget_policy",
        "recovery_motion_estimates",
    )})
    selector = PostponeSelector()
    scene = TrafficSituation(value, recovery_selector=selector)
    assert scene.search_report["status"] == "MODEL_CALL_LIMIT"
    assert scene.search_report["model_calls"] == options["recovery_guidance"]["max_calls"]
    assert scene.search_report["code_after_model_budget_decisions"] == 0
    simulator = JointActionSimulation(scene)
    before = deepcopy(simulator.store._robots)
    result = simulator.execute(scene.decide())
    assert result["steps_completed"] == 0
    assert simulator.store._robots == before
