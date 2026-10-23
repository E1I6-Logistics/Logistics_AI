# KEV 배차 실험 기록 — 2026-10-15

## 1. 목적

주문 배차에서 KEV가 가용 로봇 후보를 선택하는지, KEV 장애 시 기존 알고리즘으로 fallback하는지, 실패 후 cooldown 동안 KEV 재호출을 건너뛰는지 시뮬레이션으로 확인한다.

이번 실험은 **시뮬레이션 전용**이다. 실제 로봇 주행 검증으로 해석하지 않는다.

## 2. 확인할 동작

- `algorithm` 운영 모드: 기존 알고리즘 배차를 사용한다.
- `ai` 운영 모드: dispatch만 KEV를 사용하고 route와 workstation은 알고리즘을 사용한다.
- 가용 후보가 1대뿐이면 KEV 요청을 생략한다.
- KEV 호출이 실패하면 알고리즘으로 fallback하고 기본 30초 동안 KEV 호출을 건너뛴다.
- 기본 KEV 배차 HTTP timeout은 3초다. 연결이 닫혀 있으면 연결 거부가 빨리 날 수 있으므로, 닫힌 포트 시험은 timeout 측정이 아니다.

관련 설정과 구현은 `backend/app/services/mode_service.py`, `backend/app/services/order_dispatcher.py`, `backend/app/services/order_manager.py`, `backend/app/services/mock_data.py`에 있다.

## 3. 실험 전 상태 및 주의사항

- 이번 기록 작성 시점에 백엔드 `8000`과 Kev `8011`은 실행 중이 아닌 것으로 확인했다. 실행 전에는 상태를 다시 점검한다.
- 백엔드는 시작할 때 `operation_mode=algorithm`으로 초기화된다. **매 실행 후 주문 전에 AI 또는 algorithm 모드를 다시 설정한다.**
- `SIM_BATTERY_OVERRIDE`는 백엔드 시작 시 시뮬레이션 로봇의 초기 배터리에 반영된다. 적용 여부는 환경변수보다 `GET /api/robots` 응답으로 확인한다.
- `start_fms.sh`는 `backend.log`를 `>`로 열어 재시작 때 덮어쓴다. 실행별 로그를 보관하려면 중지/재시작 전에 복사한다.
- `KEV dispatch selected`, 후보 1대 생략, cooldown 로그는 INFO 로그다. 현재 앱에 INFO 로그 구성이 없으면 표시되지 않을 수 있다. 실험 전에 INFO 로그가 보이는지 확인하고, 안 보이면 별도 로깅 설정을 적용한 뒤 재시작한다. `falling back` 경고는 WARNING 로그다.
- `time curl /api/orders`는 주문 등록 응답 시간을 측정할 뿐, 백그라운드 `tick()`의 KEV 처리 시간을 측정하는 수단으로 사용하지 않는다.
- A에서 robot3 선택은 예상 결과이지 결정적 보장은 아니다. 연동 여부와 선택 품질을 따로 기록한다.

## 4. 공통 시작 및 점검

프로젝트 루트:

```bash
cd /home/heeyeon/turtlebot3_E1i6/Logistics_AI
```

실행 상태 확인:

```bash
curl -fsS http://127.0.0.1:8000/health
curl -fsS http://127.0.0.1:8011/v1/models
```

AI 배차 시험을 할 때마다 (FMS 시작 완료 후):

```bash
curl -fsS -X PUT http://127.0.0.1:8000/api/mode \
  -H 'Content-Type: application/json' \
  -d '{"mode":"simulation","operation_mode":"ai"}'

curl -fsS http://127.0.0.1:8000/api/mode
curl -fsS http://127.0.0.1:8000/api/robots | python3 -m json.tool
```

응답의 `feature_engines.dispatch`가 `ai`인지 확인한다. 로봇 후보 확인 시 `robot1`, `robot2`, `robot3`이 `ONLINE`이고 `IDLE`인지 확인한다.

주문 한 건 등록 예시:

```bash
curl -fsS -X POST http://127.0.0.1:8000/api/orders \
  -H 'Content-Type: application/json' \
  -d '{"items":{"A":1,"B":0,"C":0,"D":0},"total_quantity":1,"operation_mode":"ai"}' \
  | python3 -m json.tool
```

주문과 로그 확인:

```bash
curl -fsS http://127.0.0.1:8000/api/orders | python3 -m json.tool
grep -E 'KEV dispatch|falling back' logs/backend.log
grep -E 'GET /v1/models|POST /v1/systemone' logs/kev.log
```

## 5. 시험 계획

### A. KEV 배차 연동 및 선택 품질

**조건:** 실제 Kev 서버 실행, 시뮬레이션, `ai` 운영 모드, 배터리 초기값 robot1=30 / robot2=78 / robot3=92.

매 회 실행 전 이전 FMS를 정상 종료하고, 프로젝트 루트에서 시작한다:

```bash
SIM_BATTERY_OVERRIDE="robot1=30,robot2=78,robot3=92" ./start_fms.sh
```

시작 후 공통 시작 및 점검 절차대로 AI 모드를 설정하고, `/api/robots`에서 배터리값 및 가용 후보를 확인한 뒤 주문을 한 건씩 등록한다. 3~5회 반복한다. 각 실행의 로그를 재시작 전에 복사한다.

```bash
cp logs/backend.log logs/kev_dispatch_A_run1_backend.log
cp logs/kev.log logs/kev_dispatch_A_run1_kev.log
```

| Run | 시간 | 가용 후보 | 배터리 1/2/3 | KEV POST 200 | KEV 선택 로그 | 배정 로봇 | 주문 완료 | 메모 |
|---|---|---|---|---|---|---|---|---|
| A-1 | 2026-10-15 15:36 | 3대 (robot1/2/3) | 30/78/92 | 성공 (HTTP 200) | `robot2` 선택 확인 | robot2 | 완료 후 복귀 확인 (15:37, IDLE/N1) | 주문 ID `e4e19d32-b59c-4697-8ad1-e71d1afdf9ba`; fallback 로그 없음 |
| A-2 | 2026-10-15 15:39 | 3대 (robot1/2/3) | 30/78/92 | 성공 (HTTP 200) | `robot2` 선택 확인 | robot2 | 최종 `COMPLETED` 확인 (15:43 조회) | 주문 ID `0de10344-0af9-494f-b1d9-1284236fe5d2`; fallback 로그 없음 |
| A-3 | 2026-10-15 15:43 | 3대 (robot1/2/3) | 30/78/92 | 성공 (HTTP 200) | `robot2` 선택 확인 | robot2 | 완료 (`COMPLETED`) | 주문 ID `1949b6f9-5dac-4e3d-9c8e-f4dba5a6bf70`; fallback 로그 없음 |
| A-4 | 2026-10-15 15:51 | 3대 (robot1/2/3) | 30/78/92 | 성공 (HTTP 200) | `robot1` 선택 확인 | robot1 | 진행 중 (15:51 조회) | 주문 ID `c1b3fb15-2bac-47c7-993f-659ccc903bdf`; fallback 로그 없음 |
| A-5 | 2026-10-15 15:53 | 3대 (robot1/2/3) | 30/78/92 | 성공 (HTTP 200) | `robot1` 선택 확인 | robot1 | 진행 중, 작업대 이동 단계 (`MOVING_TO_WORKSTATION`, 15:53 조회) | 주문 ID `21361efa-e2a7-41e5-8a32-dd2061571b43`; fallback 로그 없음 |

