"""입력 축약이 후보·예약 제약·원본과 실제 요청 기록을 훼손하지 않는지 확인한다."""

from copy import deepcopy
import json

import pytest

from backend.simulation.traffic_recovery_prompt_probe import concise_dependency_request, ConciseDependencySelector
from backend.app.services.traffic_joint_actions import TrafficSituation
from backend.simulation.traffic_joint_actions import JointActionSimulation
from tests.test_traffic_recovery_dependencies import payload


def request_example():
    move=dict(robot_id='arbitrary_robot', path=['11','6'], current_other_occupants=[],
              finishes_on_required_nodes=[dict(robot_id='other',node='6')],
              estimated_motion=dict(travel_seconds=1.123456789,mean_goal_completion_lower_bound_seconds=None))
    state=dict(dependency_facts=dict(offered_moves={'Z':move},irreversible_blockers=[],robots=[]),
               blocked_nodes=['9'],blocked_edges=[['8','10']],recent_checks=[dict(result='REJECTED')],
               planning_control=dict(remaining_model_calls=3))
    criteria={'Z':json.dumps(move),'BACKTRACK':'현재 순서를 미루기'}
    return state,criteria


def test_preserves_every_choice_constraint_numeric_value_and_original_request():
    state,criteria=request_example(); original=deepcopy((state,criteria))
    shortened,choices,_=concise_dependency_request(state,criteria)
    assert choices==criteria and (state,criteria)==original
    assert json.loads(choices['Z'])==state['dependency_facts']['offered_moves']['Z']
    assert 'offered_moves' not in shortened['dependency_facts']
    restored=deepcopy(shortened);restored['dependency_facts']['offered_moves']=deepcopy(state['dependency_facts']['offered_moves'])
    assert restored==state


@pytest.mark.parametrize('change', ['missing','different'])
def test_refuses_stale_or_missing_candidate_data(change):
    state,criteria=request_example()
    if change=='missing':del criteria['Z']
    else:criteria['Z']=json.dumps(dict(robot_id='wrong',path=['0']))
    with pytest.raises(ValueError,match='다릅니다'):concise_dependency_request(state,criteria)


def test_bad_compact_proposal_still_fails_reservation_and_sends_no_movement():
    class BadSelector:
        last_inference=None
        def select_choice(self,state,criteria,instructions,**kwargs):
            assert 'offered_moves' not in state['dependency_facts']
            key=next((k for k,v in criteria.items() if k!='BACKTRACK' and json.loads(v)['robot_id']=='robot1'),next(iter(criteria)))
            self.last_inference={'request':dict(state=deepcopy(state),criteria=deepcopy(criteria))}
            return {'choice':key}
    value=payload();value['config'].update(recovery_motion_estimates=True,recovery_branch_policy='DEFER')
    scene=TrafficSituation(value,recovery_selector=ConciseDependencySelector(BadSelector()))
    calls=scene.search_report['guidance_calls']
    assert calls[0]['check_result']=='NO_FEASIBLE_RESERVATION'
    assert 'offered_moves' not in calls[0]['inference']['request']['state']['dependency_facts']
    assert scene.search_report['status']!='FOUND'
    assert JointActionSimulation(scene).execute(scene.decide())['steps_completed']==0
