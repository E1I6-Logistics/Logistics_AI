# kev_mapf_standalone 함수 설명·사용법·테스트

대상: `/home/jhlee/Logistics_AI/backend/app/services/kev_mapf_standalone.py`  
테스트: `/home/jhlee/Logistics_AI/tests/test_kev_mapf_integration.py`

## 1. 현재 구현 범위와 연결 흐름

기존 AI가 생성한 `result['path']`를 입력받아 여러 로봇의 공유 구간과 우선순위, WAIT/REROUTE/HOLD를 제안한다. 이미 제공된 경로는 다시 추론하지 않는다. 실제 주행 명령과 교통 예약은 이 모듈이 수행하지 않는다.

```text
order_route_dispatcher.dispatch_order_route()
  → preview_llm_route(graph, current_node, node, selector, model)
  → required_path = result['path']
  → [연결 예정] plan_mapf_from_route_results(...)
       → Graph: 기존 route_service로 그래프/경로 검증
       → parse_robot_state(): 제공된 경로 복사·정규화
       → KevMAPF.resolve(): 공유 구간/우선순위/행동 제안
       → 우회 필요 시 LiveKEV.route() → preview_llm_route()
       → paths(int), required_paths(str), decisions 반환
  → [미구현] WAIT/HOLD·우선순위 실행 정책과 최신 상태 재검증
  → 기존 navigate_to_node(required_path=...)
       시뮬레이션: mock_fms → TrafficManager → DistanceAStar.plan_timed → 예약·주행
       실제 로봇: ros_gateway → RealNavigation → TrafficManager
                  → 경유점 전송 → Nav2 NavigateThroughPoses
```

`order_route_dispatcher.py`는 수정하지 않았고, 위 MAPF 호출도 아직 추가하지 않았다. 다른 로봇 상태 자동 수집도 아직 없다. 배차·작업대 선택·기존 예약 알고리즘은 이 함수에서 변경하지 않는다.

## 2. 주로 사용할 연결 함수

### plan_mapf_from_route_results(graph, route_requests, *, planner=None, config=None)

**역할:** 이미 만들어진 주문 경로 결과들을 받아 MAPF 제안을 반환하는 동기 함수.

| 인자 | 형식 | 설명 |
|---|---|---|
| `graph` | GeoJSON dict 또는 `Graph` | 전체 유향 그래프. 유효 간선에 저장 `weight` 필요 |
| `route_requests` | `list[dict]` | 아래 주문/현재 노드/목적지/경로 결과 목록. 최소 한 로봇 필요 |
| `planner` | `LiveKEV`, `MockKEV`, 테스트 대역 또는 None | None이면 실제 Kev 판단 클라이언트를 생성한다. 오프라인에서는 명시적으로 Mock을 전달 |
| `config` | dict 또는 None | 대기 가능 노드·점유·제외 노드·반복 제한·MAPF 모델 설정 |

각 요청의 필드:

| 필드 | 필수 | 설명 |
|---|---|---|
| `order.robot_id` | 예 | 로봇 ID. 같은 목록에서 중복 불가 |
| `order.selector` | 예 | 그 로봇의 새 경로/우회 생성에 사용할 기존 selector |
| `order.model` | 예 | 그 로봇의 경로 모델. 빈 값 불가 |
| `current_node` | 예 | 확인된 현재 노드, int 또는 정수 문자열 |
| `node` | 예 | 이번 단계의 목적지 |
| `result.path` | 예 | 기존 preview 결과의 노드 순서. 비어 있으면 오류 |
| `status` | 아니오 | 상태 문자열. 기본 `IDLE` |
| `task_priority` | 아니오 | 모델에 제공할 우선순위 정보. 기본 0, int 변환. 높고 낮음의 운영 의미는 호출자가 정의 |

`result`의 다른 메타데이터는 변경하지 않는다. MAPF 입력 변환에서는 경로만 사용한다. `task_priority` 자체가 우선순위를 강제로 결정하는 것은 아니며 Kev 판단 입력으로 전달된다.

설정:

| config 키 | 기본값 | 의미 |
|---|---|---|
| `mapf_model` | `kev-08b` | planner=None일 때 MAPF 판단용 Kev 모델. 주문 경로 모델과 별개 |
| `wait_allowed_nodes` | 미지정 | 물리적으로 대기 가능한 노드 목록. 명시한 빈 목록 `[]`이면 WAIT 후보 없음 |
| `zone_occupancy` | `{}` | 공유 자원 ID → 실제 점유 로봇 ID. 예: `{'node:3': 'b', 'edge:2:3': 'c'}` |
| `blocked_nodes` | `[]` | 제외 노드. 최종 경로에 남아 있으면 BLOCKED 처리 |
| `max_rounds` | 16 | 판단 라운드 상한. 1 이상 |
| `max_reroutes` | 3 | 전체 우회 시도 상한. 0 이상 |

`wait_allowed_nodes` 미지정 시 지도에서 `wait_allowed=True` 또는 `safe_wait=True`를 사용한다. 둘 다 없으면 검증되지 않은 대기 위치를 제안할 수 있어 `REVIEW_REQUIRED`와 경고가 발생할 수 있다. 간선 자원 키는 두 노드 ID를 숫자 오름차순으로 정렬한 `edge:a:b` 형식이다.

반환값:

| 키 | 설명 |
|---|---|
| `paths` | 로봇 ID → `list[int]`. 우회 성공 시 제안 경로 변경 가능 |
| `required_paths` | 로봇 ID → `list[str]`. 기존 FMS의 required_path 형식으로 변환한 후보 |
| `path_sources` | `preview_result`, 우회 시 `kev_rerouted` 또는 mock 표기 |
| `decisions` | 공유 구간별 우선 로봇·양보 로봇·행동·대기 노드 또는 새 경로/실패 이유 |
| `potential_shared_zones` | 최종 경로들의 공유 자원. 실제 동시 충돌을 의미하지 않음 |
| `global_priority_relations` | `[high, low]` 우선순위 관계 목록 |
| `status` | `PROPOSED`, `REVIEW_REQUIRED`, `BLOCKED` |
| `time_prediction_used` | 항상 false. ETA 기반 동시 주행 스케줄 아님 |
| `fms_execution_approved` | 항상 false. 주행 허가 아님 |
| `metrics`, `warnings`, `safety` | 호출·시간·우회 지표, 경고, 검증 범위 |

`PROPOSED`여도 자동 실행하지 않는다. WAIT는 실제 정차와 구간 해제 이벤트 처리가 필요하고, HOLD는 보류이며, REROUTE도 기존 예약기의 승인이 필요하다. 반복 상한에 도달하면 보수적으로 BLOCKED가 될 수 있다. 충돌이 없어도 상태 수집이 누락되었다면 운영 안전성을 보장하지 않는다.

### preview_order_route_for_mapf(graph, order, current_node, node)

**역할:** 결과가 아직 없는 경우 기존 dispatcher와 같은 인자로 `preview_llm_route()`를 호출하고 결과 dict를 반환한다.

```python
from backend.app.services.kev_mapf_standalone import preview_order_route_for_mapf

result = preview_order_route_for_mapf(graph, order, current_node, node)
```

실제 모델을 호출할 수 있다. 기존 `result`가 이미 있으면 이 함수를 다시 호출하지 않는다. 카탈로그·모델 설정·검증 오류는 기존 preview의 예외로 전달된다.

### mapf_robot_from_route_result(robot_id, current_node, goal_node, result, *, status='IDLE', task_priority=0)

**역할:** 한 로봇의 기존 `result.path`를 `id/current_node/goal_node/path/status/task_priority` dict로 복사·변환한다. 모델을 호출하지 않는다.

```python
robot = mapf_robot_from_route_result('a', '1', '4', {'path': ['1', '2', '3', '4']})
# robot['path'] == [1, 2, 3, 4]
```

이 함수만으로 그래프·시작점·목적지·유향 간선 검증이 완료되지는 않는다. 검증은 이후 `parse_robot_state()`와 `Graph.check_path()`가 수행한다.

## 3. 완결된 오프라인 사용 예

아래 예는 실제 AI 서버·로봇 없이 실행한다. 지도·경로는 내장 demo를 사용하며 판단은 Mock이다.