#### A 실험 조건

- 백엔드는 15:35에 시작된 동일 프로세스에서 A-1~A-5를 수행했다.
- KEV 서버도 재시작하지 않은 동일 세션에서 5회 반복 추론했다.
- 매회 가용 후보는 `robot1`, `robot2`, `robot3`으로 동일했고 배터리 초기값은 30/78/92였다.
- 따라서 이번 결과는 독립적인 cold-start 5회가 아니라, 동일한 백엔드/KEV 세션에서 조건을 맞춘 반복 추론 결과다.

#### A 실험 판정

**연동 판정: 통과.** KEV 서버의 `POST /v1/systemone ... 200 OK`, 백엔드의 `KEV dispatch selected` 로그, 주문의 실제 `robot_id`가 5회 모두 연결됐다.

KEV가 로그만 남기고 실제 배차에는 영향을 주지 않은 것이 아니라는 증거도 확보했다. 기존 알고리즘은 동일 후보 순서라면 첫 후보인 `robot1`을 선택하지만, A 실험에서는 `robot2`가 3회 실제 배정됐다. 따라서 KEV의 선택 결과가 주문의 `robot_id`에 반영되는 경로는 정상 동작한다.

**배터리 기반 선택 품질 판정: 기준 미달.** 배터리가 가장 높은 `robot3`은 한 번도 선택되지 않았고, 배터리 30%인 `robot1`이 2회 선택됐다. 이는 단순히 배터리 반영이 약한 수준을 넘어, 현재 지시문의 배터리 우선 의도와 반대되는 선택이 발생한 것이다.

주문 완료 여부는 KEV 배차 연동 판정과 분리한다. A-2는 15:43 조회에서 최종 `COMPLETED`였음을 확인했다. A-4와 A-5는 기록된 조회 시점에는 진행 중이었으며, FMS 재시작 전에 최종 상태를 다시 조회하지 못해 최종 완료 여부는 미확인이다.

### B1. Kev 프로세스 없이 닫힌 포트에서 즉시 fallback

**조건:** `FMS_KEV_ENABLED=0`, 포트 8011에 다른 프로세스가 없음, `ai` 모드.

먼저 이전 스택을 종료하고 8011 포트가 비었는지 확인한다. 이후:

```bash
FMS_KEV_ENABLED=0 SIM_BATTERY_OVERRIDE="robot1=30,robot2=78,robot3=92" ./start_fms.sh
```

AI 모드를 설정하고 주문 한 건을 등록한다. 포트 연결 거부는 timeout이 아니라 즉시 실패할 수 있다. 백엔드의 `falling back` WARNING과 대체 로봇 배정을 기록한다.

| 항목 | 결과 |
|---|---|
| 8011 포트가 비었음 | 확인 (연결 거부, listener 없음) |
| fallback WARNING | 확인 (2026-10-15 16:04:51, `/v1/models` 연결 거부) |
| 배정 로봇 | robot1, 주문 `e30e785e-3619-4f60-9574-7e235af477b1` 완료 |
| Kev 연결/요청 | Kev 프로세스 비활성, 요청 응답 없음; 연결 실패 후 fallback |

### B2. 응답 없는 TCP 서버로 timeout 및 cooldown 확인

**조건:** 가짜 서버가 8011에서 연결을 받고 응답하지 않음, FMS는 `FMS_KEV_ENABLED=0`, 운영 모드는 `ai`.

가짜 서버를 먼저 별도 터미널에서 실행한다. 아래 서버는 HTTP 응답을 보내지 않고, TCP 연결 시각을 출력하며 연결을 유지한다.

```bash
python3 - <<'PY'
import datetime
import socket

server = socket.socket()
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind(("127.0.0.1", 8011))
server.listen()
print("silent server listening on 8011", flush=True)
connections = []
while True:
    connection, address = server.accept()
    timestamp = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(timestamp, "connection from", address, flush=True)
    connections.append(connection)
PY
```

다른 터미널에서:

```bash
FMS_KEV_ENABLED=0 SIM_BATTERY_OVERRIDE="robot1=30,robot2=78,robot3=92" ./start_fms.sh
```

AI 모드로 바꾼 뒤 첫 주문을 넣고, 완료/배정 상태를 확인한 다음 cooldown 안에 두 번째 주문을 등록한다. 첫 호출은 새 selector의 `/v1/models` GET에서 멈추므로 가짜 서버에는 **대개 연결 한 건만** 찍히고, `/v1/systemone` POST는 발생하지 않는다. 첫 연결 후 약 3초 전후의 fallback 경고를 확인한다.

두 번째 주문은 cooldown 중이면 가짜 서버에 새 연결이 없어야 한다. cooldown 만료 후 재연결을 확인할 때는 주문 완료를 기다려 **가용 후보가 2대 이상**인지 먼저 확인한다. 한 후보만 남으면 코드가 Kev 호출을 건너뛰기 때문에 만료 시험이 되지 않는다. INFO 로그가 활성화되어 있지 않다면 cooldown 로그 대신 가짜 서버의 연결 시각과 backend WARNING을 함께 기록한다.

| 요청 | 주문 ID | 가용 후보 수 | 가짜 서버 연결 수/시각 | fallback 로그 시각 | 배정 로봇 | 판정 |
|---|---|---:|---|---|---|---|
| B2-1 첫 호출 | 재시험 주문 `c138a56f-b8c0-4c0b-a72b-525b8fa4f1a8` | 미확인 | 가짜 서버 연결 `16:09:55.650`; `/v1/models` 응답 대기 timeout | 2026-10-15 16:09:58 (`TimeoutError`) | robot1, 최종 `COMPLETED` | 30초 cooldown 시작; fallback 확인. 앞선 16:07:08 시도는 cooldown 중 후속 주문을 넣지 못해 만료됨 |
| B2-2 cooldown 중 | `fc9c20d3-48a4-45b6-b13f-744940699e24` | 최소 2대 (robot1 사용 중, robot2 배정) | 16:09:55 이후 새 연결 없음; cooldown 생략 로그 확인 | 2026-10-15 16:10:04 (`cooldown 24.0s left`) | robot2, 최종 `COMPLETED` | cooldown 분기에서 KEV 호출을 건너뛰고 알고리즘 배정; 연결 시각 기록에도 새 연결 없음 |
| B2-3 cooldown 만료 후 | `b4fc1c90-4c5a-4c3e-bf1c-246372edc6f1` | 2대 이상 (Kev 재호출 발생으로 확인; 정확한 수 미기록) | 가짜 서버 재연결 `16:11:49.430` | 2026-10-15 16:11:52 (`TimeoutError`) | robot1, 주문 `COMPLETED` (조회 시 `RETURNING`) | cooldown 만료 후 Kev 재호출 확인, 다시 timeout 후 fallback |

가짜 서버 종료는 해당 터미널에서 `Ctrl+C`로 한다. 이후 FMS를 재시작하기 전에 8011이 비었는지 확인한다.

