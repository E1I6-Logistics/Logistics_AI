# KEV 배차 통합 및 검증 — 2026-10-27

## 기준과 작업 범위

- 기준: `origin/dev` = `4e96e024c1e7209fb7b5ea4d9097accd9f552497`.
- 참조: `origin/heeyeon` = `5e6de05b885d3a4b483cf6e677c09bcdbe1ca4d7`.
- 작업 브랜치: `codex/heeyeon-dispatch-integration`.
- heeyeon 전체를 merge하지 않고 near_distance 배차와 전압 수집을 dev에 연결했다.
- `kev_dispatch_client.py`는 위 heeyeon 커밋에서 가져왔다. 선택 입력·지침·대체 규칙은 같은 커밋의 OrderManager를 기준으로 별도 모듈에 구현했다.
- 원본 checkout의 미커밋 파일은 건드리지 않았다. dev 변경·원격 push·장비 구동은 이 작업에 포함하지 않는다.

## 확정한 동작

1. dev의 연결·위치 최신성·작업/복구 중 여부·도킹 출발 가능 검사로 후보를 수집한다.
2. AI 배차는 IDLE/CHARGING/복귀 중 전체 유효 후보를 거리로 비교한다. 상태별 강제 우선순위는 없다. CHARGING은 현재 실제 모드에서만 지원한다.
3. 재시도 주문은 다른 유효 후보가 있으면 직전 실패 로봇을 제외한다. 앞 주문의 픽업과 겹칠 때 복귀 로봇을 가로채지 않는 dev 조건도 보존한다.
4. 복합 주문도 실제 첫 방문 `pickup_nodes[0]`까지 route graph 거리로 비교한다. 최단 거리 +0.12m 이내만 남긴다.
5. 한 대면 KEV 없이 선택한다. 두 대 이상이면 KEV Choice API에 요청한다. KEV는 IDLE·비복귀를 선호하도록 지시받지만 코드가 선택을 강제하지는 않는다.
6. 배차용 KEV의 전체 응답 대기 제한은 기본 3초다. 모델 정보 조회와 선택 요청을 합친 기한이며, 경로 생성·MAPF의 시간 제한과 독립적이다.
7. 오류·시간 초과·후보 외 응답은 최신 검사와 거리 필터를 다시 거친 후보군의 코드 규칙으로 대체한다. 기본 30초 동안 해당 환경의 배차용 KEV를 건너뛰며 주문 배차·기존 주행은 계속한다. 이후 다음 배차에서 다시 시도한다.
8. 대체 규칙은 heeyeon과 같이 IDLE·비복귀 우선, 현재 배터리 잔량 내림차순, 로봇 ID 오름차순이다. CHARGING과 복귀 중 사이에는 별도 강제 순서를 추가하지 않았다.
9. 실행 가능한 대기 주문은 등록 순서로 처리한다. 첫 KEV 응답을 기다리는 동안 뒤 주문을 먼저 배차하지 않는다. 실행 가능한 후보/경로가 없는 주문은 대기시키고, 다른 실행 가능한 주문은 기존처럼 진행한다. 실험용 주문 순서 선택·최근 작업량 판단은 추가하지 않았다.

## dev에서 보존한 실행 경계

- 주문 상태 변경과 실제 후보 검사는 기존 `navigation -> order` 잠금 순서를 유지한다.
- HTTP는 별도 asyncio 작업/스레드에서 수행한다. 매 tick에서 HTTP 응답을 기다리지 않으므로 시뮬레이션 갱신과 기존 실제 주문 처리가 진행된다.
- 배차 전용 executor는 한 스레드로 제한하고 경로/MAPF의 기본 executor와 분리했다. 취소해도 즉시 중단되지 않는 HTTP가 있어도 배차 호출이 병렬로 누적되지 않으며, 대기 중인 취소 요청은 실행하지 않는다.
- 매 tick에서 후보와 거리를 다시 계산한다. 모드 변경 세대, 주문/재시도, 후보 상태·노드·목표·주행 식별자, 그래프가 달라지면 이전 선택을 폐기한다. 전압·도킹 조건도 현재 값으로 재검사한다.
- 선택 시점에 주문을 할당한다. KEV 요청 중에는 로봇/통로를 예약하지 않는다. 취소되거나 오래된 응답은 주문·로봇을 변경하지 못한다.
- 정상 완료된 배차 client만 재사용한다. 시간 초과한 HTTP worker의 늦은 응답은 반영하지 않는다. HTTP 작업 취소는 ROS/MQTT 콜백 스레드에서도 asyncio 루프를 통해 전달한다.
- 복귀 중이면 기존 `WAITING_RETURN_STOP` 절차로 정지 완료 후 새 주문을 실행한다.
- 충전 중이면 기존 도킹 이력·센서 접촉 검사와 실제 출발/접촉 해제 감시를 유지한다. KEV 선택만으로 주행하거나 도킹 증명을 지우지 않는다.
- **경로·MAPF·통행 예약·도킹 실행 코어는 수정하지 않았다:** `order_route_dispatcher.py`, `kev_mapf/kev_dispatcher.py`, `mapf_execution.py`, `real_navigation.py`, `traffic_manager.py`, `ros_gateway.py`는 기준 dev와 동일하다.
- 거리 계산 불가/잘못된 거리는 해당 후보를 제외한다. 유효 거리 후보가 없으면 그 주문은 대기한다. heeyeon의 일부 예외 경로처럼 기존 알고리즘으로 거리 제한을 우회하지 않는다.

