# KEV 배차 ETA 후보군 실험 시나리오 - 2026-10-23

## 1. 목적

ETA 근소 차이 범위가 2초일 때 다음 동작을 시뮬레이션으로 확인한다.

- ETA가 확실히 짧은 후보는 KEV 호출 없이 코드가 배정한다.
- ETA가 근소한 후보들만 KEV에 전달한다.
- KEV 오류와 cooldown에서는 같은 ETA 후보군 안의 임시 규칙 기준선으로 대체 선택한다.
- 기존 목록 순서 알고리즘, 임시 규칙 기준선, KEV 선택 결과를 구분해 기록한다.

이 문서는 시뮬레이션 배차 검증용이다. 실제 로봇 주행 품질, 실제 배터리 소모, 최소 배터리 하한은 검증 범위에 포함하지 않는다.

## 2. 정책과 공통 조건

| 항목 | 값 |
| --- | --- |
| 첫 픽업 주문 | `C=1` (`N6`) |
| ETA 속도 | 0.2 m/s |
| ETA 근소 차이 범위 | 2초 |
| 로봇 배터리 | robot1=40, robot2=90, robot3=70 |
| 임시 규칙 기준선 | IDLE·비복귀, 현재 배터리 높은 순, robot_id 오름차순 |

`N6`까지의 route graph 기준 ETA는 다음과 같다.

| 노드 | 거리 | ETA |
| --- | ---: | ---: |
| N6 | 0.000 m | 0.00초 |
| N3 | 0.513 m | 2.57초 |
| N4 | 0.822 m | 4.11초 |
| N2 | 2.287 m | 11.44초 |
| N0 | 2.438 m | 12.19초 |

충전 노드는 robot1=N0, robot2=N1, robot3=N2이다. 다른 로봇의 충전 노드에 로봇을 배치하지 않는다.

2026-10-24부터 `.env`에 실제 로봇용 `KEV_DISPATCH_ETA_SPEED_MPS=0.06`이 있다. 이 시나리오를 재현할 때는 아래 시작 명령 앞에 `KEV_DISPATCH_ETA_SPEED_MPS=0.2`를 붙여 0.2 m/s로 실행한다.

## 3. 공통 절차

프로젝트 루트:

```bash
cd /home/heeyeon/turtlebot3_E1i6/Logistics_AI
```

S0~S2는 KEV 서버를 포함해 아래처럼 시작한다.

```bash
SIM_BATTERY_OVERRIDE="robot1=40,robot2=90,robot3=70" \
KEV_DISPATCH_DEBUG=1 \
./start_fms.sh
```

수동 배치 전에는 algorithm 모드로 둔다.

```bash
curl -fsS -X PUT http://127.0.0.1:8000/api/mode \
  -H 'Content-Type: application/json' \
  -d '{"mode":"simulation","operation_mode":"algorithm"}'
```

수동 이동은 아래 형식으로 보낸다. `driving_mode`를 생략하면 standard가 적용된다.

```bash
curl -fsS -X POST http://127.0.0.1:8000/api/command/goal-node \
  -H 'Content-Type: application/json' \
  -d '{"robot_id":"robot1","node_id":"3"}'
```

배치 후, 그리고 모드 전환 전에는 다음을 확인한다.

- 이전 주문이 `COMPLETED`이고 복귀가 완료되었다.
- 세 로봇의 `goal_node`와 `route`가 비어 있다.
- 세 로봇이 `IDLE`이고 `dispatch_available=true`다.
- 각 로봇의 `occupied_node`가 시나리오 표의 위치와 같다.

로봇 상태 확인:

```bash
curl -fsS http://127.0.0.1:8000/api/robots | python3 -m json.tool
```

### 시나리오별 로그 보관

`start_fms.sh`는 시작할 때 `logs/backend.log`와 `logs/kev.log`를 덮어쓴다. 각 시나리오의 결과와 로그를 확인한 직후, 다음 시나리오를 위해 FMS를 재시작하기 전에 아래처럼 복사한다. `logs/`는 git 추적 대상이 아니므로 원본 로그는 로컬에만 보관된다.

```bash
cp logs/backend.log logs/testD6_S0_backend.log
cp logs/kev.log logs/testD6_S0_kev.log
```