### C. 기존 알고리즘 배차 회귀

**조건:** `SIM_BATTERY_OVERRIDE` 미설정, `algorithm` 운영 모드. 이전 override가 셸에서 export되어 있지 않고 로컬 `.env`에도 지정되어 있지 않은지 확인한다.

```bash
SIM_BATTERY_OVERRIDE= ./start_fms.sh
```

빈 값을 명시해 `.env`에 혹시 `SIM_BATTERY_OVERRIDE`가 있어도 시작 스크립트가 덮어쓰지 못하게 한다. 기존 B2 로그를 보관하고 무응답 가짜 서버를 종료한 뒤 8011 포트가 비어 있는지 확인한다. C에서는 Kev 서버를 켜 둔 상태로 알고리즘 주문 전후 `/v1/systemone` POST 횟수를 비교한다.

주문 전 `/api/mode`를 확인하고 필요하면 algorithm 모드로 설정한다:

```bash
curl -fsS -X PUT http://127.0.0.1:8000/api/mode \
  -H 'Content-Type: application/json' \
  -d '{"mode":"simulation","operation_mode":"algorithm"}'
curl -fsS http://127.0.0.1:8000/api/robots | python3 -m json.tool
```

기본 배터리 92/78/64와 기존 알고리즘 배차 결과를 기록한다. 시작 스크립트가 `.env`를 읽으므로 `.env`에 override가 설정돼 있다면 제거하거나 빈 값으로 두고 시작해야 한다.

| 항목 | 결과 |
|---|---|
| operation_mode | `algorithm` |
| 배터리 1/2/3 | 92/78/64 (override 미적용) |
| 주문 ID | `07482793-a03f-4a30-adb7-4c51fbf87965` |
| 배정 로봇 | robot1 |
| KEV POST 횟수 (주문 전/후) | 0 / 0 |
| KEV 관련 backend 로그 | 없음 |
| 주문 완료 | `COMPLETED` |

### D. KEV 배차 선택 품질 원인 분석 및 개선

B1, B2, C로 안전장치와 기존 알고리즘 회귀를 먼저 확인한 뒤 별도 품질 실험으로 진행한다.

#### D1. 원인 가설 및 확인 방법

| 가설 | 확인 방법 | 판정 기준 |
|---|---|---|
| KEV 요청에 배터리 값이 누락됐거나 예상과 다른 형식으로 전달됨 | 배차 요청 1회의 `state`, `candidates`, `instructions`와 응답의 선택·확률·점수 필드를 그대로 로그로 남긴다. | 요청 body에 30/78/92가 정확히 있고 후보 설명과 일치하는지 확인 |
| 후보 순서 편향 | 같은 입력을 유지하고 KEV에 전달 직전 후보 순서만 `robot3`, `robot2`, `robot1`로 바꾼 실험 조건을 만든 뒤 5회 실행한다. 각 요청의 criteria 키 순서를 기록한다. | 선택이 앞쪽 후보를 따라 이동하면 순서 편향 가능성이 높음 |
| 위치 또는 노드 번호 해석의 영향 | `pickup_nodes`와 각 로봇의 `current_node`를 요청 로그로 확인하고, 실제 그래프 기반 픽업 거리도 별도로 계산한다. | 노드 번호의 숫자상 근접성 또는 실제 이동 거리가 선택과 연관되는지 비교 |
| 동일 입력 재현성 | 같은 `state`, 후보 설명, 후보 순서를 그대로 재전송해 응답의 선택·확률을 비교하고, 입력이 달라진 항목을 확인한다. | 동일 입력의 응답이 달라지는지 또는 state/순서 변화에 따라 선택이 바뀌는지 확인 |

#### D1.1 선택 규칙과 반복 결과 해석

Kev는 choice 확률을 반환하고 `kev/api.py`의 `to_answers()`가 원시 확률의 최대값에 해당하는 option을 최종 `choice`로 정한다. Logistics_AI의 `KevSelector.select_choice()`는 그 `choice`를 그대로 사용하므로, 선택은 확률 sampling이 아니다. 다만 A-1~A-5는 각기 다른 `order_id`를 state에 포함한 요청이므로 완전히 동일한 model input 반복은 아니다. 반복 선택 차이를 설명하려면 매회 요청 `state`와 criteria를 비교해야 한다. 동일한 model input인데 선택이 달랐다면 확률값, 동률 여부, 추론 재현성을 추가 확인한다.

#### D1.2 연속 반복 측정 절차와 해석

FMS를 재시작하지 않고 한 세션에서 주문을 순차 실행하면 `backend.log`는 누적된다. 매 회차 `testD1_runN.log`를 따로 복사하면 앞 회차 로그까지 중복 포함되므로, 5회 주문을 모두 마친 뒤 한 번만 보관한다:

```bash
cp logs/backend.log logs/testD1_all.log
grep -c 'KEV_DISPATCH_DEBUG' logs/testD1_all.log
```

세 로봇이 모두 `IDLE`이고, 배터리 30/78/92, `current_node`와 `occupied_node`가 담당 station(각각 N0/N1/N2), `goal_node`/`route`가 비어 있는 상태인지 매 주문 전에 확인한다. `_dispatch_robot_profile()`은 `state`, `battery`, `current_node`, `occupied_node`, `goal_node`, `returning`을 KEV state에 넣는다. 주문 state에는 `items`, `total_quantity`, `pickup_nodes`, `workstation_node`, `last_robot_id`, `order_id`가 들어간다. 같은 조건의 새 주문이라면 `order_id`는 의도적으로 달라지고, 나머지 요청 state와 후보 설명·순서는 로그로 실제 동일한지 검증한다.

`question_id`는 주문 ID에 따라 달라지지만 Kev API에서 답변을 매핑하는 키일 뿐, 모델에 렌더링되는 state/options/instructions에는 포함되지 않는다. 그러므로 이를 다르게 했다는 이유만으로 확률 차이를 설명하지 않는다. 확률 변화가 `order_id`만 다른 요청들과 함께 나타나면 ID 영향의 단서가 되지만, 인과를 확정하려면 같은 요청을 재생해 state의 `order_id`만 바꾼 대조 실험을 수행한다. 정확히 같은 모델 입력인데 결과가 달라진 경우에는 수치적 추론 변동 또는 동률도 검토한다.

FMS 배차 코드에는 `KEV_DISPATCH_DEBUG=1`일 때만 한 줄 JSON INFO 로그를 남기는 계측을 추가했다. 로그에는 `order_id`, `question_id`, Kev에 전달한 전체 state, 후보 설명과 순서(`choices`), instructions, 응답의 probabilities/confidence/answer_confidence/choice가 포함된다. 호출이 예외로 끝나도 동일한 요청 필드와 예외 유형·메시지를 기록한 뒤 기존 예외/fallback 처리를 유지한다. 환경변수가 없거나 `1`이 아니면 이 추가 JSON 로그는 남기지 않는다.

재현 명령은 다음과 같다:

```bash
SIM_BATTERY_OVERRIDE="robot1=30,robot2=78,robot3=92" KEV_DISPATCH_DEBUG=1 ./start_fms.sh
```