```python
from backend.app.services.kev_mapf_standalone import (
    MockKEV, demo_data, plan_mapf_from_route_results,
)

graph, jobs = demo_data()
route_requests = [
    {
        'order': {'robot_id': raw['id'], 'selector': 'kev', 'model': 'kev-08b'},
        'current_node': str(raw['current_node']),
        'node': str(raw['goal_node']),
        'result': {'path': [str(n) for n in raw['path']]},
        'task_priority': raw['task_priority'],
    }
    for raw in jobs['robots']
]
plan = plan_mapf_from_route_results(
    graph, route_requests, planner=MockKEV(),
    config={'wait_allowed_nodes': [2, 6], 'max_reroutes': 0},
)
print(plan['required_paths'])
print(plan['decisions'])
assert plan['fms_execution_approved'] is False
```

아래는 기존 주문 흐름에 추가할 수 있는 호출 예시이며 아직 dispatcher에 적용하지 않았다.

```python
# dispatch_order_route() 내부의 연결 예정 예시. 현재 코드에는 미적용.
# result는 이미 생성된 preview_llm_route 결과다.
route_requests = [
    {'order': order, 'current_node': current_node, 'node': node, 'result': result},
    *other_route_requests,  # 호출자가 같은 시점의 남은 경로·상태로 구성
]
mapf_plan = await asyncio.to_thread(
    plan_mapf_from_route_results, graph, route_requests,
    config={'wait_allowed_nodes': verified_wait_nodes,
            'zone_occupancy': fresh_zone_occupancy},
)
# 계획 완료 후 기존 모드/지도/주문/출발 위치 검사를 다시 수행한다.
# WAIT/HOLD 등을 실행 정책으로 처리하고 허가된 경우에만
# mapf_plan['required_paths'][order['robot_id']]를 required_path로 전달한다.
```

## 4. Graph의 모든 메서드

`Graph`는 저장 weight를 사용하는 기존 `route_service`의 얇은 래퍼다. 최단 경로를 독자적으로 계산하지 않는다.

| 함수 | 입력·반환 및 역할 | 사용 예 |
|---|---|---|
| `Graph.__init__(data)` | GeoJSON FeatureCollection → Graph. `nodes`: 좌표, `edge_records`: 유향 간선 목록, `edges`: 병렬 간선 최솟값 lookup, `compact`: 전체 adjacency. Point가 없거나 weight가 잘못되면 오류 | `g = Graph(graph_data)` |
| `Graph.load(path)` | UTF-8 JSON 파일을 읽어 Graph 생성. classmethod | `g = Graph.load(Path('/home/jhlee/Logistics_AI/routes/test_int.geojson'))` |
| `Graph.check_path(path, *, start=None, goal=None)` | 노드·유향 간선·끝점·간선 재사용·노드 방문 상한 검증. 성공 시 None, 실패 시 ValueError. start/goal 생략 시 경로의 양 끝을 기준으로 검사 | `g.check_path([1, 2, 3, 4], start=1, goal=4)` |
| `Graph.virtual_graph(forbidden_nodes, forbidden_edges)` | 제외 노드와 그 연결 간선, 지정 유향 간선을 제거한 임시 GeoJSON dict 반환 | `filtered = g.virtual_graph({3}, {(1, 2)})` |
| `Graph.declared_wait_nodes()` | Point properties의 `wait_allowed=True` 또는 `safe_wait=True` 노드 집합 반환 | `allowed = g.declared_wait_nodes()` |

`virtual_graph()`의 GeoJSON 컨테이너는 새로 만들지만 남은 feature 객체는 원본과 공유한다. 반환 feature를 직접 수정하는 용도로 사용하지 않는다. 제외 간선은 방향별 `(a, b)`이며 양방향 제거에는 두 방향을 전달한다. Graph 생성은 간선이 없어도 Point 노드가 있으면 가능하다.

## 5. LiveKEV와 MockKEV의 모든 메서드