`S0` 부분을 해당 시나리오 이름으로 바꿔 S1~S4에도 적용한다. KEV를 실행하지 않는 S3/S4에서는 `kev.log` 복사는 생략해도 된다.

AI 배차 모드 전환:

```bash
curl -fsS -X PUT http://127.0.0.1:8000/api/mode \
  -H 'Content-Type: application/json' \
  -d '{"mode":"simulation","operation_mode":"ai"}'
```

주문 등록:

```bash
curl -fsS -X POST http://127.0.0.1:8000/api/orders \
  -H 'Content-Type: application/json' \
  -d '{"items":{"A":0,"B":0,"C":1,"D":0},"total_quantity":1,"operation_mode":"ai"}' \
  | python3 -m json.tool
```

## 4. S0 - 기존 알고리즘 비교 기준

| 로봇 | 위치 | N6 ETA | 배터리 |
| --- | --- | ---: | ---: |
| robot1 | N3 | 2.57초 | 40 |
| robot2 | N4 | 4.11초 | 90 |
| robot3 | N2 | 11.44초 | 70 |

1. 공통 배치를 만든다.
2. `operation_mode=algorithm`을 유지한다.
3. `C=1` 주문을 등록한다. 이때 요청의 `operation_mode`는 생략하거나 `algorithm`으로 보낸다.

**기대 결과:** 기존 알고리즘은 후보 목록 순서에 따라 robot1을 배정한다.

**기록:** 후보 순서, 배정 robot_id, 주문 완료·복귀 여부, KEV 호출이 없었는지.

## 5. S1 - ETA가 확실히 앞서는 후보

| 로봇 | 위치 | N6 ETA | 배터리 |
| --- | --- | ---: | ---: |
| robot1 | N6 | 0.00초 | 40 |
| robot2 | N4 | 4.11초 | 90 |
| robot3 | N2 | 11.44초 | 70 |

1. algorithm 모드에서 표의 위치로 배치한다.
2. 모든 수동 주행이 끝난 뒤 AI 모드로 전환한다.
3. `C=1` 주문을 등록한다.

**기대 결과:** robot1만 최단 ETA+2초 범위에 들어간다. KEV 호출 없이 robot1이 배정된다.

**통과 조건:**

- 최종 배정은 robot1이다.
- backend 로그에 `KEV dispatch skipped (ETA winner)`가 있다.
- KEV 서버의 새 `/v1/systemone` 요청이 없다.

## 6. S2 - ETA 근소 후보군 KEV 선택

공통 배치를 사용한다.

| 로봇 | 위치 | N6 ETA | 배터리 | ETA 후보 여부 |
| --- | --- | ---: | ---: | --- |
| robot1 | N3 | 2.57초 | 40 | 포함 |
| robot2 | N4 | 4.11초 | 90 | 포함 |
| robot3 | N2 | 11.44초 | 70 | 제외 |

1. algorithm 모드에서 공통 배치를 만든다.
2. 모든 수동 주행이 끝난 뒤 AI 모드로 전환한다.
3. `C=1` 주문을 등록한다.

**기대 결과:** KEV에는 robot1·robot2만 전달된다. 임시 규칙 기준선은 robot2를 선택한다. KEV 선택은 robot1 또는 robot2일 수 있다.

**통과 조건:**

- debug JSON의 `excluded_by_eta`가 `["robot3"]`다.
- `choices`와 `state.robots`에 robot1·robot2만 있다.
- `rule_baseline_choice`가 robot2다.
- `falling back` 또는 cooldown 로그가 없다.
- 최종 배정이 robot1 또는 robot2다.

KEV가 후보 밖 로봇 ID를 응답하면 백엔드는 이를 오류로 처리하고 fallback한다. 이 경우는 KEV 정상 선택 검증인 S2에서 실패로 기록한다.

## 7. S3 - KEV 오류 fallback

KEV 서버를 실행하지 않고, cooldown을 600초로 설정한다.

```bash
./stop_fms.sh

FMS_KEV_ENABLED=0 \
KEV_DISPATCH_COOLDOWN_SECONDS=600 \
SIM_BATTERY_OVERRIDE="robot1=40,robot2=90,robot3=70" \
KEV_DISPATCH_DEBUG=1 \
./start_fms.sh
```