INFO 로그는 현재 [backend/app/main.py](../backend/app/main.py)의 `backend.app` logger 설정을 통해 `backend.log`에 기록된다. 로그는 `KEV_DISPATCH_DEBUG {JSON}` 형식이며, Kev choice 응답에는 선택·confidence·probabilities가 있지만 기본 reason 필드는 없다.

첫 다섯 D-1 trial을 수행했고 `logs/testD1_all.log`에서 debug JSON 5건을 확인했다.

| Trial | order_id | 후보 battery | 후보 current/occupied node | probabilities (robot1/2/3) | confidence | choice | 최종 배정/상태 | 관찰 |
|---|---|---|---|---|---:|---|---|---|
| D1-1 | `3fc4df21-ff7a-4a02-9725-39f2273403f2` | 30/78/92 | 0/0, 1/1, 2/2 | 0.3433/0.3445/0.3122 | 0.0167 | robot2 | robot2, `COMPLETED` | 1·2위 확률 차 0.0012로 매우 근접; robot3 확률 최저 |
| D1-2 | `ad7f4c5b-2f87-4e93-842a-3e198cc21f42` | 30/78/92 | 0/0, 1/1, 2/2 | 0.3428/0.3464/0.3108 | 0.0197 | robot2 | 최종 `COMPLETED`; station 복귀 확인 | 로그상 D1-1과 order ID 외 state/choices/instructions 동일; 1·2위 차 0.0036 |
| D1-3 | `0cb9b1b2-8823-4a7a-97a9-a2c005e02263` | 30/78/92 | 0/0, 1/1, 2/2 | 0.3436/0.3406/0.3158 | 0.0154 | robot1 | 최종 `COMPLETED`; station 복귀 확인 | 요청 state/choices/instructions는 앞선 회차와 동일(order ID 제외); 1·2위 차 0.0030 |
| D1-4 | `7fbb9582-aa0f-418e-8d3b-8eb4ada4d40b` | 30/78/92 | 0/0, 1/1, 2/2 | 0.3333/0.3452/0.3216 | 0.0177 | robot2 | 최종 `COMPLETED`; station 복귀 확인 | 요청 조건은 앞선 회차와 동일(order ID 제외); robot3 확률은 상승했으나 여전히 최저 |
| D1-5 | `8ed9f2ab-6677-4c71-8a8a-a0c4f0a59102` | 30/78/92 | 0/0, 1/1, 2/2 | 0.3400/0.3410/0.3190 | 0.0115 | robot2 | 주문 `COMPLETED`; 조회 시 robot2 `RETURNING` | 요청 state/choices/instructions는 앞선 회차와 동일(order ID 제외); 1·2위 차 0.0010; 저장 로그 5건 확인 |

다섯 요청 모두 품목 A 1개, pickup N5, `last_robot_id=null`이며 profiles, choices, instructions는 같고 order ID만 달랐다. 선택은 robot2 4회, robot1 1회, robot3 0회다. robot3 확률은 다섯 회 모두 가장 낮았다(0.3108~0.3216). 확률 분포는 거의 균등하고 1·2위 차이는 0.0010~0.0036이다. 배터리가 가장 높은 robot3을 우선하는 경향은 관찰되지 않았으며, 이 결과는 선택 품질이 기대에 못 미친다는 증거다. order ID만 다른 요청에서 확률도 변했지만 ID의 인과 효과라고 확정하려면 state의 ID만 바꾼 대조 재생이 필요하다. 다섯 주문의 작업은 모두 `COMPLETED`; D1-5 조회 시 robot2는 station 복귀 중이었다.

**Temperature 주의:** Kev의 `/v1/models`에 표시되는 `temperature`는 checkpoint의 확률 보정용 temperature이며, 텍스트 생성의 sampling temperature가 아니다. `/v1/systemone` 요청 형식에도 temperature 옵션이 없다. 따라서 이를 0으로 낮추는 방법은 현재 API에서 사용할 수 없고, 양수 calibration temperature를 바꿔도 choice의 argmax 자체는 바뀌지 않는다. 이 실험에서는 temperature 조정보다 동일 입력 재현성, 후보/criteria 순서, state 표현을 확인한다.

**후보 순서 주의:** 현재 `available_robots()`는 후보를 `(우선순위, robot_id)` 기준으로 정렬하므로, 입력 로봇의 등록 순서만 바꿔서는 역순 실험이 되지 않는다. 실험 전용으로 KEV 호출 직전 후보 list와 profiles/criteria 순서를 함께 뒤집고, 그 순서가 실제 요청에 반영됐는지 기록해야 한다.

### D-2. Kev API 직접 재생 결과

FMS를 띄우지 않고 D1-1의 저장 요청을 바탕으로 `tools/kev_replay.py`에서 Kev `/v1/systemone`에 직접 요청했다. R0는 동일 요청을 3회 보냈고, R1은 후보(criteria)와 `state.robots` 순서를 함께 반전, R2는 `state.order.order_id`만 제거, R3은 후보 설명에 높은 배터리 우선 `battery_rank`를 추가, R4는 instructions만 최고 배터리 선택 문장으로 바꿨다. `question_id`는 모든 요청에서 원본 값을 유지했다.

실행 명령:

```bash
/home/heeyeon/venv/robot/bin/python tools/kev_replay.py --log logs/testD1_all.log
```

| 조건 | P(robot1) | P(robot2) | P(robot3) | confidence | choice |
|---|---:|---:|---:|---:|---|
| R0-1 | 0.3433 | 0.3445 | 0.3122 | 0.0167 | robot2 |
| R0-2 | 0.3433 | 0.3445 | 0.3122 | 0.0167 | robot2 |
| R0-3 | 0.3433 | 0.3445 | 0.3122 | 0.0167 | robot2 |
| R1 후보 및 profiles 순서 반전 | 0.3727 | 0.3142 | 0.3131 | 0.0591 | robot1 |
| R2 state에서 order_id 제거 | 0.3727 | 0.3099 | 0.3174 | 0.0590 | robot1 |
| R3 choices에 battery_rank 추가 | 0.3361 | 0.3821 | 0.2818 | 0.0732 | robot2 |
| R4 “Choose the robot with the highest battery.” | 0.1961 | 0.3453 | 0.4585 | 0.1878 | robot3 |

재생 스크립트를 출력 형식 정리 후 동일 조건으로 한 번 더 실행했으며, 두 실행의 R0–R4 결과는 일치했다. 따라서 위 표는 동일한 7-request batch 두 번에서 확인됐다.

**관찰:** R0의 같은 요청은 세 번 확률까지 정확히 같아 재현됐다. R2에서 state의 order ID를 제거하자 선택이 robot2에서 robot1로 바뀌고 확률도 달라져, ID가 모델 입력에 포함되어 결과에 영향을 줄 가능성이 높다. 인과 확인은 ID만 바꾼 R0/R2 요청을 동일 서버 세션에서 더 반복하는 것이 좋다. R4는 간단하고 직접적인 배터리 지시에서 robot3(배터리 92%)을 최고 확률로 선택했다. 따라서 모델이 배터리 비교를 전혀 못 한다기보다 원래 지시문과 추가 state/후보 정보가 판단을 희석했을 가능성이 있다. R3의 rank 표시는 robot3 선택을 높이지 못했다.