| 함수 | 설명·반환 | 사용법 |
|---|---|---|
| `LiveKEV.__init__(repo, *, model, deadline, max_steps)` | 기존 KevSelector와 preview를 준비하고 호출 지표 초기화. 생성만으로 모델 추론을 수행하지 않음 | `live = LiveKEV(Path('/home/jhlee/Logistics_AI'), model='kev-08b', deadline=0.15, max_steps=None)` |
| `LiveKEV.pick(state, candidates, instructions, question_id)` | 후보 1개면 강제 선택, 2개 이상이면 Kev에 판단 요청. 선택 ID 문자열 반환. 빈 후보/잘못된 반환 ID는 오류 | 엔진에서 `live.pick(state, {'p0': 'a 우선', 'p1': 'b 우선'}, instructions, 'priority_0')` |
| `LiveKEV.route(graph_data, start, goal, *, selector='kev', model=None)` | 기존 preview로 새 완성 경로 생성·검증 후 `list[int]` 반환. model=None이면 생성자의 모델. start==goal도 preview 계약 사용 | `path = live.route(graph, 1, 4, selector='kev', model='kev-08b')` |
| `MockKEV.pick(state, candidates, instructions, question_id)` | 실제 추론 없이 `action_wait`를 우선 선택, 없으면 첫 후보. mock 지표만 기록 | `mock = MockKEV()` 후 엔진에 전달 |
| `MockKEV.route(graph_data, start, goal, *, selector='kev', model=None)` | 오프라인 데모용 BFS 경로. 저장 weight는 검증하지만 가중치 최단 경로를 보장하지 않음 | `path = mock.route(graph, 1, 4)` |

`MockKEV`는 별도 생성자 없이 기본 생성자를 사용한다. `deadline/max_steps`는 LiveKEV 생성 인자의 호환성만 유지하며 실제 preview 제어에는 사용되지 않는다. 경로 생성 설정은 모델 카탈로그에서 읽는다. missing catalog 예외를 별도 runner로 우회하지 않는다. 직접 `LiveKEV.route()` 호출은 오류를 전달하고, MAPF 내부 우회 실패는 HOLD 제안으로 처리한다.

카운터 `calls/forced/model_seconds`는 클라이언트 누적 값이다. 같은 planner를 여러 계획에 재사용하면 metrics를 단일 계획의 호출 수로 해석하지 않는다. `kev_choice_calls`에는 LiveKEV가 기록한 경로 preview 호출 수도 포함될 수 있다.

## 6. 경로·상태 보조 함수

| 함수 | 설명·반환 | 예시 |
|---|---|---|
| `_node(v)` | int 또는 정수 문자열을 int로 정규화. bool/float/비정수 문자열 거부 | `_node('3') == 3` |
| `_node_sort(s)` | 숫자 문자열은 `(0, int)`, 나머지는 `(1, str)`로 정렬 키 반환. 현재 모듈에서 사용하지 않는 보조 함수 | `sorted(['10', '2', 'x'], key=_node_sort)` |
| `zone_candidates(paths)` | 두 로봇 이상이 공유하는 노드/물리 간선 후보 dict. 경로는 `list[int]`, 간선 공유 판단은 양 방향을 같은 자원으로 봄 | `zone_candidates({'a': [1, 3], 'b': [2, 3]})` → `node:3` 후보 |
| `first_zone_index(path, zone)` | 자원 첫 진입 위치 또는 None. 노드는 그 인덱스, 간선은 도착 노드 인덱스 | `first_zone_index([1, 2, 3], {'kind': 'node', 'node': 3}) == 2` |
| `would_cycle(priorities, high, low)` | high→low 관계 추가 시 우선순위 순환이 생기는지 bool 반환. 원본 관계 수정 없음 | `would_cycle({('a', 'b')}, 'b', 'a') is True` |
| `parse_robot_state(data, graph, live)` | `data['robots']`를 검증된 로봇 ID → 상태 dict로 변환. 중복 ID/끝점/노드/간선 검증. 최소 한 로봇 필요 | `robots = parse_robot_state(jobs, g, MockKEV())` |

`parse_robot_state()`는 각 로봇의 `path`, `route_result`, `result` 중 **한 가지**만 받는다. 여러 가지를 함께 주면 오류다. 셋 다 없으면 `live.route()`로 새 경로를 생성한다. 이는 CLI/직접 호출 기능이며, 공개 연결 함수 `plan_mapf_from_route_results()`는 모든 result를 필수로 받기 때문에 누락된 result를 자동 생성하지 않는다.

숫자 문자열은 canonical ID로 변환한다. 예를 들어 `'003'` → `3` → `'3'`이며 앞자리 0 표기는 보존하지 않는다. 기존 preview 결과도 정규 숫자 문자열을 사용한다. ID 정규화가 임의의 비숫자 노드 ID까지 지원하는 것은 아니다.

## 7. KevMAPF의 모든 메서드