1. 포트 8011에 KEV 서버가 없는지 확인한다.
2. algorithm 모드에서 공통 배치를 다시 만든다.
3. 모든 수동 주행이 끝난 뒤 AI 모드로 전환한다.
4. `C=1` 주문을 등록하고 KEV 오류 시각을 기록한다.

**기대 결과:** KEV 연결 오류 후 같은 ETA 후보군 안의 임시 규칙 기준선에 따라 robot2가 배정된다.

**통과 조건:**

- `KEV dispatch failed; falling back` 경고가 있다.
- `KEV_DISPATCH_DEBUG` JSON에 `"rule_baseline_choice":"robot2"`가 있다.
- 최종 배정은 robot2다.
- robot3는 최종 배정되지 않는다.

## 8. S4 - cooldown fallback

1. S3 주문이 완료되고 robot2가 N1로 복귀할 때까지 기다린다.
2. 진행 중 주문·복귀·수동 주행이 없는 것을 확인한다.
3. algorithm 모드로 전환해 공통 배치(N3/N4/N2)를 다시 만든다.
4. AI 모드로 전환한다.
5. S3의 KEV 오류 시각에서 600초가 지나기 전에 `C=1` 주문을 등록하고 시각을 기록한다.

**기대 결과:** KEV 재접속 없이 cooldown 경로를 타고, 같은 ETA 후보군에서 임시 규칙 기준선에 따라 robot2가 배정된다.

**통과 조건:**

- backend 로그에 `KEV dispatch skipped (cooldown ...)`가 있다.
- KEV 서버 접속 또는 새 `/v1/systemone` 요청이 없다.
- 최종 배정은 robot2다.
- S3 오류 시각과 S4 주문 시각의 차이가 600초 미만이다.

600초 이상이면 cooldown이 만료된 것이므로 S4는 무효로 기록하고 S3부터 다시 진행한다.

## 9. 결과 기록표

| 시나리오 | 후보 | 제외 후보 | ETA(초) | 기준선 | KEV choice | confidence | fallback/cooldown | 최종 배정 | 주문 완료 | 비고 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| S0 | robot1·2·3 | 없음 | 2.57 / 4.11 / 11.44 | 해당 없음 | 해당 없음 | 해당 없음 | 없음 | robot1 | COMPLETED | algorithm 모드 비교 기준 |
| S1 | robot1 | robot2·3 | 0.00 / 4.11 / 11.44 | 해당 없음 | 호출 없음 | 해당 없음 | ETA winner | robot1 | COMPLETED | KEV POST 0→0 |
| S2 | robot1·2 | robot3 | 2.57 / 4.11 / 11.44 | robot2 | robot2 | 0.0695 | 없음 | robot2 | COMPLETED | P(robot1)=0.4653, P(robot2)=0.5347 |
| S3 | robot1·2 | robot3 | 2.57 / 4.11 / 11.44 | robot2 | 오류 | 해당 없음 | fallback | robot2 | COMPLETED | 8011 연결 거부, cooldown=600초 |
| S4 | robot1·2 | robot3 | 2.57 / 4.11 / 11.44 | robot2 | 호출 없음 | 해당 없음 | cooldown fallback | robot2 | COMPLETED | 오류 후 약 95초, 505.3초 남음 |

S3 실행을 위해 FMS를 재시작하면서 `logs/backend.log`가 덮어써졌다. S0~S2는 이 문서에 기록한 API·터미널 관측값만 남아 있고, 별도 원본 backend 로그 파일은 보관하지 못했다. S3~S4는 현재 `logs/backend.log`로 재대조했다.

## 10. 실행 결론

S0~S4는 모두 통과했다. ETA 차이가 명확한 경우에는 KEV를 호출하지 않고 코드가 선택했고, ETA가 근소한 경우에는 코드가 만든 후보군 안에서만 KEV 또는 기준선 fallback이 선택했다. KEV 오류와 cooldown에서도 ETA 후보군 밖의 robot3는 선택되지 않았다.

