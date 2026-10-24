"""비교 보고서가 미완료와 계산값을 실제 성과로 오해하지 않는지 검사한다."""
from copy import deepcopy
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))
import order_traffic_comparison as comparison


def test_comparison_summary_keeps_incomplete_metrics_unset(tmp_path, monkeypatch):
    monkeypatch.setattr(comparison, 'ROOT', tmp_path)
    report = dict(schedule=[{},{}], orders=[dict(order_id='one',status='COMPLETED',total_quantity=2)],
        submitted={'one':dict(ack_seconds=1,request_seconds=1,scheduled_seconds=1)},
        completed_at={'one':5}, dispatched_at={'one':2}, elapsed_seconds=10, robot_waiting_seconds={})
    comparison.summarize(report)
    assert not report['summary']['all_work_completed']
    assert report['summary']['mean_order_seconds'] is None
    assert report['summary']['completed_items_per_minute'] is None
    assert report['summary']['work_completion_seconds'] is None


def test_comparison_summary_distinguishes_actual_time_from_candidate_time(tmp_path, monkeypatch):
    monkeypatch.setattr(comparison, 'ROOT', tmp_path)
    candidate = lambda seconds: dict(estimated_metrics=dict(new_request_completion_seconds=seconds))
    report = dict(schedule=[{}], orders=[dict(order_id='one',status='COMPLETED',total_quantity=2,
        traffic_coordination_history=[dict(state='APPLIED',choice='slow',selected_by='KEV',selection_seconds=.2,
            candidates=dict(slow=candidate(4),fast=candidate(3)))])],
        submitted={'one':dict(ack_seconds=1,request_seconds=1.1,scheduled_seconds=1)},
        completed_at={'one':7}, dispatched_at={'one':2}, elapsed_seconds=10, robot_waiting_seconds={'robot1':1.5})
    comparison.summarize(report)
    summary=report['summary']
    assert summary['mean_order_seconds']==6
    assert summary['work_completion_seconds']==7
    assert summary['drain_seconds']==10
    assert summary['mean_dispatch_wait_seconds']==1
    assert summary['minimum_expected_matches']==0
    assert summary['multi_candidate_decisions']==1
    assert summary['sampled_robot_waiting_seconds']==1.5
    assert abs(summary['max_registration_lateness_seconds']-.1)<1e-9
    # 완료 관측이 없으면 일부 주문만의 평균을 전체 결과로 쓰지 않는다.
    missing=deepcopy(report);missing['completed_at']={}
    comparison.summarize(missing)
    assert missing['summary']['mean_order_seconds'] is None
