"""기존 관제에서 동일 주문표로 KEV/계산값 선택을 비교. 기본은 계획 출력.

--run은 로컬 시뮬·AI 백엔드를 작업이 없을 때 재시작한다. 이력·모드·위치를
보관·복원하며 기존 주문은 삭제하지 않는다. 접수한 시험 주문도 자동 취소하지 않는다.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import random
import signal
import subprocess
import sys
import time
from urllib.request import Request, urlopen
from uuid import uuid4
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
BASE_URL = 'http://127.0.0.1:8000/'


def request(endpoint, payload=None):
    req = Request(BASE_URL+endpoint, data=None if payload is None else json.dumps(payload).encode(),
                  headers={'Content-Type': 'application/json'})
    with urlopen(req, timeout=8) as response:
        return json.load(response)


def snapshot():
    return {key: request(ep) for key, ep in dict(mode='api/mode', orders='api/orders',
            robots='api/robots', scenario='api/scenarios/status').items()}


def ensure_idle(saved):
    mode = saved['mode']
    if mode['mode'] != 'simulation' or mode['operation_mode'] != 'ai':
        raise ValueError('시뮬·AI에서만 비교할 수 있습니다.')
    if not mode.get('order_traffic', {}).get('enabled') or mode['order_traffic']['route_source'] != 'EXISTING_SHORTEST_PATH':
        raise ValueError('일반 주문 교통조정과 A* 경로 설정이 필요합니다.')
    if saved['scenario']['state'] in ('running', 'stopping'):
        raise ValueError('다른 시나리오가 실행 중입니다.')
    if any(o['status'] in ('PENDING', 'IN_PROGRESS') or o.get('phase') == 'RETURNING'
           or o.get('recovery_status') in ('PENDING', 'RETURNING', 'BLOCKED', 'STOPPED')
           for o in saved['orders']['orders']):
        raise ValueError('진행 주문·복귀·복구를 먼저 정리하세요.')
    if any(r['status'] != 'IDLE' or r.get('route') or r.get('goal_node') or r.get('occupied_edge')
           or r.get('stop_requested') for r in saved['robots']):
        raise ValueError('대기 상태가 아닌 로봇이 있습니다.')
    if any(w['status'] != 'FREE' for w in saved['orders']['workstations'].values()):
        raise ValueError('작업대가 비어 있지 않습니다.')


def make_schedule(seed, count, interval):
    from backend.simulation.order_stream_scenario import random_items
    rng = random.Random(seed)
    return [dict(at_seconds=0.0 if i < 3 else (i-2)*interval, items=random_items(rng))
            for i in range(count)]


def schedule_hash(schedule):
    return hashlib.sha256(json.dumps(schedule, sort_keys=True).encode()).hexdigest()


def restart_backend(policy, output, baseline_robots=None):
    """로컬 프로세스·API를 확인한 뒤 비어 있는 백엔드만 교체한다."""
    if policy not in ('KEV', 'MIN_EXPECTED_TIME'):
        raise ValueError('잘못된 선택 방식')
    saved = snapshot(); ensure_idle(saved)
    pidfile = ROOT/'logs/backend.pid'
    pid = int(pidfile.read_text().strip())
    command_line = Path(f'/proc/{pid}/cmdline').read_bytes()
    if b'backend.app.main:app' not in command_line:
        raise ValueError('PID가 관제 백엔드와 일치하지 않습니다.')
    python = os.fsdecode(command_line.split(b'\0')[0])
    if not Path(python).is_absolute() or not Path(python).is_file():
        raise ValueError('실행 중인 백엔드의 Python 경로를 확인할 수 없습니다.')
    env = dict(item.decode().split('=', 1) for item in Path(f'/proc/{pid}/environ').read_bytes().split(b'\0') if item)
    if env.get('FMS_ORDER_TRAFFIC_ENABLED') != '1' or env.get('FMS_ORDER_ROUTE_SOURCE') != 'EXISTING_SHORTEST_PATH':
        raise ValueError('백엔드 실행 환경이 시험 조건과 다릅니다.')
    env['FMS_TRAFFIC_SELECTION_POLICY'] = policy
    output.mkdir(parents=True, exist_ok=True)
    state_path = output/'state.json'
    state_path.write_text(json.dumps(saved, ensure_ascii=False, indent=2)+'\n')
    bootstrap = '''import sys,json
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from backend.app.main import app
from backend.app.services.order_manager import order_manager
from backend.app.services.mock_data import mock_fms
from backend.app.services.mode_service import mode_manager
from backend.app.services.scenario_service import scenario_manager
saved=json.loads(Path(sys.argv[2]).read_text())
mode_manager.set_mode(saved['mode']['mode'],saved['mode']['operation_mode'])
order_manager.orders.update({o['order_id']:dict(o,route_started=False) for o in saved['orders']['orders']})
with mock_fms._lock:
 if mock_fms._traffic._reservations.snapshot(): raise RuntimeError('Unexpected startup reservation')
 for robot in saved['robots']:
  mock_fms._robots[robot['robot_id']].update({k:v for k,v in robot.items() if k in mock_fms._robots[robot['robot_id']]})
with scenario_manager.lock: scenario_manager._status=saved['scenario']
import uvicorn
uvicorn.run('backend.app.main:app',host='0.0.0.0',port=8000)
'''
    (output/'bootstrap.py').write_text(bootstrap)
    latest = snapshot(); ensure_idle(latest)
    if latest['orders'] != saved['orders'] or latest['mode'] != saved['mode']:
        raise ValueError('백업 중 주문·설정이 바뀌어 재시작을 중단했습니다.')
    if starting_positions(latest['robots']) != starting_positions(saved['robots']):
        raise ValueError('백업 중 로봇 위치가 바뀌어 재시작을 중단했습니다.')
    restore_path=state_path
    if baseline_robots is not None:
        # 시험 시작 자세만 맞춘다. 현재 이력과 원래 위치는 state.json에 보관한다.
        restore_state=json.loads(json.dumps(saved))
        baseline={r['robot_id']:r for r in baseline_robots}
        for robot in restore_state['robots']:
            for key in ('x','y','yaw','battery','current_node','occupied_node'):
                robot[key]=baseline[robot['robot_id']][key]
        restore_path=output/'trial-start-state.json'
        restore_path.write_text(json.dumps(restore_state,ensure_ascii=False,indent=2)+'\n')
    os.kill(pid, signal.SIGTERM)
    for _ in range(100):
        try:
            if Path(f'/proc/{pid}/stat').read_text().split()[2] == 'Z': break
        except FileNotFoundError: break
        time.sleep(.2)
    else: raise RuntimeError('백엔드 종료 대기 초과. 두 번째 서버를 실행하지 않습니다.')
    with (ROOT/'logs/backend.log').open('ab', buffering=0) as log:
        child = subprocess.Popen([python, '-c', bootstrap,
                                  str(ROOT), str(restore_path)], cwd=ROOT, env=env,
                                 stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    pidfile.write_text(str(child.pid)+'\n')
    for _ in range(120):
        if child.poll() is not None: raise RuntimeError('백엔드 시작 실패: logs/backend.log 확인')
        try:
            after = snapshot(); ensure_idle(after)
            if after['orders']['orders'] != saved['orders']['orders']:
                raise ValueError('주문 이력 복원 불일치')
            if after['mode']['order_traffic'].get('selection_policy') != policy:
                raise ValueError('선택 방식 반영 불일치')
            expected=json.loads(restore_path.read_text())
            if [(r['robot_id'],r['occupied_node'],r['x'],r['y'],r['yaw']) for r in after['robots']] != [
                    (r['robot_id'],r['occupied_node'],r['x'],r['y'],r['yaw']) for r in expected['robots']]:
                raise ValueError('로봇 위치 복원 불일치')
            break
        except OSError: time.sleep(.25)
    else: raise RuntimeError('백엔드 시작 대기 초과')
    print('BACKEND', policy, 'history', len(saved['orders']['orders']), 'pid', child.pid, flush=True)
    return after


def starting_positions(robots):
    return [{k:r[k] for k in ('robot_id','occupied_node','x','y','yaw','battery','status')}
            for r in sorted(robots,key=lambda r:r['robot_id'])]


def summarize(report):
    orders = report['orders']; submitted = report['submitted']; completed = report['completed_at']
    all_complete = (len(orders) == len(report['schedule'])
                    and all(o['status'] == 'COMPLETED' and o['order_id'] in completed for o in orders))
    latency = [completed[o['order_id']]-submitted[o['order_id']]['ack_seconds'] for o in orders
               if o['order_id'] in completed]
    events = [e for o in orders for e in o.get('traffic_coordination_history', []) if e['state']=='APPLIED']
    comparisons = [e for e in events if len(e.get('candidates', {}))>1]
    hits = sum(math.isclose(e['candidates'][e['choice']]['estimated_metrics']['new_request_completion_seconds'],
                min(c['estimated_metrics']['new_request_completion_seconds'] for c in e['candidates'].values()),
                rel_tol=0,abs_tol=1e-9) for e in comparisons)
    items = sum(o['total_quantity'] for o in orders)
    dispatch_wait = [report['dispatched_at'][oid]-spec['ack_seconds']
                     for oid,spec in submitted.items() if oid in report['dispatched_at']]
    horizon = max(completed.values(), default=0) if all_complete else None
    traces_path=ROOT/'logs/order-traffic/selection-inferences.jsonl'
    traces=[json.loads(line) for line in traces_path.read_text().splitlines()] if traces_path.exists() else []
    traces=[t for t in traces if t['order_id'] in submitted]
    report['summary'] = dict(all_work_completed=all_complete, registered=len(orders), items=items,
        work_completion_seconds=horizon, drain_seconds=report['elapsed_seconds'],
        mean_order_seconds=sum(latency)/len(latency) if all_complete else None,
        max_order_seconds=max(latency) if all_complete else None,
        mean_dispatch_wait_seconds=sum(dispatch_wait)/len(dispatch_wait) if len(dispatch_wait)==len(report['schedule']) else None,
        max_dispatch_wait_seconds=max(dispatch_wait,default=None),
        completed_items_per_minute=60*items/horizon if horizon else None,
        max_registration_lateness_seconds=max((v['request_seconds']-v['scheduled_seconds'] for v in submitted.values()),default=0),
        multi_candidate_decisions=len(comparisons), minimum_expected_matches=hits,
        selected_by=dict(Counter(e['selected_by'] for e in events)),
        kev_response_count=sum(t['event']=='RESPONSE' for t in traces),
        kev_error_count=sum(t['event']=='ERROR' for t in traces),
        selection_seconds=sum(e.get('selection_seconds',0) for e in events),
        sampled_robot_waiting_seconds=sum(report['robot_waiting_seconds'].values()),
        failed=sum(o['status']=='FAILED' or bool(o.get('return_error')) for o in orders))
    report['selection_trace']=traces


def run_trial(policy, schedule, output, timeout):
    from backend.app.services.occupancy import occupied_resource
    from backend.app.services.route_graph import load_route_graph
    initial=snapshot(); ensure_idle(initial)
    if initial['mode']['order_traffic'].get('selection_policy') != policy: raise ValueError('선택 설정 불일치')
    if any(r['occupied_node'] != {'robot1':'0','robot2':'1','robot3':'2'}.get(r['robot_id']) for r in initial['robots']):
        raise ValueError('로봇이 기준 시작 노드에 있지 않습니다.')
    graph=load_route_graph()
    if request('api/route/graph') != graph:
        raise ValueError('관제와 관측기에 설정된 지도가 다릅니다.')
    output.mkdir(parents=True,exist_ok=True)
    (output/'initial.json').write_text(json.dumps(initial,ensure_ascii=False,indent=2)+'\n')
    prefix='CMP-'+('KEV' if policy=='KEV' else 'CALC')+'-'+uuid4().hex[:6]
    known={o['order_id'] for o in initial['orders']['orders']}
    report=dict(state='running',policy=policy,schedule=schedule,schedule_sha256=schedule_hash(schedule),
        poll_interval_seconds=.15,
        initial_positions=starting_positions(initial['robots']), initial_mode=initial['mode'],
        initial_history_count=len(known),map_sha256=hashlib.sha256(json.dumps(graph,sort_keys=True).encode()).hexdigest(),
        submitted={},completed_at={},dispatched_at={},phase_events=[],robot_waiting_seconds={},
        problems=[],occupancy_observations=0,orders=[])
    started=time.monotonic(); last=0.0; index=0; previous_phases={}; previous_waiting=[]; prior_counts=None
    print('START',policy,str(output),flush=True)
    try:
        with (output/'samples.jsonl').open('a') as samples:
            while True:
                current=snapshot(); elapsed=time.monotonic()-started
                if current['mode'] != initial['mode'] or current['scenario']['state'] in ('running','stopping'):
                    raise ValueError('운영 설정 또는 다른 시나리오 변경. 추가 공급 중지')
                unexpected={o['order_id'] for o in current['orders']['orders']}-known-set(report['submitted'])
                if unexpected: raise ValueError('별도 주문이 등록돼 조건이 달라짐. 추가 공급 중지')
                while index < len(schedule) and time.monotonic()-started >= schedule[index]['at_seconds']:
                    spec=schedule[index]; at=time.monotonic()-started
                    row=request('api/orders',dict(order_name=f'{prefix}-{index+1:02d}',items=spec['items'],
                        total_quantity=sum(spec['items'].values()),operation_mode='ai',selector='kev',model='kev-08b'))
                    report['submitted'][row['order_id']]=dict(index=index+1,scheduled_seconds=spec['at_seconds'],
                        request_seconds=at,ack_seconds=time.monotonic()-started)
                    index+=1
                owned=set(report['submitted'])
                rows=[o for o in current['orders']['orders'] if o['order_id'] in owned]
                report['orders']=rows
                for o in rows:
                    oid=o['order_id']
                    if o['status'] != 'PENDING':report['dispatched_at'].setdefault(oid,elapsed)
                    if o['status']=='COMPLETED':report['completed_at'].setdefault(oid,elapsed)
                    signature=(o['status'],o['phase'],o['robot_id'])
                    if previous_phases.get(oid)!=signature:
                        report['phase_events'].append(dict(seconds=elapsed,order_id=oid,status=o['status'],phase=o['phase'],robot_id=o['robot_id']))
                        previous_phases[oid]=signature
                for rid in previous_waiting:
                    report['robot_waiting_seconds'][rid]=report['robot_waiting_seconds'].get(rid,0)+elapsed-last
                previous_waiting=[r['robot_id'] for r in current['robots'] if r['status']=='WAITING']; last=elapsed
                resources=[occupied_resource(r,graph) for r in current['robots']]
                report['occupancy_observations']+=1
                if len(resources)!=len(set(resources)):raise ValueError('중복 점유 관측')
                samples.write(json.dumps(dict(seconds=elapsed,robots=current['robots'],orders=rows),ensure_ascii=False)+'\n');samples.flush()
                counts=dict(Counter(o['status'] for o in rows))
                if counts!=prior_counts:
                    print('PROGRESS',policy,round(elapsed,2),index,counts,flush=True);prior_counts=counts
                if any(o['status'] in ('FAILED','CANCELLED') or o.get('return_error') for o in rows):
                    raise ValueError('주문 실패·취소·복귀 오류. 접수 주문은 유지')
                if len(rows)==len(schedule) and all(o['status']==o['phase']=='COMPLETED' for o in rows) and all(
                        r['status']=='IDLE' and not r.get('route') and not r.get('goal_node') for r in current['robots']):
                    report['state']='completed';break
                if elapsed>=timeout:raise TimeoutError('시험 시간 한도. 접수한 주문은 계속 처리')
                (output/'progress.json').write_text(json.dumps(dict(state=report['state'],registered=index,counts=counts,seconds=elapsed),ensure_ascii=False)+'\n')
                time.sleep(.15)
    except (Exception,KeyboardInterrupt) as exc:
        report.update(state='interrupted' if isinstance(exc,KeyboardInterrupt) else 'failed')
        report['problems'].append(str(exc))
    finally:
        report['elapsed_seconds']=time.monotonic()-started
        current=snapshot()
        report['orders']=[o for o in current['orders']['orders'] if o['order_id'] in report['submitted']]
        report['final_robots']=current['robots']
        summarize(report)
        (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
        print('RESULT',policy,report['state'],json.dumps(report['summary'],ensure_ascii=False),flush=True)
    return report


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run',action='store_true')
    p.add_argument('--seeds',type=int,nargs='+',default=[42,17])
    p.add_argument('--count',type=int,default=8)
    p.add_argument('--interval',type=float,default=5)
    p.add_argument('--timeout',type=float,default=300)
    args=p.parse_args()
    if not 3<=args.count<=30 or not math.isfinite(args.interval) or args.interval<1 or not math.isfinite(args.timeout) or args.timeout<60:
        p.error('count 3~30, interval 1초 이상, timeout 60초 이상')
    plans=[dict(seed=seed,schedule=make_schedule(seed,args.count,args.interval)) for seed in args.seeds]
    if not args.run:
        print(json.dumps(plans,ensure_ascii=False,indent=2));return 0
    output=ROOT/'logs/order-traffic-comparisons'/datetime.now(ZoneInfo('Asia/Seoul')).strftime('%Y%m%d-%H%M%S')
    output.mkdir(parents=True)
    (output/'schedules.json').write_text(json.dumps(plans,ensure_ascii=False,indent=2)+'\n')
    original=snapshot();ensure_idle(original)
    original_policy=original['mode']['order_traffic'].get('selection_policy','KEV')
    results=[]; error=None
    try:
        for pair,plan in enumerate(plans):
            policies=['KEV','MIN_EXPECTED_TIME'] if pair%2==0 else ['MIN_EXPECTED_TIME','KEV']
            paired=[]
            for policy in policies:
                folder=output/f"seed-{plan['seed']}-{pair}-{policy}"
                restart_backend(policy,folder/'restart',original['robots'])
                result=run_trial(policy,plan['schedule'],folder,args.timeout)
                results.append(dict(seed=plan['seed'],policy=policy,path=str(folder),summary=result['summary'],state=result['state']))
                if result['state']!='completed':raise RuntimeError('미완료 시험. 뒤 시험을 실행하지 않습니다.')
                paired.append(result)
            if paired[0]['initial_positions']!=paired[1]['initial_positions'] or paired[0]['map_sha256']!=paired[1]['map_sha256']:
                raise RuntimeError('시작 위치·지도 비교 불일치')
            if any(r['summary']['max_registration_lateness_seconds']>.5 for r in paired):
                raise RuntimeError('등록 일정에서 0.5초 이상 지연. 비교 조건 검토 필요')
    except (Exception,KeyboardInterrupt) as exc:
        error=str(exc); print('ISSUE',error,flush=True)
    finally:
        restored=False
        try:
            current=snapshot();ensure_idle(current)
            if current['mode']['order_traffic'].get('selection_policy')!=original_policy:
                restart_backend(original_policy,output/'restore-original-policy')
            restored=True
        except Exception as exc:print('RESTORE_DEFERRED',str(exc),flush=True)
        final=dict(state='completed' if error is None else 'failed',error=error,results=results,
                   original_policy=original_policy,original_policy_restored=restored,
                   note='주문표·등록 일정은 같지만 주행 상태와 이후 후보는 선택에 따라 달라질 수 있음. 표본 관측 기반 시간.')
        (output/'comparison.json').write_text(json.dumps(final,ensure_ascii=False,indent=2)+'\n')
        print('COMPARISON',str(output/'comparison.json'),final['state'],flush=True)
    return 0 if error is None else 1


if __name__=='__main__':
    raise SystemExit(main())