## 모드와 설정

| 환경/운영 모드 | 기본 배차 | 경로·MAPF |
|---|---|---|
| 시뮬-AI | KEV near_distance | 기존 dev AI 경로·MAPF |
| 실제-AI | 기존 규칙 — KEV 배차 기본 비활성 | 기존 dev AI 경로·MAPF·도킹 |
| 실제/시뮬 알고리즘 | 기존 규칙 | 기존 dev 알고리즘 |

`.env.example`에 다음을 명시했다. 실제 `.env`는 수정하지 않았다.

```dotenv
FMS_SIM_KEV_DISPATCH_ENABLED=1
FMS_REAL_KEV_DISPATCH_ENABLED=0
KEV_DISPATCH_DISTANCE_TIE_METERS=0.12
KEV_DISPATCH_TIMEOUT_SECONDS=3
KEV_DISPATCH_COOLDOWN_SECONDS=30
KEV_DISPATCH_DEBUG=0
FMS_MIN_DISPATCH_BATTERY_VOLTAGE=11.2
FMS_BATTERY_VOLTAGE_WINDOW_S=60
```

실제 KEV 배차는 별도 검증 후 `FMS_REAL_KEV_DISPATCH_ENABLED=1`로 바꾸고 백엔드를 재시작한다. 배차를 끄더라도 기존 AI 경로·MAPF는 유지한다. 배차의 cooldown이 경로·MAPF의 KEV를 끄지는 않는다.

## 배터리

- ROS `BatteryState.voltage`의 유효한 양수·유한 측정값을 저장한다. 잔량(%)과 전압(V)은 별개다.
- 최근 60초의 최저값이 11.2V 미만이거나 유효 기록이 없으면 새 주문 배차에서 제외한다. 유효 측정값이 하나라도 있으면 60초를 채우지 않아도 판정한다. 연결이 끊기면 전압 이력을 지운다.
- **heeyeon처럼 전압 필터는 알고리즘·AI 배차 모두 적용한다.** 알고리즘의 선택 우선순위는 기존대로이며, 전압 필터는 새 공통 배차 조건이다. 이미 수행 중인 주문을 이 필터로 중단하지 않는다.
- 11.2V와 60초는 합의한 초기 설정이며 실제 장비 적정성 검증 결과로 취급하지 않는다.
- 시뮬레이션은 heeyeon과 같이 기본 12V 고정값이다. 실제 60초 수신 이력/방전 시뮬레이션은 아니다. `SIM_BATTERY_VOLTAGE_OVERRIDE`와 `SIM_BATTERY_OVERRIDE`로 재현할 수 있다.
- 로봇 HTTP/WebSocket에 전압과 전압 차단 사유를 제공한다. 로봇 상세에 전압·전압 부족/기록 없음, 주문 상세에 배차 대기 이유를 표시한다.

## 검증

자동 테스트는 실제 ROS 노드·장비·MQTT 브로커·KEV 서버를 구동하지 않고 실행했다. 기존 AMR install의 사용자 메시지만 테스트 환경에 적용했다. 이후 실서버 추가 검증은 아래에 별도로 기록했다.

- 신규 KEV 배차 테스트 **38개 통과**.
- 신규 배차 + 기존 주문/도킹 집중 검사 **129개 통과**.
- 전체 **368 passed / 6 failed / 0 skipped**. 여섯 실패는 아래의 기준 dev에서도 재현되는 기존 실패다.

```bash
source /home/woozoo/dev/Logistics_AMR/install/local_setup.bash
/home/woozoo/venv/robot/bin/python -m pytest tests -q --disable-warnings --tb=line
```

검증 내용: 거리 경계/복합 주문 첫 픽업, 가까운 복귀/CHARGING 선택, 도킹 접촉 상실·재연결·전압 이력 소실 제외, 응답 후 접촉 상실 제외, KEV 호출 중 잠금 해제·tick 진행, 오류/전체 기한/늦은 응답/30초 복귀, 같은 후보 ID의 노드 변경/모드 왕복/그래프 변경, FIFO·중복 할당·취소, ROS 전압 전달·이력 만료, 기존 주문 스트레스 시나리오, 배차 후 기존 MAPF 게이트를 거치는 픽업 2곳→작업대→복귀 연결.