이 결과는 시뮬레이션 단일 회차다. 실제 로봇의 ETA 속도와 주행 중 배터리 소모를 반영한 운영값 검증은 별도로 수행해야 한다.

## 11. D-7 재실행 (2026-10-24)

`goal_node` 해제(`9389c26`), 전압 하한 필터, 백엔드 연결 주기 갱신, 배차 불가 안내(`f4568d8`)가 들어간 코드(`05fc34a`)로 S0~S4를 다시 실행했다. 조건은 3절과 같고, `.env`의 실제 로봇용 값 대신 `KEV_DISPATCH_ETA_SPEED_MPS=0.2`를 실행 시 지정했다. 배치·주문·대기는 API 스크립트로 자동 실행했다.

| 시나리오 | 후보 | 제외 후보 | 기준선 | KEV choice | confidence | fallback/cooldown | 최종 배정 | 주문 완료 | 비고 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| S0 | robot1·2·3 | 없음 | 해당 없음 | 해당 없음 | 해당 없음 | 없음 | robot1 | COMPLETED | algorithm 모드 |
| S1 | robot1 | robot2·3 | 해당 없음 | 호출 없음 | 해당 없음 | ETA winner | robot1 | COMPLETED | KEV `/v1/systemone` 요청 0건 |
| S2 | robot1·2 | robot3 | robot2 | robot2 | 0.0695 | 없음 | robot2 | COMPLETED | P(robot1)=0.4653, P(robot2)=0.5347. D-6과 같음 |
| S3 | robot1·2 | robot3 | robot2 | 오류 | 해당 없음 | fallback | robot2 | COMPLETED | 8011 연결 거부, cooldown=600초 |
| S4 | robot1·2 | robot3 | robot2 | 호출 없음 | 해당 없음 | cooldown fallback | robot2 | COMPLETED | 오류 후 약 42초, 558.2초 남음 |

S0~S4 모두 D-6과 같은 결과로 통과했다. 원본 로그는 `logs/testD7_S0_backend.log`, `logs/testD7_S0-S2_backend.log`, `logs/testD7_S0-S2_kev.log`, `logs/testD7_S3-S4_backend.log`에 보관했다(git 제외).

### 시뮬레이션과 실제 로봇 ETA의 차이

- ETA는 거리 ÷ 고정 속도이므로 후보 순위는 속도와 관계없이 거리 순서와 같다. 실제 로봇에서도 N1→N5(1.356 m, 21.1초)가 N2→N5(1.502 m, 25.9초)보다 빨라 순서가 맞았다. 거리 차이가 큰 S1 같은 경우는 실제에서도 같은 선택이 예상된다.
- 2초 범위가 담는 거리는 시뮬레이션(0.2 m/s) 0.4 m, 실제 설정(0.06 m/s) 0.12 m다. S2 배치(N6까지 0.513 m와 0.822 m)는 시뮬레이션에서는 KEV 후보군이 되지만 실제 설정에서는 robot1이 ETA winner가 된다.
- 시뮬레이션 이동(`mock_data.py`)은 구간을 일정 속도로 이동하고 회전은 yaw만 즉시 바꾼다. 실제 로봇은 회전에서 시간을 많이 쓴다(N7→N5 0.377 m, 약 105° 회전, 평균 10.7초). 거리는 비슷하고 회전 수가 다른 후보는 실제에서 순서가 뒤집힐 수 있으나, 실측은 같은 모양의 경로뿐이라 아직 확인하지 않았다.
- 실제 로봇은 같은 경로에서도 회차마다 약 4초(robot2 18.7~22.6초) 흔들려, 거리 차이가 작은 동률 상황의 실제 도착 순서는 보장되지 않는다.

## 12. D-8 S2 재확인 - 질문 ID 변경 후 (2026-10-26)

로봇 배차 KEV 질문 ID를 `dispatch_{order_uuid}`에서 `dispatch_order_{queue_sequence}_retry_{retry_count}`로 바꾼 코드(`cb56d2a`)에서 S2만 다시 실행했다. 조건은 3절과 같다(`KEV_DISPATCH_ETA_SPEED_MPS=0.2`, `SIM_BATTERY_OVERRIDE="robot1=40,robot2=90,robot3=70"`, `KEV_DISPATCH_DEBUG=1`, 재시작 직후).