```python
# 직접 엔진을 쓰는 경우. FMS 연결에는 plan_mapf_from_route_results() 권장.
graph, jobs = demo_data()
mock = MockKEV()
robots = parse_robot_state(jobs, graph, mock)
engine = KevMAPF(graph, robots, mock, jobs, rounds=16, reroutes=3)
proposal = engine.resolve()
```

| 함수 | 입력·반환 및 역할 |
|---|---|
| `KevMAPF.__init__(graph, robots, live, config, *, rounds=16, reroutes=3)` | 검증된 로봇 dict, 판단/경로 생성 client, 대기/점유 설정을 받아 엔진 구성. 대기 가능 노드 존재 검증, 우선순위·처리 상태 초기화 |
| `_graph_state()` | 전체 compact 유향 그래프·weight, 모든 로봇의 현재 상태·경로 및 현재 노드에서 나가는 `available_edges`, `eta_available=False` 반환 |
| `_state(zone, info, participants)` | `_graph_state()`에 선택된 공유 자원, 실제 점유자, 참여 로봇 ID, 누적 우선순위, 검증된 대기 노드를 추가한 판단 입력 반환 |
| `_pair_for(zone, info)` | 해당 zone에서 아직 처리하지 않은 두 로봇 `(a, b)` 또는 None 반환 |
| `_wait_candidates(robot, info)` | 남은 경로의 공유 자원 진입 이전에서 제외 노드/대기 정책을 적용한 중복 없는 `list[int]` 반환 |
| `_reroute(robot, zid, info)` | 공유 노드 또는 양방향 물리 간선, blocked_nodes를 제외한 임시 지도에서 그 로봇의 selector/model로 우회. 끝점·원본 유향 간선·제외 구간 재검증. `(path, None)` 또는 `(None, error)` 반환. `zid`는 현재 본문에서 사용하지 않음 |
| `resolve()` | zone→priority→action→wait node 판단 반복. 우선순위 후보에서 순환을 검사하고 실제 점유자를 우선 취급. WAIT/REROUTE/HOLD 제안과 최종 경로/지표 dict 반환 |

언더스코어 메서드는 엔진 내부용이다. 운영 연결부에서 직접 호출할 필요는 없다. `available_edges`는 그래프상 인접 목록이며 실제 예약 허가가 아니다. 공유 자원 검사도 노드/간선 공유만 대상으로 하며 지도에 표현되지 않은 기하학적 교차는 찾지 않는다.

`resolve()`는 성공한 우회 경로를 `self.robots`에 반영하고 판단 이력을 쌓는다. 따라서 직접 전달한 robots dict를 변경할 수 있고 같은 엔진의 반복 resolve를 새 계획처럼 사용하면 안 된다. 공개 연결 함수는 별도 robots dict를 만들어 원본 route_requests/result를 보존한다. 새 계획에는 새 엔진을 만든다.

## 8. 데모와 CLI 함수

| 함수 | 역할·사용법 |
|---|---|
| `demo_data()` | 작은 저장 weight 그래프와 세 로봇의 공급 경로/대기 가능 노드 설정을 `(Graph, jobs_dict)`로 반환. 실제 AI 호출 없음 |
| `main(argv=None)` | 인자를 파싱해 graph/jobs/client/엔진을 준비하고 제안 JSON 저장·출력. `PROPOSED`면 0, 그 외 계획 상태면 2. 파싱/입력/모델 오류는 예외 또는 argparse 오류로 종료 |

### 내장 demo 실행

```bash
cd /home/jhlee/Logistics_AI
mkdir -p /home/jhlee/Documents/Codex/2026-10-14/files-pasted-by-the-user-home/work
/home/jhlee/venv/robot/bin/python backend/app/services/kev_mapf_standalone.py \
  --demo \
  --output /home/jhlee/Documents/Codex/2026-10-14/files-pasted-by-the-user-home/work/mapf_demo.json
```

`mode=MOCK_DEMO_NOT_MODEL_INFERENCE`, `fms_execution_approved=false`, `time_prediction_used=false`를 확인한다. 내장 demo는 두 로봇의 공유 `node:3`에서 WAIT를 제안한다.

### 사용자 제공 경로로 mock 실행

```bash
/home/jhlee/venv/robot/bin/python backend/app/services/kev_mapf_standalone.py \
  --mock --graph /absolute/path/graph.geojson --jobs /absolute/path/jobs.json \
  --output /absolute/path/proposal.json --max-reroutes 0
```

