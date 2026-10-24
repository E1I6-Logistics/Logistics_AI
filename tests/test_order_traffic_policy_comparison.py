"""전체 흐름 비교의 시간·미완료·초기 상태 집계 정의를 확인한다."""

from copy import deepcopy
import pytest
from tests.order_traffic_policy_comparison import summarize, positions, digest, remaining_policies


def fixture():
    submitted=[dict(order_id="one",at_seconds=1,request_seconds=.9,scheduled_seconds=.8),
               dict(order_id="two",at_seconds=2,request_seconds=1.9,scheduled_seconds=1.8)]
    one=dict(order_id="one",status="COMPLETED",phase="COMPLETED",robot_id="robot1",total_quantity=1)
    two=dict(order_id="two",status="COMPLETED",phase="COMPLETED",robot_id="robot2",total_quantity=2)
    report=dict(state="COMPLETED",schedule=[{},{}],submitted=submitted,orders=[one,two],
                samples=3,capacity_violations=0,elapsed_seconds=10,initial_robots=[],graph_sha256="graph",
                mode=dict(simulation_settings=dict(speed_mps=.2)))
    observations=[]
    for t,statuses in ((3,("IN_PROGRESS","IN_PROGRESS")),(5,("COMPLETED","IN_PROGRESS")),(8,("COMPLETED","COMPLETED"))):
        orders=[dict(one,status=statuses[0]),dict(two,status=statuses[1])]
        observations.append(dict(at_seconds=t,orders=dict(orders=orders),robots=[dict(robot_id="robot1",status="WAITING")]))
    return report,observations


def test_summary_uses_order_ack_to_work_completion_and_separate_return_time():
    report,observations=fixture()
    traces=[dict(event="REQUEST"),dict(event="RESPONSE",elapsed_seconds=.3)]
    result=summarize(report,observations,[dict(selected_by="KEV")],traces)
    assert result["all_work_completed"] and result["all_returns_completed"]
    assert result["work_completion_seconds"]==8
    assert result["mean_order_seconds"]==5
    assert result["mean_dispatch_wait_seconds"]==1.5
    assert result["drain_seconds"]==10
    assert result["sampled_robot_waiting_seconds"]==5
    assert result["model_calls"]==result["model_responses"]==1
    assert result["summed_model_response_seconds"]==.3
    assert result["max_dispatch_wait_seconds"]==2
    assert result["sampled_longest_robot_waiting_seconds"]=={"robot1":5}
    assert result["max_pending"]==0 and result["max_in_progress"]==2


def test_incomplete_work_does_not_get_successful_whole_batch_metrics():
    report,observations=fixture()
    report["state"]="FAILED";report["orders"][1]["status"]="PENDING"
    observations=observations[:2]
    result=summarize(report,observations,[],[])
    assert not result["all_work_completed"]
    assert result["work_completion_seconds"] is None and result["mean_order_seconds"] is None
    assert result["batch_items_per_minute"] is None and result["drain_seconds"] is None


def test_return_failure_is_not_hidden_by_completed_work():
    report,observations=fixture();report["state"]="FAILED";report["orders"][1]["return_error"]="blocked"
    result=summarize(report,observations,[],[])
    assert result["all_work_completed"] and not result["all_returns_completed"]
    assert result["failed_orders"]==1 and result["drain_seconds"] is None


def test_initial_positions_ignore_telemetry_timestamp_and_input_row_order():
    rows=[dict(robot_id="robot2",occupied_node="1",x=1,y=0,status="IDLE",updated_at="old"),
          dict(robot_id="robot1",occupied_node="0",x=0,y=0,status="IDLE",updated_at="old")]
    changed=deepcopy(rows);changed.reverse()
    for row in changed:row["updated_at"]="new"
    assert digest(positions(rows))==digest(positions(changed))
    changed[0]["occupied_node"]="7"
    assert digest(positions(rows))!=digest(positions(changed))


def test_resume_skips_completed_trial_and_rejects_policy_failure():
    report=dict(trials=[dict(policy="MIN_EXPECTED_TIME",state="COMPLETED",summary=dict(all_returns_completed=True))])
    assert remaining_policies(report)==("KEV","KEV_STRATEGY_ORDER")
    report["trials"][0]["state"]="FAILED"
    with pytest.raises(ValueError,match="자동 재실행하지"):
        remaining_policies(report)