| 항목 | D-6·D-7 | D-8 |
| --- | --- | --- |
| question_id | `dispatch_{order_uuid}` | `dispatch_order_1_retry_0` |
| 후보 / 제외 | robot1·2 / robot3 | robot1·2 / robot3 |
| 기준선 | robot2 | robot2 |
| KEV choice | robot2 | robot2 |
| 확률 | robot1 0.4653, robot2 0.5347 | robot1 0.4653, robot2 0.5347 |
| confidence | 0.0695 | 0.0695 |
| 최종 배정 / 완료 | robot2 / COMPLETED | robot2 / COMPLETED |

질문 ID 변경 후에도 선택과 확률이 같았다. 원본 로그는 `logs/testD8_S2_backend.log`(git 제외).

## 13. D-9 후보 범위·최근 작업량 비교 (2026-10-26)

`KEV_DISPATCH_CANDIDATE_SCOPE`(`near_eta`/`all_eligible`)와 `KEV_DISPATCH_RECENT_WORKLOAD`(0/1) 4개 조건을 코드 `b3aacb8`에서 비교했다. 조건마다 FMS를 재시작했고, 공통 조건은 3절과 같다(ETA 0.2 m/s, 근소 범위 2초, 배터리 40/90/70, 작업량 집계 600초). 배치는 조건마다 같다.

- L1: robot1=N3, robot2=N4, robot3=N2, C 주문 1건 (S2 배치)
- L2: robot1=N6, robot2=N4, robot3=N2, C 주문 1건 (S1 배치)
- L3: 충전 노드 N0/N1/N2에서 A 주문 4건을 하나씩 완료·복귀 후 연속 등록. N5 ETA는 robot1 8.26초, robot2 6.78초, robot3 7.51초로 셋 모두 근소 범위 안이다.

| 조건 | 범위 | 작업량 | L1 | L2 | L3 배정 (1~4) | KEV 호출 중 ETA 최단 선택 | fallback |
| --- | --- | --- | --- | --- | --- | --- | --- |
| A | near_eta | 끔 | robot2 | robot1 (ETA winner, KEV 없음) | robot3 ×4 | 0/5 | 0 |
| B | all_eligible | 끔 | robot1 | robot1 | robot2 ×4 | 6/6 | 0 |
| C | near_eta | 켬 | robot1 | robot1 (ETA winner, KEV 없음) | robot2, robot2, robot3, robot3 | 3/5 | 0 |
| D | all_eligible | 켬 | robot1 | **robot3** | robot2 ×4 | 5/6 | 0 |

KEV 확률(주요 회차):

- A L3: robot1 0.2953, robot2 0.3207, robot3 0.3840 (4회 동일). L1은 D-6~D-8과 같은 robot2 0.5347.
- B L3: robot2 0.6006. L2: robot1 0.5222.
- C L3: robot2 0.4362 → 0.4439, 이후 robot2 작업량 2·robot3 0에서 robot3 0.4557 → 0.4425.
- D L2: robot1(ETA 0.00초, 작업량 1) 0.2899, robot3(ETA 11.44초, 작업량 0) 0.3778로 robot3 선택.

관찰:

- 같은 입력에서 KEV 확률은 매번 같았다. 회차별 차이는 입력(작업량) 차이에서만 생겼다.
- `all_eligible`(B)은 작업량이 없으면 6회 모두 ETA 최단 로봇을 골랐다.
- `near_eta`(A)는 지시문대로 근소 후보 안에서 ETA로 재정렬하지 않아, L3에서 ETA 최단이 아니고 배터리도 가장 높지 않은 robot3를 4회 연속 골랐다(편중).
- 작업량을 켜면 `near_eta`(C)에서 robot2·robot3가 2회씩 나뉘어 편중이 줄었다.
- `all_eligible` + 작업량(D)에서는 L2에서 ETA 0.00초인 robot1 대신 작업량이 적은 robot3(11.44초)를 골랐다. 지시문의 "작업량이 확실히 짧은 ETA를 뒤집지 않게 한다"를 지키지 않은 사례로, 근소 범위 밖 후보를 KEV에 맡길 때의 위험이다.
- 4개 조건 모두 KEV 오류·fallback 없이 주문이 완료됐다.