경로는 실제 graph의 노드·유향 간선과 일치해야 한다. 다음은 jobs 형식 예시이며 임의 graph에서 유효하다는 뜻은 아니다.

```json
{
  "robots": [
    {"id": "a", "current_node": "1", "goal_node": "4",
     "route_result": {"path": ["1", "2", "3", "4"]},
     "selector": "kev", "model": "kev-08b", "status": "IDLE"},
    {"id": "b", "current_node": "5", "goal_node": "7",
     "path": ["5", "3", "7"], "selector": "ollama", "model": "selected-model"}
  ],
  "wait_allowed_nodes": [5],
  "zone_occupancy": {},
  "blocked_nodes": []
}
```

`--mock`도 누락 경로/허용된 우회는 mock BFS로 만들 수 있다. 제공 경로를 그대로 검사하려면 모든 경로를 공급하고 `--max-reroutes 0`을 쓴다. 명시적인 공개 연결 함수의 `required_paths`와 달리 CLI 엔진 출력은 `paths` 중심이며 `required_paths`를 추가하지 않는다.

### live 실행 조건

`--demo/--mock` 없이 `--repo`, `--graph`, `--jobs`, `--model`을 사용하면 실제 Kev 판단을 호출할 수 있다. 현재 문서 작업에서는 실행하지 않았다. 새 경로/우회 생성에는 기존 모델 카탈로그 파일과 활성 모델 설정이 필요하다. 실제 주행은 live 모드에서도 보내지 않는다.

`--max-rounds/--max-reroutes`는 CLI 계획 제한이다. `--deadline/--max-steps`는 deprecated 호환 옵션이며 live preview의 설정을 바꾸지 않는다. 기본 `--output`은 실행 디렉터리의 `mapf_result.json`이므로 명시적인 output 경로 사용을 권장한다.

## 9. 다른 로봇 입력 구성 시 확인할 사항

시뮬레이션은 `mock_fms.robot_snapshots(mode)`가 전체 상태의 lock 보호 복사본을 제공한다. 실제 모드는 FleetManager의 Robot에 `route` 복사본이 있지만, 전체 로봇의 경로·점유를 일관된 시점에 복사하는 공개 API 연결은 아직 없다. authoritative 예약·경로 상태는 RealNavigation/TrafficManager에도 있다.

남은 경로는 진행 인덱스와 확인된 점유를 기준으로 구성한다. 반복 노드가 있을 수 있어 단순 `path.index(current_node)`로 자르면 안 된다. 예약 대기 중 route=None이면 고정 경로가 TrafficManager의 `_required_paths`에 남아 있을 수 있으므로 경로를 임의로 새로 생성하지 않는다.

정차 로봇은 current_node==node, `result.path=[current_node]`로 표현할 수 있다. 간선 위에서 occupied_node가 None인 로봇을 가까운 노드로 임의 대체하지 않는다. 현재 adapter에는 확인된 노드가 필요하며 간선 점유를 `zone_occupancy`로 공급하는 것만으로 그 로봇의 전체 남은 경로 표현이 완료되는 것은 아니다. 점유를 공급해도 여러 번 순차적으로 얻은 snapshot이 자동으로 같은 시점이 되지는 않는다.

## 10. 테스트 방식과 재실행 명령

### 빠른 통합·예약 회귀 테스트

```bash
cd /home/jhlee/Logistics_AI
/home/jhlee/venv/robot/bin/python -m unittest \
  tests.test_kev_mapf_integration tests.test_fixed_route_reservation -v
```

연결 테스트만 확인하려면:

```bash
/home/jhlee/venv/robot/bin/python -m unittest tests.test_kev_mapf_integration -v
```

한 시나리오만 실행하려면:

```bash
/home/jhlee/venv/robot/bin/python -m unittest \
  tests.test_kev_mapf_integration.MapfIntegrationTests.test_reroute_reuses_preview_with_original_robot_model -v
```

시스템 기본 Python에는 dotenv가 없을 수 있다. 실제 프로젝트 의존성이 설치된 위 가상환경을 사용한다. 설치나 서버 시작 없이 수행하는 검증이다.

### 테스트 대역 구성

