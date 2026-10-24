"""대표 상황 입력을 만들고 코드만으로 준비를 확인한다. KEV·관제·ROS에 접속하지 않는다."""

import argparse
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
from time import monotonic
from zoneinfo import ZoneInfo

from backend.app.config import BASE_DIR
from backend.app.services.traffic_joint_actions import TrafficSituation, fingerprint
from backend.app.services.traffic_situation_adapter import build_traffic_situation_input
from backend.simulation.traffic_joint_actions import JointActionSimulation


CATALOG_PATH = Path(BASE_DIR) / 'tests/traffic_recovery_benchmark_cases.json'


def build_case(spec, graph, limits):
    states, requests, metadata = {}, [], {}
    for number, (rid, robot) in enumerate(spec['robots'].items(), 1):
        states[rid] = dict(status='IDLE', occupied_node=robot['start'], occupied_edge=None,
                           navigation_id=None, route=None, goal_node=None,
                           connected=True, fresh=True, active=False)
        requests.append(dict(order=dict(robot_id=rid, selector='kev', model='kev-08b'),
                             current_node=robot['start'], node=robot['goal'], result=dict(path=robot['path'])))
        metadata[rid] = dict(request_order=number, waiting_seconds=0)
    return build_traffic_situation_input(
        graph=graph, route_requests=requests, robot_states=states, reservations=[], protected_resources={},
        wait_allowed_nodes=spec['wait_allowed_nodes'], snapshot_id=f"benchmark-review-{spec['id']}",
        request_metadata=metadata, config=dict(recovery_search=deepcopy(limits),
            yield_paths=deepcopy(spec['yield_paths']), reroute_paths=deepcopy(spec['reroute_paths'])),
    )


def code_check(payload, strategy):
    value = deepcopy(payload)
    value['config'].update(recovery_strategy=strategy)
    if strategy == 'DEPENDENCY_DFS':
        value['config']['recovery_branch_policy'] = 'DEFER'
    started = monotonic()
    scene = TrafficSituation(value)
    planning_seconds = monotonic() - started
    result = JointActionSimulation(scene).execute(scene.decide())
    return dict(strategy=strategy, planning_seconds=planning_seconds,
                search=scene.search_report, result=result)


def prepare(catalog, graph):
    prepared = []
    for spec in catalog['cases']:
        payload = build_case(spec, graph, catalog['common_limits'])
        runs = [code_check(payload, strategy) for strategy in ('DEPENDENCY_DFS', 'BREADTH_FIRST')]
        solved = spec['expected'] == 'SOLVABLE'
        checks = {}
        for row in runs:
            prefix = row['strategy']
            result, search = row['result'], row['search']
            checks[prefix+'_expected_outcome'] = ((search['status']=='FOUND' and result['status']=='COMPLETED') if solved
                                                  else (search['status']=='EXHAUSTED' and result['steps_completed']==0))
            checks[prefix+'_no_capacity_violation'] = result['capacity_violations']==0
            checks[prefix+'_no_model_calls'] = search['model_calls']==0
        breadth = runs[1]
        checks['breadth_first_step_control'] = (not solved or
            breadth['result']['steps_completed']==spec['minimum_steps_control'])
        prepared.append(dict(id=spec['id'],title=spec['title'],spec=deepcopy(spec),input=payload,
                             input_sha256=fingerprint(payload),code_preparation=runs,
                             checks=checks,preparation_passed=all(checks.values())))
        print(spec['id'], 'PREPARED' if all(checks.values()) else 'REVIEW_REQUIRED',
              [(r['strategy'],r['search']['status'],r['search']['checks']) for r in runs],flush=True)
    return dict(created_at=datetime.now(ZoneInfo('Asia/Seoul')).isoformat(),
                status='AWAITING_OPERATIONAL_REVIEW_NOT_KEV_COMPARISON',
                model_requests_sent=0,live_fms_commands_sent=False,
                all_preparations_passed=all(c['preparation_passed'] for c in prepared),
                catalog=deepcopy(catalog),graph_sha256=fingerprint(graph),cases=prepared)


def summary(report):
    lines = ['# 대표 상황의 코드 준비 확인', '',
             '실제 KEV 비교는 아직 하지 않았다. 제공한 경로·정지 상태에서의 실행 가능성 확인이며 일반 주문·처리량 시험과 구분한다.', '',
             '| 상황 | 관계 코드 결과 | 검사 | BFS 첫 계획의 단계 수 | 준비 확인 |',
             '| --- | --- | ---: | ---: | --- |']
    for case in report['cases']:
        dependency,breadth=case['code_preparation']
        lines.append(f"| {case['title']} | {dependency['search']['status']} | {dependency['search']['checks']} | "
                     f"{breadth['search']['found_steps']} | {case['preparation_passed']} |")
    return '\n'.join(lines)+'\n'


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalog',type=Path,default=CATALOG_PATH)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    catalog=json.loads(args.catalog.read_text())
    graph=json.loads((Path(BASE_DIR)/catalog['map']).read_text())
    # 기존 결과를 덮어쓰지 않도록 실행할 때마다 새 폴더를 지정한다.
    args.output.mkdir(parents=True,exist_ok=False)
    report=prepare(catalog,graph)
    (args.output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    (args.output/'summary.md').write_text(summary(report))
    (args.output/'catalog.json').write_text(json.dumps(catalog,ensure_ascii=False,indent=2)+'\n')
    print('REPORT',str(args.output.resolve()),flush=True)
    if not report['all_preparations_passed']:
        raise SystemExit(1)


if __name__=='__main__':
    main()