후보 변경으로 요청을 반복 폐기해도 HTTP 동시 실행이 하나로 제한되고, 한 스레드로 제한한 경로용 기본 executor가 계속 실행되는 것도 확인했다.

전체 테스트에 남은 기존 실패 6건은 변경 없는 dev의 `git archive` 기준본 `/tmp/logistics-kev-baseline.ru0Mlm`에서도 동일하게 재현했다. 해당 세 파일만 기준본에서 실행하면 **14 passed / 6 failed**다.

- `test_selector_iterative_route_benchmark.py`: 기존 선택기 호출/경로 기대값 불일치 4건.
- `test_selector_latency_benchmark.py`: `test_int.geojson` 기대값과 현재 `test.geojson` 불일치 1건.
- `test_selector_question_types.py`: 같은 그래프 이름 불일치 1건.

이 여섯 건을 통과시키기 위해 기존 경로 로직이나 테스트 기대값을 변경하지 않았다. 전체 결과가 모두 통과했다고 보고하지 않는다.

TypeScript `npm run typecheck`, Vite `npm run build`, Python 구문 검사(76개 파일), `git diff --check`도 통과했다.

### 오프라인 선택 재생

N0~N4 중 겹치지 않는 세 위치의 순열 60개 × 첫 픽업 N5/N6 = 120개 정적 입력에서 기존 시뮬 알고리즘과 새 near_distance의 **규칙 대체 선택**을 비교했다. 실제 KEV 선택·주행·충전·충돌 실험은 아니다.

- 첫 픽업 거리 합계: 기존 187.433m → 새 선택 127.129m.
- 72개 입력에서 감소, 48개 동일, 증가 0개.
- 거리 후보가 한 대인 입력 96개(KEV 호출 불필요), 복수 후보 24개.
- 이 PC의 후보 준비 시간: 중앙값 2.447ms / 95백분위 2.669ms. 일반 성능 보장값은 아니다.

실제 KEV의 선택 품질·동시 경로/MAPF 요청 시 지연·주문 완료 시간 개선을 정량 비교하는 시험, 실제 장비의 배터리 임계값 및 도킹 출발은 후속 검증 항목이다.

## 실서버 추가 검증 — 2026-10-27

사용자 요청으로 실제 FastAPI/Vite와 GPU KEV 서버를 연결해 시뮬레이션 주문을 실행했다. 기존 5173/8000 서버는 유지했다.

- 검증 화면: `http://localhost:5175/`, 백엔드: `http://127.0.0.1:8002`.
- KEV: 기존 `127.0.0.1:8011`, `jaredpalmer/kev-0.8b@v1.0`, CUDA/torch/bfloat16.
- 장비와 분리: ROS domain 92 / LOCALHOST, 전용 MQTT `127.0.0.1:1885`, 컨테이너 `fms-kev-sim-mqtt-20261027`.
- 실제-AI 배차는 계속 비활성화. 원격 dev/push/물리 로봇 명령은 수행하지 않았다.
- `KEV 실서버 검증 A`: robot2 선택, N5 픽업→N3 작업대→N1 복귀 완료.
- `KEV 두 후보 배차 검증`: 가상 robot1을 N3으로 이동시켜 근소 거리 후보를 만든 뒤 실행. robot2 선택, N5 픽업→N4 작업대→N1 복귀 완료. 결과는 `logs/kev-live-first-run.json`에 저장했다.
- 로그 설정을 보완해 요청/응답을 재확인했다. `robot1` 1.299m, `robot2` 1.356m(차이 0.057m)만 KEV에 전달됐으며, 응답은 `choice=robot2`, 확률 robot1=0.474/robot2=0.526, 오류 없음이었다. `KEV 두 후보 실제응답 확인` 주문에 반영됐다.
- 재확인 주문도 N5 픽업→N4 작업대→N1 복귀까지 완료했다. 결과는 `logs/kev-live-confirmed-run.json`, 모델 요청/응답은 `logs/kev-integration-backend-confirm.log`에 보존했다.
- 모델이 선택한 로봇이 가장 짧은 거리의 로봇과 다르더라도 합의한 0.12m 후보 범위 안의 선택이다. 이 한 건을 KEV의 선택 품질 우수성 증거로 취급하지 않는다.
- 화면 상단의 고정된 ‘배차 알고리즘’ 문구를 백엔드 `feature_engines` 응답에 따라 ‘배차 KEV/알고리즘’으로 표시하도록 수정했다. 배차 debug 로그는 기본 uvicorn 로그에서도 보이게 했다.
- 이 표시 보완 후 TypeScript 검사·Vite 빌드, 배차/주문/도킹 집중 테스트 129개를 다시 통과했다.

현재 서버는 확인을 위해 실행 상태로 유지한다. 종료할 때는 `logs/kev-integration-backend.pid`, `logs/kev-integration-frontend.pid`의 해당 검증용 프로세스만 종료하고 전용 MQTT 컨테이너를 중지한다. 기존 5173/8000/8011 서비스는 별개다.