**R1 해석 제한:** D-2의 R1은 candidate 순서와 `state.robots` 순서를 동시에 뒤집었다. 이번 D-3에서는 두 변수를 각각 따로 반전해 분리했다.

### D-3. 배터리 순열 및 순서 분리 실험

D1-1 로그를 기준으로 `order_id`를 모든 state에서 제거하고, 같은 로봇 프로필과 노드에 배터리값 30/78/92를 여섯 가지로 배정했다. 각 배정에서 V1/V2/V3를 실행하고, V3에서는 criteria 순서만 또는 state profile 순서만 따로 뒤집었다. Kev 서버에 30개의 direct request를 보냈으며 FMS/로봇은 구동하지 않았다.

재현 명령:

```bash
/home/heeyeon/venv/robot/bin/python tools/kev_replay.py --d3 --log logs/testD1_all.log
```

#### V1 — 원래 instructions, order_id 제거

| Battery (robot1/2/3) | 정답 | P(robot1/2/3) | confidence | choice | 정답 여부 |
|---|---|---|---:|---|---|
| 30/78/92 | robot3 | 0.3727/0.3099/0.3174 | 0.0590 | robot1 | ✗ |
| 30/92/78 | robot2 | 0.3593/0.3299/0.3107 | 0.0390 | robot1 | ✗ |
| 78/30/92 | robot3 | 0.4244/0.2672/0.3084 | 0.1366 | robot1 | ✗ |
| 78/92/30 | robot2 | 0.4132/0.3090/0.2778 | 0.1198 | robot1 | ✗ |
| 92/30/78 | robot1 | 0.4589/0.2519/0.2892 | 0.1883 | robot1 | ✓ |
| 92/78/30 | robot1 | 0.4642/0.2655/0.2703 | 0.1962 | robot1 | ✓ |

**정확도: 2/6.** 원래 지시문은 6가지 중 robot1이 최고 배터리인 두 조건에서만 정답이었다.

#### V2 — 단순한 최고 배터리 지시

| Battery (robot1/2/3) | 정답 | P(robot1/2/3) | confidence | choice | 정답 여부 |
|---|---|---|---:|---|---|
| 30/78/92 | robot3 | 0.2069/0.3387/0.4544 | 0.1815 | robot3 | ✓ |
| 30/92/78 | robot2 | 0.2092/0.4281/0.3628 | 0.1421 | robot2 | ✓ |
| 78/30/92 | robot3 | 0.3084/0.2442/0.4474 | 0.1711 | robot3 | ✓ |
| 78/92/30 | robot2 | 0.3039/0.4083/0.2878 | 0.1124 | robot2 | ✓ |
| 92/30/78 | robot1 | 0.4725/0.2190/0.3085 | 0.2087 | robot1 | ✓ |
| 92/78/30 | robot1 | 0.4619/0.2720/0.2661 | 0.1928 | robot1 | ✓ |

**정확도: 6/6.** 이 한 차례의 여섯 순열에서는 단순한 battery instruction이 모든 최고 배터리 로봇을 골랐다.

#### V3 — 유휴/복귀 필터 후 battery 우선, 기타 값은 동률 해소에만 사용

| Battery (robot1/2/3) | 정답 | P(robot1/2/3) | confidence | choice | 정답 여부 |
|---|---|---|---:|---|---|
| 30/78/92 | robot3 | 0.1405/0.2961/0.5634 | 0.3451 | robot3 | ✓ |
| 30/92/78 | robot2 | 0.1326/0.5076/0.3599 | 0.2614 | robot2 | ✓ |
| 78/30/92 | robot3 | 0.2743/0.1905/0.5353 | 0.3029 | robot3 | ✓ |
| 78/92/30 | robot2 | 0.2505/0.4692/0.2803 | 0.2037 | robot2 | ✓ |
| 92/30/78 | robot1 | 0.4364/0.2246/0.3390 | 0.1546 | robot1 | ✓ |
| 92/78/30 | robot1 | 0.4300/0.2780/0.2919 | 0.1451 | robot1 | ✓ |

**정확도: 6/6.** V3도 모두 정답이었고, 이 batch의 confidence는 0.1451~0.3451이었다.

#### V3 순서 영향 분리

`R1a`는 criteria 순서만 robot3→robot2→robot1로 바꿨고, `R1b`는 `state.robots` 목록만 뒤집었다. 여섯 배터리 순열마다 각각 실행했다.

| Battery (robot1/2/3) | 정답 | R1a P(robot1/2/3) | conf | choice | R1b P(robot1/2/3) | conf | choice |
|---|---|---|---:|---|---|---:|---|
| 30/78/92 | robot3 | 0.1654/0.3014/0.5332 | 0.2998 | robot3 ✓ | 0.3043/0.2726/0.4232 | 0.1347 | robot3 ✓ |
| 30/92/78 | robot2 | 0.1721/0.6046/0.2233 | 0.4069 | robot2 ✓ | 0.2527/0.4971/0.2502 | 0.2457 | robot2 ✓ |
| 78/30/92 | robot3 | 0.2758/0.1904/0.5338 | 0.3007 | robot3 ✓ | 0.4081/0.1986/0.3933 | 0.1121 | robot1 ✗ |
| 78/92/30 | robot2 | 0.2411/0.6017/0.1572 | 0.4025 | robot2 ✓ | 0.3813/0.4626/0.1561 | 0.1939 | robot2 ✓ |
| 92/30/78 | robot1 | 0.5817/0.2060/0.2124 | 0.3725 | robot1 ✓ | 0.7446/0.1259/0.1295 | 0.6169 | robot1 ✓ |
| 92/78/30 | robot1 | 0.5510/0.2783/0.1706 | 0.3265 | robot1 ✓ | 0.6961/0.1853/0.1186 | 0.5441 | robot1 ✓ |

**정확도:** R1a criteria-only 반전 6/6, R1b state-profile-only 반전 5/6. 이 표본에서는 criteria 순서 반전으로 정답률이 떨어지지 않았다. state profile 순서 반전에서는 78/30/92 조건에서 robot1을 골라 한 번 틀렸지만, 한 건만으로 일반적인 위치 편향이라고 단정할 수는 없다.

#### D-3 해석

- V1 2/6, V2 6/6, V3 6/6이었다. 이 입력·모델 조합에서는 배터리 선택 실패를 가장 잘 설명하는 요소가 원래 instructions의 모호함이라는 신호다.
- R2에서 order_id 제거만으로 결과가 바뀐 점, 그리고 D3에서 order_id를 제거한 V2/V3의 높은 정확도는 ID 제거를 백엔드에서 시험할 근거다. 그래도 R2는 기존 고정 배터리 배정 한 건이므로, 순열 환경에서 ID 유/무를 나란히 비교하는 후속시험이 가장 엄밀하다.
- R1a 6/6은 criteria 순서만의 강한 편향을 지지하지 않는다. R1b에서 5/6이었으므로 state profile 순서 영향 가능성은 남지만 표본으로 확정할 수 없다.
- R4와 달리 V2/V3가 6/6이어서 최고 배터리 선택 능력은 확인됐으나, 이는 결정적 보장이 아니며 실제 배차 지시문 평가도 시뮬레이션 조건에 한정된다.

