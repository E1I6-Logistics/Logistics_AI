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