정리: 현재 데이터로는 근소 후보군 안에서만 KEV가 판단하는 `near_eta`에 최근 작업량을 더한 조건(C)이 ETA 보호와 편중 완화를 함께 만족했다. `all_eligible`은 작업량을 함께 줄 때 ETA가 크게 느린 로봇을 고를 수 있어, 운영에 쓰려면 코드가 ETA 차이 상한을 다시 검증해야 한다. 조건별 1회(L3 4건)라 표본이 작고, 시뮬레이션은 회전 시간이 없어 완료 시간은 비교하지 않았다. 원본 로그는 `logs/testD9_{A,B,C,D}_backend.log`, `logs/testD9_{A,B,C,D}_kev.log`(git 제외).

## 14. D-10 all_eligible ETA 상한 검증 (2026-10-26)

D-9의 D 조건(`all_eligible` + 작업량)에서 KEV가 ETA 0초 robot1 대신 11.44초 robot3를 고른 문제를 막기 위해, `all_eligible`에서는 KEV가 고른 로봇의 ETA가 최단 ETA보다 근소 범위(`KEV_DISPATCH_ETA_TIE_SECONDS`, 현재 2초)를 넘게 길면 코드가 거부한다. 거부되면 근소 후보군의 기준선으로 배정하고, 서버 오류가 아니므로 cooldown은 걸지 않는다. 로그에는 `KEV dispatch rejected (ETA guard)`와 `falling back to rule baseline (ETA guard)`가 남는다. `near_eta`는 후보가 이미 범위 안이라 검증하지 않는다.

D 조건으로 L1·L2만 다시 실행했다.

| 배치 | KEV 선택 | ETA 차이 | 결과 | 최종 배정 |
| --- | --- | ---: | --- | --- |
| L1 | robot1 | 0.000초 | 허용 | robot1 |
| L2 | robot3 | 11.437초 | 거부, 기준선 | robot1 |

D-9에서 robot3로 배정됐던 L2가 robot1로 배정됐다. 원본 로그는 `logs/testD10_D_L1L2_backend.log`(git 제외).

이 보호를 넣으면 `all_eligible`도 ETA 차이 2초 밖의 로봇은 배정되지 않으므로, 결과적으로 `near_eta`와 같은 ETA 경계를 갖는다. 차이는 KEV가 범위 밖 후보까지 본 뒤 고르고 코드가 사후에 거부한다는 점이다. 운영 관점에서는 처음부터 범위 안 후보만 주는 `near_eta` + 최근 작업량(C)이 더 단순하고 예측 가능하다.

## 15. D-11 다중 주문 FIFO / KEV 주문 우선순위 비교 (2026-10-26)

주문 10건을 한꺼번에 등록하고 로봇 3대(충전 노드 N0/N1/N2 출발)로 처리했다. 코드 `868e7da`, AI 모드, 로봇 배차는 D-9의 운영 후보 C(`near_eta` + 최근 작업량), ETA 0.2 m/s, 배터리 40/90/70, 장기 대기 보호 120초. 주문 순서는 M01 A, M02 C, M03 A, M04 C, M05 A+C, M06 B, M07 D, M08 A, M09 C, M10 B+D.

| 조건 | 주문 선택 | 전체 처리 시간 | 평균 대기(등록→배정) | 최대 대기 | 평균 완료 | 로봇별 배정 | 배정 순서 |
| --- | --- | ---: | ---: | ---: | ---: | --- | --- |
| F | FIFO | 117.4초 | 37.7초 | 87.9초 (M10) | 62.8초 | robot1 3, robot2 3, robot3 4 | M01→M10 순서대로 |
| K | KEV (`KEV_ORDER_SCHEDULING_ENABLED=1`) | 91.4초 | 26.0초 | 59.5초 (M04) | 48.0초 | robot1 4, robot2 2, robot3 4 | 01, 02, 03, 09, 07, 10, 08, 06, 05, 04 |

KEV 주문 선택 7회의 입력과 선택(대기 시간은 동시 등록이라 같은 값):