### D-4. Backend 반영 및 A′ 재시험

D-3 근거를 적용해 `OrderManager._select_robot_with_kev()`를 다음과 같이 변경했다.

- `state.order.order_id`는 Kev state에서 제거한다. 상위 debug record의 `order_id`와 API 응답 매핑용 `question_id`는 유지한다.
- `state.robots` profiles를 `robot_id` 오름차순으로 고정한다. `choices`도 같은 순서를 따른다.
- instructions는 D-3 V3 문장으로 교체한다.
- algorithm 모드 분기와 KEV 실패 시 기존 fallback 알고리즘은 변경하지 않았다.
- V3에서 빠진 "직전 로봇 회피"는 지시문 대신 코드 규칙으로 옮겼다. 재시도 주문은 다른 후보가 있으면 `last_robot_id`를 KEV 후보에서 제외하고, 그 로봇만 남으면 그대로 배정한다. 새 주문(`last_robot_id` 없음)인 A′에는 영향이 없다. 이후 전체 테스트는 **112 passed, 같은 기존 3 failed**다.

변경 후 `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 ... pytest tests --ignore=tests/test_selector_latency_benchmark.py --ignore=tests/test_selector_question_types.py`는 기준과 같은 **111 passed, 같은 기존 3 failed**였다. KEV debug 관련 집중 테스트는 7 passed다. 전체 실패 목록은 아래 테스트 기준 표와 같다.

A′ 재시험은 새 백엔드 세션에서 아래 조건으로 진행한다. KEV 서버는 start script가 함께 시작한다. AI 모드로 설정한 뒤 후보 세 대가 같은 홈 node에 복귀하고 IDLE인 것을 확인해 5회 주문을 순차 실행한다. 각 주문은 작업 완료·station 복귀 후 다음 회차를 넣는다.

```bash
SIM_BATTERY_OVERRIDE="robot1=30,robot2=78,robot3=92" KEV_DISPATCH_DEBUG=1 ./start_fms.sh
```

```bash
curl -fsS -X PUT http://127.0.0.1:8000/api/mode \
  -H 'Content-Type: application/json' \
  -d '{"mode":"simulation","operation_mode":"ai"}'
```

다섯 회차 후 `logs/backend.log`를 `logs/testA_prime.log`로 복사한다. 각 JSON에서 top-level `order_id`/`question_id`는 남고 `state.order.order_id`는 빠져야 하며, profiles는 robot1/robot2/robot3 순서여야 한다. A′의 목표 관측은 5/5 robot3 선택 및 매회 robot3 확률 1위다. confidence도 회차별로 기록하되, 5회 결과만으로 운영 임계값을 확정하지 않는다.

#### A′ 결과 (2026-10-16, 로그: `logs/testA_prime.log`)

위 조건으로 백엔드를 시작했고, 실행 중인 백엔드 프로세스 환경에서 `SIM_BATTERY_OVERRIDE=robot1=30,robot2=78,robot3=92`와 `KEV_DISPATCH_DEBUG=1`을 확인했다. 주문 8건을 넣었고, 그중 배차 시점에 세 대가 모두 홈 node(0/1/2)에서 IDLE이었던 6건을 A′ 회차로 집계했다.

| 회차 | 주문 | 시각 | 후보 | P(robot1) / P(robot2) / P(robot3) | confidence | 선택 |
|---|---|---|---|---|---|---|
| 1 | ORDER-001 | 09:32:58 | robot1·2·3 | 0.1405 / 0.2961 / **0.5634** | 0.3451 | robot3 |
| 2 | ORDER-002 | 09:34:32 | robot1·2·3 | 0.1405 / 0.2961 / **0.5634** | 0.3451 | robot3 |
| 3 | ORDER-005 | 09:40:12 | robot1·2·3 | 0.1405 / 0.2961 / **0.5634** | 0.3451 | robot3 |
| 4 | ORDER-006 | 09:40:43 | robot1·2·3 | 0.1405 / 0.2961 / **0.5634** | 0.3451 | robot3 |
| 5 | ORDER-007 | 09:41:15 | robot1·2·3 | 0.1405 / 0.2961 / **0.5634** | 0.3451 | robot3 |
| 6 | ORDER-008 | 09:41:49 | robot1·2·3 | 0.1405 / 0.2961 / **0.5634** | 0.3451 | robot3 |

- **결과: 6/6 robot3 선택, 매회 robot3 확률 1위.** 모든 회차에서 `falling back` 경고가 없었다. 같은 조건의 기존 algorithm 배차라면 목록 순서에 따라 robot1이 선택된다.
- 각 JSON에서 top-level `order_id`/`question_id`는 남았고, `state.order.order_id`는 없었으며, profiles는 robot1/robot2/robot3 순서였다.
- 회차마다 `question_id`가 달랐지만 확률이 소수 넷째 자리까지 같았다. 이 조건에서는 `question_id`가 선택에 영향을 주지 않는다. 출력이 결정적이므로 같은 입력의 반복 횟수는 신뢰도를 더 높여 주지 않는다.
- 참고 회차 ORDER-003에서는 robot3이 빠진 2대 후보(30/78)에서도 robot2를 골랐다. 후보 구성이 달라져도 배터리 기준을 따른다는 추가 근거다(1건).
- confidence 0.3451은 D-3 V3 batch 범위(0.1451~0.3451)의 상단과 같다. 6회 결과만으로 운영 임계값을 정하지 않는다.

참고 회차 (A′ 집계 제외, robot3이 이전 주문을 처리하던 중에 주문을 넣음):

| 주문 | 시각 | 후보 | 결과 | 비고 |
|---|---|---|---|---|
| ORDER-003 | 09:34:39 | robot1·2 | P = 0.3816 / **0.6184**, conf 0.2367 → robot2 | 남은 후보 중 배터리가 높은 쪽을 선택. algorithm이라면 robot1 |
| ORDER-004 | 09:34:40 | robot1 | KEV 호출 없음(`skipped (single candidate)`) → robot1 | robot3은 API에서 IDLE(node 5)로 보였지만 ORDER-002 처리 중이라 후보에서 제외됨 |

ORDER-004에서 보듯 API의 `status: IDLE`만으로는 배차 가능 여부를 판단할 수 없다. 다음 실험에서는 주문 목록에 PENDING/IN_PROGRESS 주문이 없는지도 함께 확인한 뒤 다음 회차를 넣는다.

한계: 시뮬레이션에서만 검증했다. 시뮬레이션 배터리는 주행 중 줄지 않으므로 초기값 기준의 판단만 확인했다. 재시도 주문(`last_robot_id` 회피)과 실제 로봇(`fleet_manager`) 경로는 A′ 범위에 포함되지 않는다.

### D-5. 재시도 주문의 직전 로봇 제외 (2026-10-16)

V3 지시문에서 빠진 "직전 로봇 회피"를 코드 규칙으로 옮긴 뒤, 실제 KEV 서버로 재시도 흐름을 확인했다. 시뮬레이션 주문은 API로 `FAILED`를 만들 수 없다. 정지 명령을 보내면 주문이 `CANCELLED`가 된다. 그래서 `tools/retry_scenario.py`가 백엔드 모듈을 직접 불러 백엔드와 같은 10Hz 루프(`order_manager.tick()` → `mock_fms.advance_simulation()`)를 돌린다. 운영 코드는 수정하지 않았다.

