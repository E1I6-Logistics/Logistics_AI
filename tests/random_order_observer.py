"""기존 관제의 무작위 주문 공급을 제한된 건수만 실행하고 관측 기록을 남긴다.

기본은 설정 출력. --run일 때만 시뮬·AI의 기존 주문 공급 시나리오를 시작한다.
실패하면 새 공급을 중지하고 현재 상태를 보존한다. 접수 주문을 취소하지 않는다.
"""

import argparse
from collections import Counter
from datetime import datetime
import json
from pathlib import Path
import time
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--target", type=int, default=20)
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--host", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    if not 3 <= args.target <= 100 or not 10 <= args.timeout <= 3600:
        parser.error("target은 3~100, timeout은 10~3600초여야 합니다.")
    if not args.run:
        print(json.dumps(dict(target=args.target, timeout=args.timeout, scenario="continuous_ai_orders",
                              note="--run을 붙여야 관제에 주문을 등록합니다."), ensure_ascii=False))
        return 0

    def request(endpoint, payload=None):
        req = Request(args.host.rstrip('/')+'/api/'+endpoint,
                      data=None if payload is None else json.dumps(payload).encode(),
                      headers={"Content-Type": "application/json"})
        with urlopen(req, timeout=5) as response:
            return json.load(response)

    mode, initial, robots, scenario = [request(ep) for ep in ['mode', 'orders', 'robots', 'scenarios/status']]
    if mode['mode'] != 'simulation' or mode['operation_mode'] != 'ai' or not mode.get('order_traffic', {}).get('enabled'):
        raise ValueError('시뮬·AI와 일반 주문 교통조정이 켜져 있어야 합니다.')
    if any(o['status'] in ['PENDING', 'IN_PROGRESS'] or o.get('phase') == 'RETURNING'
           or o.get('recovery_status') in ['PENDING', 'RETURNING'] for o in initial['orders']):
        raise ValueError('기존 주문·복귀를 먼저 끝내세요. 기존 주문은 변경하지 않습니다.')
    if any(r.get('route') is not None or r.get('goal_node') is not None for r in robots) or scenario.get('state') in ['running', 'stopping']:
        raise ValueError('주행 또는 시나리오가 실행 중입니다.')
    root = Path(__file__).resolve().parents[1]
    output = root/'logs/random-order-runs'/datetime.now(ZoneInfo('Asia/Seoul')).strftime('%Y%m%d-%H%M%S')
    output.mkdir(parents=True)
    graph = request('route/graph')
    # 실제 지도 응답 형식과 API 원본을 기록한다. 물리 통로 판정은 아래에서 공유 함수를 사용한다.
    import sys
    sys.path.insert(0, str(root))
    from backend.app.services.occupancy import occupied_resource
    from backend.app.services.route_graph import load_route_graph
    graph_for_occupancy = load_route_graph()
    report = dict(state='starting', target=args.target, timeout_seconds=args.timeout, mode=mode,
                  scenario='continuous_ai_orders', initial_order_count=len(initial['orders']),
                  output=str(output), problems=[], phase_events=[], progress=[])
    (output/'initial.json').write_text(json.dumps(dict(mode=mode, orders=initial, robots=robots, graph=graph), ensure_ascii=False, indent=2)+'\n')
    started = time.monotonic()
    owned, run_id, supplied_stopped = set(), None, False
    previous_phases, last_positions, last_progress = {}, None, started
    first_seen, completed_at, settled_at, last_rows = {}, {}, {}, []
    max_pending = max_active = checks = 0
    prior_counts = None

    def stop_owned_supply():
        nonlocal supplied_stopped
        status = request('scenarios/status')
        if run_id and status.get('run_id') == run_id and status.get('state') == 'running':
            request('scenarios/stop', {})
        supplied_stopped = True

    try:
        scenario = request('scenarios/run', dict(name='continuous_ai_orders'))
        run_id = scenario['run_id']
        report.update(state='running', run_id=run_id, settings=scenario['order_stream'])
        print('START', str(output), json.dumps(scenario['order_stream'], ensure_ascii=False), flush=True)
        with (output/'samples.jsonl').open('a') as samples:
            while time.monotonic()-started < args.timeout:
                elapsed = time.monotonic()-started
                current_mode = request('mode')
                if current_mode['mode'] != 'simulation' or current_mode['operation_mode'] != 'ai':
                    raise ValueError('운용 모드 변경')
                scenario, data, robots = [request(ep) for ep in ['scenarios/status', 'orders', 'robots']]
                if scenario.get('run_id') != run_id:
                    raise ValueError('공급 시나리오가 다른 실행으로 바뀌었습니다.')
                owned.update(scenario['order_stream']['order_ids'])
                rows = [o for o in data['orders'] if o['order_id'] in owned]
                last_rows = rows
                if len(rows) != len(owned):
                    raise ValueError('등록 주문 누락·서버 초기화')
                counts = dict(Counter(o['status'] for o in rows))
                max_pending = max(max_pending, counts.get('PENDING', 0))
                max_active = max(max_active, counts.get('IN_PROGRESS', 0))
                for o in rows:
                    oid = o['order_id']; first_seen.setdefault(oid, elapsed)
                    if o['status'] == 'COMPLETED': completed_at.setdefault(oid, elapsed)
                    # COMPLETED 단계에는 복귀 완료뿐 아니라 다음 주문으로의 전환도 포함된다.
                    if o['status'] == o['phase'] == 'COMPLETED': settled_at.setdefault(oid, elapsed)
                    signature = (o['status'], o['phase'], o['robot_id'])
                    if previous_phases.get(oid) != signature:
                        report['phase_events'].append(dict(seconds=round(elapsed, 2), order_name=o['order_name'],
                            order_id=oid, status=o['status'], phase=o['phase'], robot_id=o['robot_id']))
                        previous_phases[oid] = signature
                resources = [occupied_resource(r, graph_for_occupancy) for r in robots]
                checks += 1
                if len(resources) != len(set(resources)):
                    raise ValueError('노드·물리 엣지 중복 점유를 관측했습니다.')
                positions = [(r['robot_id'], round(r['x'], 3), round(r['y'], 3), r['status']) for r in robots]
                progress_key = (positions, sorted(previous_phases.items()))
                if progress_key != last_positions:
                    last_progress = time.monotonic(); last_positions = progress_key
                samples.write(json.dumps(dict(seconds=round(elapsed, 2), robots=robots,
                    orders=[{k:o.get(k) for k in ['order_id', 'order_name', 'status', 'phase', 'robot_id', 'pickup_index',
                            'workstation_node', 'error', 'recovery_status', 'traffic_coordination']} for o in rows]),
                    ensure_ascii=False)+'\n')
                samples.flush()
                if counts != prior_counts or (not report['progress'] or elapsed-report['progress'][-1]['seconds'] >= 30):
                    status = dict(seconds=round(elapsed, 2), generated=len(owned), counts=counts)
                    report['progress'].append(status); print('PROGRESS', json.dumps(status, ensure_ascii=False), flush=True)
                    prior_counts = counts
                if any(o['status'] in ['FAILED', 'CANCELLED'] or o.get('return_error') for o in rows):
                    raise ValueError('주문 실패·취소·복귀 오류')
                if scenario.get('state') == 'failed':
                    raise ValueError(scenario.get('message', '공급 오류'))
                if len(owned) >= args.target and not supplied_stopped:
                    stop_owned_supply(); print('SUPPLY_STOPPED', len(owned), flush=True)
                if rows and len(owned) >= args.target and all(o['status'] == o['phase'] == 'COMPLETED' for o in rows):
                    report['state'] = 'completed'; break
                if rows and time.monotonic()-last_progress > 45:
                    raise ValueError('45초 동안 위치·주문 단계 변화가 없음. 교착 여부는 별도 분석 필요')
                (output/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
                time.sleep(.5)
            else:
                raise ValueError('시험 시간 한도 초과. 미완료 주문은 계속 처리됩니다.')
    except (Exception, KeyboardInterrupt) as exc:
        report.update(state='interrupted' if isinstance(exc, KeyboardInterrupt) else 'failed')
        report['problems'].append(dict(seconds=round(time.monotonic()-started, 2), reason=str(exc)))
        print('ISSUE', str(exc), flush=True)
    finally:
        try:
            stop_owned_supply()
            report['final_scenario'] = request('scenarios/status')
            last_rows = [o for o in request('orders')['orders'] if o['order_id'] in owned]
            report['final_robots'] = request('robots')
        except Exception as exc:
            report['problems'].append(dict(reason='마지막 상태 수집 실패: '+str(exc)))
        report.update(elapsed_seconds=time.monotonic()-started, orders=last_rows,
                      generated=len(owned), max_pending=max_pending, max_in_progress=max_active,
                      occupancy_observations=checks, first_seen=first_seen,
                      completed_at=completed_at, phase_settled_at=settled_at)
        (output/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
        print('RESULT', report['state'], 'generated', len(owned), 'report', output/'report.json', flush=True)
    return 0 if report['state'] == 'completed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