| 회차 | FIFO(order_1)의 최단 픽업 ETA | KEV 선택의 ETA | 지시문 "대기가 비슷하면 ETA가 짧은 주문" 준수 |
| --- | ---: | ---: | --- |
| 1 | 6.78 | 6.78 (order_3) | 예 (동률) |
| 2 | 4.11 | 4.11 (order_6) | 예 (동률) |
| 3 | 2.57 | 2.57 (order_4) | 예 (동률) |
| 4 | 10.71 | 6.78 (order_5) | 예 |
| 5 | 4.11 | 8.04 (order_4) | **아니오** |
| 6 | 2.57 | 6.49 (order_3) | **아니오** |
| 7 | 4.11 | 8.04 (order_2) | **아니오** |

관찰:

- 이번 1회에서는 K가 전체 처리 시간(−22%), 평균 대기(−31%), 최대 대기(−32%)가 모두 짧았다. KEV·로봇 배차 오류, fallback, cooldown, 장기 대기 보호(120초) 발동은 두 조건 모두 없었다.
- KEV는 7회 모두 가장 오래된 주문(order_1)을 고르지 않았다. 그 결과 M04가 마지막에 배정됐다.
- 7회 중 3회는 order_1의 ETA가 가장 짧았는데도 다른 주문을 골라 지시문을 지키지 않았다. 세 번 모두 order_1을 피한 경우여서 라벨 위치에 따른 편향 가능성이 있다.
- 따라서 이번 개선이 KEV가 의도한 기준(대기·ETA)으로 고른 결과라고 보기는 어렵다. 동시 등록이라 대기 시간이 같았고, 시뮬레이션에는 회전 시간이 없으며, 조건별 1회다.

다음 확인: 같은 조건 반복으로 처리 시간 차이가 재현되는지, 주문 라벨 순서를 섞었을 때 order_1 회피가 계속되는지, 주문을 시간 간격을 두고 등록해 대기 시간이 다를 때 오래 기다린 주문을 고르는지. 원본 로그는 `logs/testD11_{F,K}_backend.log`, `logs/testD11_{F,K}_kev.log`(git 제외).

## 16. D-12 주문 우선순위 KEV 라벨·순서 재생 (2026-10-26)

D-11 K에서 KEV에 보낸 주문 우선순위 요청 7건(`KEV_ORDER_SCHEDULING_DEBUG`)을 같은 KEV 서버에 다시 보내, 내용은 그대로 두고 표시 방식만 바꿨다. 선택은 원래 FIFO 순번으로 되돌려 비교했다.

- original: 로그 그대로
- reversed: 라벨은 그대로, `choices`와 `pending_orders`의 나열 순서만 뒤집음
- relabeled: 나열 순서는 그대로, 라벨 이름을 거꾸로 붙임(가장 오래된 주문이 `order_N`)

| 변형 | 선택(원래 순번) 1~7 | original과 다른 회차 | 가장 오래된 주문 선택 | 최단 ETA 주문 선택 |
| --- | --- | ---: | ---: | ---: |
| original | 3, 6, 4, 5, 4, 3, 2 | – | 0/7 | 4/7 |
| reversed | 1, 4, 3, 3, 3, 2, 1 | 7/7 | 2/7 | 4/7 |
| relabeled | 3, 4, 4, 2, 4, 2, 1 | 4/7 | 1/7 | 5/7 |

- original 재생은 D-11 로그의 선택과 7회 모두 같았다. 같은 입력이면 결과가 고정된다.
- 1·2위 확률 차이는 회차별 0.002~0.103(대부분 0.04 이하)로, 후보 사이 확률이 거의 같다.
- 내용이 같아도 나열 순서만 바꾸면 7회 모두, 라벨 이름만 바꾸면 4회 선택이 달라졌다. 가장 오래된 주문 회피는 라벨 이름을 바꿔도 대부분 남았지만(1/7), 순서를 바꾸면 줄었다(2/7). `order_1` 이름보다는 나열 위치와 `queue_position=1` 표기의 영향이 섞인 것으로 보인다.

결론: 현재 주문 우선순위 KEV의 선택은 주문 내용(대기·ETA)보다 표시 방식에 크게 흔들린다. D-11의 처리 시간 개선을 KEV 판단의 효과로 볼 근거가 약하다. 운영·추가 실험 전에 다음 중 하나가 필요하다.