절차 (FMS 백엔드는 끄고 KEV 서버만 켠 상태):

```bash
source /opt/ros/jazzy/setup.bash
~/venv/robot/bin/python tools/retry_scenario.py
```

스크립트는 `SIM_BATTERY_OVERRIDE=robot1=30,robot2=78,robot3=92`와 `KEV_DISPATCH_DEBUG=1`을 직접 설정한다. 8000 포트가 열려 있으면 실행을 멈춘다. 그다음 아래 순서로 진행한다.

1. AI 모드로 주문 1건을 넣고 첫 배정을 기록한다.
2. 픽업대 작업 중(`WORKING_AT_PICKUP`)에 `order_manager.fail()`을 호출한다. 주행 중(MOVING)에 실패시키면 자동 복귀가 시작되지 않는다.
3. 실패 로봇이 station으로 복귀하고, 세 대 모두 `dispatch_available=true`가 될 때까지 기다린다.
4. `retry_order()`를 호출한 뒤 재배정 결과와 KEV debug JSON을 기록한다.

결과 (1회). 이때는 사용자 백엔드가 켜져 있어 `--allow-backend`로 실행했다. 스크립트는 별도 프로세스라 백엔드와 메모리 상태를 공유하지 않는다.

| 단계 | 결과 |
|---|---|
| 첫 배정 | robot3. 후보 robot1·2·3, P = 0.1405 / 0.2961 / **0.5634** (A′와 같음) |
| fail() 직후 | status=FAILED, recovery=PENDING, last_robot_id=None |
| 복귀 후 retry | status=PENDING, **last_robot_id=robot3** |
| 재시도 KEV 입력 | 후보 robot1·robot2, **excluded_last_robot=robot3**, `state.order.last_robot_id=robot3` |
| 재시도 KEV 응답 | P(robot1)=0.3966, P(robot2)=**0.6034**, confidence 0.2068 → robot2 |
| 최종 배정 | **robot2**, fallback 없음 |

판정: 5개 항목(첫 배정 robot3, 재시도 후보 robot1·robot2, excluded_last_robot=robot3, 선택 robot2, fallback 없음) 모두 PASS.

같은 조건으로 2회차를 실행했다(2026-10-16, `--allow-backend`). 확률, 후보, 선택 결과가 1회차와 같았고 5개 항목 모두 PASS였다. 원본 출력은 `logs/testD5_retry.log`에 저장했다. 이 파일은 git에서 제외된 폴더에 있어 로컬에만 있다.

- 줄어든 후보(30/78)에서도 배터리가 높은 robot2를 골랐다. A′ 참고 회차 ORDER-003(같은 두 후보)의 P(robot2)=0.6184와 값이 조금 다르다. 이번 입력에는 `last_robot_id=robot3`이 state에 들어 있었으므로 입력이 같지 않다.
- 제외는 KEV 입력 전에 코드로 처리하므로, 재시도 주문에서 직전 로봇이 다시 선택되는 일은 KEV 응답과 관계없이 일어나지 않는다. 단, 직전 로봇만 남으면 그 로봇을 배정한다.
- 한계: HTTP 계층과 실제 로봇 경로는 거치지 않았다. 실패는 인위적으로 주입했다.

### D-6. ETA 후보군 및 기준선 fallback 시나리오 (2026-10-23)

ETA 우선 정책을 반영한 backend에서 시뮬레이션 S0~S4를 실행했다. 첫 픽업은 `C=1`의 N6으로 고정했고, ETA 속도는 0.2 m/s, ETA 근소 차이 범위는 2초였다. 배터리는 시작 시 `SIM_BATTERY_OVERRIDE="robot1=40,robot2=90,robot3=70"`으로 고정했다.

공통 근소 ETA 배치는 robot1=N3(2.57초), robot2=N4(4.11초), robot3=N2(11.44초)였다. robot3는 robot2의 충전 노드 N1이 아닌 자신의 충전 노드 N2에 두어 복귀 경로를 막지 않았다. 세 로봇이 모두 IDLE 및 `dispatch_available=true`인 것을 확인한 후 주문을 등록했다.

| 시나리오 | 조건 | 관측 결과 | 판정 |
| --- | --- | --- | --- |
| S0 기존 알고리즘 | 공통 배치, algorithm 모드 | ORDER-001 `ffc8ec34-686b-44b6-b172-351d9ed36cd1`이 robot1에 배정되고 COMPLETED | PASS |
| S1 ETA 확실 우세 | robot1=N6(0초), robot2=N4(4.11초), robot3=N2(11.44초), AI 모드 | ORDER-002 `1c75e3ff-6624-4b4b-a175-d17439722836`이 robot1에 배정되고 COMPLETED. `KEV dispatch skipped (ETA winner)`, KEV POST 0→0 | PASS |
| S2 ETA 근소 후보군 | 공통 배치, AI 모드, KEV 서버 실행 | ORDER-003 `da4437c9-0cf2-417f-a7bd-dedc2fddb226`이 robot2에 배정되고 COMPLETED. 후보 robot1·robot2, `excluded_by_eta=["robot3"]`, 기준선 robot2, KEV choice robot2, P(robot1)=0.4653/P(robot2)=0.5347, confidence=0.0695, fallback 없음 | PASS |
| S3 KEV 오류 fallback | 공통 배치, `FMS_KEV_ENABLED=0`, cooldown=600초 | ORDER-001 `963f0fb3-9797-4351-89e3-e3f8dcf336ae`이 robot2에 배정되고 COMPLETED. 8011 연결 거부, debug JSON의 `"rule_baseline_choice":"robot2"`, fallback 경고 확인 | PASS |
| S4 cooldown fallback | S3 오류 후 공통 배치 재구성, 동일 backend, 600초 이내 | ORDER-002 `4520aa86-4343-4c37-bbf9-7ca1f20c15f3`이 robot2에 배정되고 COMPLETED. 15:42:47 KST에 등록했고 `KEV dispatch skipped (cooldown 505.3s left)` 확인 | PASS |

S3의 KEV 오류 로그 시각은 15:41:12 KST였고 S4 주문 등록은 약 95초 뒤였다. 따라서 S4는 설정한 600초 cooldown 안에서 실행됐다. S3/S4에서 `falling back to algorithm`은 기존 경고 문구지만, 실제 선택은 같은 ETA 후보군에서 계산한 임시 규칙 기준선(robot2)이었다. S2의 KEV 선택도 같은 기준선과 일치했지만, 단일 회차이므로 KEV가 기준선과 항상 같은 선택을 한다는 결론은 내리지 않는다.

로그 보관 한계: S3 실행을 위해 FMS를 재시작하면서 `logs/backend.log`가 덮어써졌다. S0~S2는 본문에 기록한 API·터미널 관측값만 남아 있고, 별도 원본 backend 로그 파일은 보관하지 못했다. S3~S4는 현재 `logs/backend.log`와 재대조했다.

#### 이후 개선 방향