| 함수/클래스 | 역할 |
|---|---|
| `graph_data()` | 0 weight, 비최단 경로, 공유 노드, 우회 간선을 포함한 가짜 GeoJSON 생성 |
| `requests()` | 두 주문의 문자열 경로와 서로 다른 selector/model 생성 |
| `Decisions.__init__(action='action_wait')` | 판단 입력 기록과 고정 action 설정. 예상하지 않은 새 경로 생성은 실패시키는 Mock route 설치 |
| `Decisions.pick(...)` | 전달 state를 deepcopy로 기록하고 action 질문에서 지정 action, 나머지 질문에서 첫 후보 선택 |
| `MapfIntegrationTests.setUp()` | 매 테스트에 새 graph/requests 제공. `urllib.request.urlopen`을 실패하도록 patch하고 cleanup 등록 |
| `MapfIntegrationTests.plan(**kwargs)` | 테스트 graph/requests로 공개 연결 함수 호출하는 헬퍼 |

우회 테스트는 LiveKEV 객체의 `preview_llm_route`를 Mock으로 바꾸고 `route()` 자체의 기존 경계 사용·검증을 확인한다. 생성자만 실행하며 실제 selector 판단은 `Decisions`로 대체한다. 기존 실제 주행기 회귀 테스트도 가짜 goal 전송 함수를 사용한다.

### 신규 12개 테스트에서 검증하는 내용

| 테스트 함수 | 검증 내용 |
|---|---|
| `test_provided_non_shortest_paths_are_preserved_without_inference` | 비최단 공급 경로 보존, 재추론 없음, int/str 출력, 입력 불변성, 실행 허가 false |
| `test_every_decision_receives_full_graph_and_robot_neighbors` | zone/priority/action/wait 네 판단 입력에 전체 그래프·0 weight·모든 로봇 인접 간선/상태 포함, JSON 직렬화 |
| `test_graph_weights_and_validation_match_route_service` | 기존 route_service와 노드/weight 일치, 기존 검증 함수 호출, 저장 weight 누락 거부 |
| `test_invalid_endpoints_nodes_and_reverse_edges_are_rejected` | 시작/목적지 불일치, 없는 노드/유향 간선, 빈 경로 거부 |
| `test_reused_directed_edge_is_rejected` | 같은 방향 간선 재사용 거부 |
| `test_duplicate_robot_and_missing_result_are_rejected` | 중복 로봇 ID와 result 누락 거부 |
| `test_single_robot_and_idle_node_occupant` | 단일 로봇, 한 노드 정차 점유, 점유자 우선 및 HOLD 상태 |
| `test_node_ids_reject_lossy_conversion` | bool/float/비정수 문자열 ID 거부 |
| `test_reroute_reuses_preview_with_original_robot_model` | 제외된 임시 그래프를 기존 preview에 전달, 그 주문의 selector/model 유지, 우회 적용, 입력 보존 |
| `test_missing_catalog_propagates_without_runner_fallback` | 직접 호출 시 catalog 오류 전달, MAPF 우회에서는 HOLD와 원본 경로 보존 |
| `test_reroute_endpoint_and_forbidden_resource_validation` | 우회 끝점 오류/제외 구간 재진입 거부, 실패 시 원래 경로 보존 |
| `test_optional_order_preview_uses_dispatcher_contract` | order preview 헬퍼가 기존 dispatcher와 같은 인자 사용 |

기존 고정 경로 예약 테스트 10개는 비최단 경로 유지, 대기/재개/도착, 간선 위 재개, 반복 노드, 잘못된 경로·방문 제한 거부, 알고리즘 경로, 시뮬레이션·실제 주행기 경유점 전달을 확인한다.

### 검증 범위와 한계

총 22개 테스트 통과 결과는 오프라인 경계·검증·기존 예약 흐름에 대한 결과다. 실제 Kev 추론 품질, 모델 서버 지연, ROS/Nav2 통신, 물리적 대기 공간, 실제 로봇 안전성은 검증하지 않았다. 신규 테스트는 유향 간선 재사용 거부를 확인하지만 모든 내부 분기나 노드 방문 상한 단독 시나리오를 각각 전수 검사하지는 않는다. 기본 연결 함수의 제공 경로는 재추론하지 않지만 `max_reroutes`가 양수이면 실제 planner가 우회를 생성할 수 있다.