1. 확률 차이가 작으면(예: 1·2위 차이 0.05 미만) KEV 선택을 쓰지 않고 FIFO로 둔다.
2. KEV가 고른 주문보다 대기 시간이 확실히 길거나(예: 30초 이상) 최단 ETA가 확실히 짧은(예: 2초 이상) 주문이 있으면 코드가 FIFO로 되돌린다.

원본 결과는 스크래치 `label_bias.json`에만 있다(git 제외).

## 17. D-13 간격 등록 다중 주문 비교 - 보호 규칙 포함 (2026-10-26)

D-11과 같은 주문 10건·로봇 3대 조건에서 주문을 6초 간격으로 등록했다. 코드 `2e6a2e5`(주문 우선순위 보호 규칙: 대기 30초 이상 차이 → 가장 오래된 주문, 대기가 비슷하고 ETA 2초 초과 차이 → ETA 최단 주문).

| 조건 | 전체 처리 시간 | 평균 대기 | 최대 대기 | 평균 처리(등록→완료) | 로봇별 배정 | 배정 순서 |
| --- | ---: | ---: | ---: | ---: | --- | --- |
| F (FIFO) | 118.3초 | 15.2초 | 34.4초 (M10) | 47.1초 | 3 / 3 / 4 | 01→10 순서대로 |
| K (KEV + 보호 규칙) | 112.6초 | 12.7초 | 32.9초 (M10) | 42.7초 | 3 / 4 / 3 | 01~06, 08, 09, 07, 10 |

주문 우선순위 KEV 호출 3회:

| 회차 | 후보 (대기 초, 최단 ETA 초) | KEV 선택 | 보호 규칙 | 최종 |
| --- | --- | --- | --- | --- |
| 1 | order_1 (15.0, 7.51), order_2 (8.9, 11.44), order_3 (2.9, 7.51) | order_3 | 없음 (대기 차이 12.1초 < 30초, ETA 같음) | order_3 (M08) |
| 2 | order_1 (16.1, 8.26), order_2 (10.1, 12.19) | order_2 | **ETA 규칙** (3.93초 > 2초) | order_1 (M07) |
| 3 | order_1 (21.6, 2.57), order_2 (9.6, 2.57) | order_2 | 없음 (대기 차이 12.0초 < 30초, ETA 같음) | order_2 (M09) |

관찰:

- KEV는 이번에도 3회 모두 가장 오래된 주문(order_1)을 고르지 않았다(D-11 0/7, D-12 재생과 같은 경향).
- ETA 규칙이 1회 작동해 ETA가 3.93초 긴 주문 대신 가장 오래된 주문을 배정했다. 대기 규칙(30초)은 주문 간 대기 차이가 6~12초라 한 번도 작동하지 않았다. 장기 대기 보호(120초), KEV 오류·fallback도 없었다.
- 그 결과 M07은 F보다 약 18초 더 기다렸고(29.96초 vs 12.05초), 대신 M08·M09가 먼저 배정됐다.
- 전체 처리 시간은 K가 5% 짧았지만(112.6초 vs 118.3초), D-11(22%)보다 차이가 작고 1회 결과다.
- 측정 한계: 등록 중(처음 약 54초)에는 배정 시각을 등록 직후 한 번만 읽어서, 이 구간에 배정된 주문(두 조건의 M01~M05 등)의 대기 시간은 실제(약 0.5초)보다 길게(약 6초) 기록됐다. 두 조건에 같게 적용되므로 비교에는 쓸 수 있지만 절대값은 아니다.

정리: 보호 규칙은 명백히 나쁜 선택(ETA 2초 초과 차이)을 실제로 막았다. 그러나 KEV는 대기·ETA가 비슷할 때 가장 오래된 주문을 계속 피하고, 30초 대기 기준은 이 정도 부하에서는 작동하지 않아 FIFO보다 공정성이 떨어지는 선택이 남는다. 다음 확인: 대기 기준을 낮춘 조건(예: 10초)과 같은 조건 반복. 원본 로그는 `logs/testD13_{F,K}_backend.log`, `logs/testD13_{F,K}_kev.log`(git 제외).
