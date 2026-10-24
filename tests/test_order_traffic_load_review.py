"""분기 분석이 서로 다른 목적지를 놓치거나 관측 시각을 확정값으로 바꾸지 않게 한다."""

from copy import deepcopy

import pytest

from backend.simulation.order_traffic_load_review import compare_moves, first_difference, minimum_choice, observe, strategy_review


def event(goal="3"):
    return dict(state="APPLIED", phase="MOVING_TO_WORKSTATION", robot_id="robot1", goal_node=goal,
                path=["6", "11", goal])


def test_same_leg_is_compared_even_when_the_workstation_changes():
    key = ("ORDER-1", 2)
    a = {key:dict(after_seconds=10, by_seconds=10.2, event=event("3"))}
    b = {key:dict(after_seconds=10.1, by_seconds=10.3, event=event("4"))}
    result = compare_moves(a,b)
    assert result[0]["order_name"] == "ORDER-1" and result[0]["leg_index"] == 2
    assert result[0]["changed"] == ["goal_node", "path"]


def test_observation_keeps_bounds_and_rejects_rewritten_history():
    order = dict(order_name="ORDER-1", robot_id="robot1", status="IN_PROGRESS", phase="MOVING_TO_WORKSTATION",
                 workstation_node="3", traffic_coordination_history=[])
    before = dict(at_seconds=10, orders=dict(orders=[deepcopy(order)]), robots=[])
    order["traffic_coordination_history"].append(event())
    after = dict(at_seconds=10.2, orders=dict(orders=[deepcopy(order)]), robots=[])
    result = observe([before,after], [order])
    move = result["moves"][("ORDER-1",0)]
    assert move["after_seconds"] == 10 and move["by_seconds"] == 10.2
    changed = deepcopy(after); changed["at_seconds"] = 10.4
    changed["orders"]["orders"][0]["traffic_coordination_history"][0]["path"] = ["6","3"]
    with pytest.raises(ValueError,match="이력이 바뀌"):
        observe([before,after,changed], [order])


def test_first_assignment_difference_uses_observed_time_and_keeps_both_intervals():
    a={"ORDER-9":dict(after_seconds=20,by_seconds=21,robot_id="robot1"),
       "ORDER-2":dict(after_seconds=10,by_seconds=11,robot_id="robot2")}
    b={"ORDER-9":dict(after_seconds=19,by_seconds=20,robot_id="robot2"),
       "ORDER-2":dict(after_seconds=9,by_seconds=10,robot_id="robot1")}
    result=first_difference(a,b,"robot_id")
    assert result["order_name"]=="ORDER-2"
    assert result["code"]["after_seconds"]==10 and result["kev"]["after_seconds"]==9


def test_minimum_rule_is_independent_of_candidate_order_but_keeps_small_real_differences():
    candidates={name:dict(estimated_metrics=dict(new_request_completion_seconds=value,total_distance_m=2))
                for name,value in [("KEEP_PATH",10.0000000001),("REROUTE_1",10)]}
    assert minimum_choice(candidates)==minimum_choice(dict(reversed(list(candidates.items()))))=="REROUTE_1"
    candidates["KEEP_PATH"]["estimated_metrics"]["new_request_completion_seconds"]=10
    assert minimum_choice(candidates)=="KEEP_PATH"


def test_strategy_budget_skip_is_visible_and_not_counted_as_a_successful_path_check():
    candidates={"KEEP_PATH":dict(estimated_metrics=dict(new_request_completion_seconds=10,total_distance_m=2))}
    row=dict(kev_calls=0,retries=0,choice="KEEP_PATH",candidates=candidates,
             strategy_batches=[dict(strategy_order=["KEEP_PATH","REROUTE_1"],attempts=[
                 dict(strategy="KEEP_PATH",result="VALID"),dict(strategy="REROUTE_1",result="ROUTE_CALL_LIMIT")])])
    result=strategy_review([row],[])
    assert result["all_strategy_ids_attempted"]==1
    assert result["VALID"]==1 and result["ROUTE_CALL_LIMIT"]==1