- 배터리 하한처럼 반드시 지켜야 하는 조건은 실제 주행 측정 후 정책값을 정한 뒤 `available_robots()`에서 코드로 처리한다. 확정된 하한 미만 로봇 제외는 KEV 프롬프트가 아닌 코드 필터로 보장한다.
- 후보 설명에 원시 배터리뿐 아니라 `battery_rank`, 첫 픽업까지의 실제 거리, 예상 이동 시간, 예약 혼잡도를 코드로 계산해 제공한다.
- 지시문은 `1순위 battery, 2순위 distance`처럼 우선순위를 명시한다.
- 하드 규칙과 계산 가능한 수치는 코드가 처리하고, KEV는 남은 후보 사이의 선택만 맡는다.

#### 후속 재실험 순서

1. 요청 body와 KEV 응답 메타데이터 확인
2. 후보 순서 반전 조건으로 5회 실행
3. 동일 입력 재현성 및 위치 영향 확인
4. 결과에 따라 프롬프트 또는 코드 하드 규칙 수정
5. 배터리 30/78/92 조건으로 다시 5회 실행

## 6. 과거 예비 점검 (A/B/C 결과에 합산하지 않음)

2026-10-15 예비 점검에서 시뮬레이션 로봇 세 대가 `ONLINE`/`IDLE`이었고, `operation_mode=ai` 상태에서 주문이 `robot1`에 배정되어 `COMPLETED`가 됐다. Kev 서버 로그에는 `POST /v1/systemone ... 200 OK`가 있었고, `falling back` 로그는 확인되지 않았다.

이 결과는 Kev 추론 요청 성공을 뒷받침하지만, 기본 알고리즘도 robot1을 선택할 수 있어 Kev만의 선택 결과를 구분하지 못한다. 따라서 본 실험의 반복 횟수에는 포함하지 않는다.

## 7. 전체 결과 요약 (실험 후 작성)

| 시험 | 반복 수 | 성공/통과 | 관측 결과 | 한계/후속 조치 |
|---|---:|---:|---|---|
| A KEV 배차 | 5 | 5/5 연동 확인 | robot2 3회, robot1 2회, robot3 0회; 매회 추론 HTTP 200; robot2 실제 배정으로 KEV 선택 반영 확인 | 연동은 통과. 배터리 우선 판단 품질은 기준 미달. 동일 백엔드/KEV 세션의 반복 추론 |
| B1 포트 닫힘 fallback | 1 | 통과 | 8011 연결 거부 후 fallback WARNING, robot1에 배정되어 주문 완료 | 즉시 연결 실패 확인. HTTP timeout 측정은 아님 |
| B2 무응답 timeout/cooldown | 3주문 확인 | timeout/fallback, cooldown 생략, 만료 후 재시도 모두 통과 | 연결 16:09:55.650 → 16:11:49.430; 두 번째 주문은 새 연결 없음; 만료 후 16:11:52 timeout/fallback | 응답 없는 서버를 이용한 시뮬레이션 시험이며 실 KEV 처리시간 검증은 아님 |
| C 알고리즘 회귀 | 1 | 통과 | 기본 배터리 92/78/64; robot1 배정 및 주문 완료; KEV POST 0→0, 관련 backend 로그 없음 | 해당 시뮬레이션 조건에서 algorithm 경로가 KEV를 호출하지 않음 확인 |
| D1/D2/D3 선택 입력·확률 분석 | D1 5회 + D2 14 + D3 30 direct requests | 직접 재생 완료 | D3 정확도: V1 2/6, V2 6/6, V3 6/6; R1a criteria-only 6/6, R1b state-only 5/6 | V2/V3는 최고 배터리 선택. R1b 한 오답은 재현성 추가 확인 필요 |
| D4 Backend 반영/A′ | Backend 변경 완료; A′ 6회 (+참고 2회) | A′ 완료 (2026-10-16) | order_id state 제거, V3 prompt, robot_id 정렬, last_robot_id 코드 제외; fallback/algorithm 모드 유지 | 30/78/92 조건에서 6/6 robot3, 매회 P(robot3)=0.5634 1위, fallback 없음 |
| D5 재시도 직전 로봇 제외 | 1 | 통과 (5/5 판정 항목) | 첫 배정 robot3 → fail/복귀/retry → 후보 robot1·robot2, excluded_last_robot=robot3, P(robot2)=0.6034 → robot2 | `tools/retry_scenario.py` 프로세스 내 시험. HTTP 계층·실제 로봇 미포함, 실패는 인위 주입 |
| D6 ETA 후보군·기준선 fallback | S0~S4 각 1회 | 5/5 통과 | ETA 확실 우세 시 KEV 생략, 근소 ETA 후보군에서 robot3 제외, KEV 오류·cooldown에서 robot2 기준선 선택 | 시뮬레이션 단일 회차. 실제 로봇 ETA·배터리 소모는 미검증 |

### 테스트 기준 및 D-1 계측 검증

프로젝트 테스트 실행 시 ROS pytest entry-point 충돌을 피하기 위해 `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`로 실행했다. 기준 및 변경 후 모두 같은 환경과 명령을 사용했다.

| 구분 | 결과 |
|---|---|
| 변경 전 집중 실행 | `tests/`에서 108 passed, 3 failed, 2 collection errors |
| 변경 후 집중 실행 | `tests/`에서 111 passed, 동일한 3 failed, 동일한 2 collection errors |
| 기존 실패 3개 | `test_ai_route_is_requested_only_after_workstation_selection`, `test_invalid_ai_route_is_not_driven_and_failure_returns_robot`, `test_random_cases_are_reproducible_disjoint_and_target_docking_nodes` |
| 기존 수집 오류 2개 | `test_selector_latency_benchmark.py`, `test_selector_question_types.py` (Route Graph 노드/Point fixture 문제) |
| 추가된 debug 로그 테스트 | 3 passed: 환경변수 on/off 동작, 성공 응답 필드, 예외/timeout 로그와 fallback |

D1 runtime debug 로그 5건을 `logs/testD1_all.log`에 저장했다. 다섯 회차 모두 선택 확률의 최댓값과 최종 `choice`가 일치했다.

테스트 실행을 위해 pytest만 `/home/heeyeon/venv/robot`에 설치했으며 프로젝트 dependency 파일은 수정하지 않았다. 커밋은 생성하지 않았다.

### 해석 한계

- 시뮬레이션에서만 수행한다.
- 배터리는 초기값 override로만 설정되며 시뮬레이션 주행 중 감소하지 않는다.
- Kev는 확률 분포를 반환하지만 현재 API의 `choice`는 argmax이므로 요청마다 확률 sampling하는 구조가 아니다. 5회 중 robot3 선택률은 이번처럼 입력 state가 달라지는 작업에서의 관측 빈도이지, 동일 입력의 sampling 확률로 해석하지 않는다.
- A-1~A-5는 백엔드와 KEV를 매회 재시작한 독립 실행이 아니라 동일 세션에서 수행한 반복 추론이다.
- A의 주문 완료율은 배차 선택 품질과 별도 지표로 기록한다.
- B2의 HTTP timeout은 백그라운드 tick을 막을 수 있다. 주문 API의 curl 응답시간을 KEV 처리시간으로 해석하지 않는다.
