# 도킹 통합 완료 보고서 — 2026-10-26

## Git 상태 및 보존

- Repository: https://github.com/E1I6-Logistics/Logistics_AI.git
- 시작 시 `git status --short`, `git branch --show-current`, `git branch -vv`, `git fetch origin` 실행.
- fetch 이후 기준 `origin/jhlee`: `4ee9d80e9414bbeb4b703505204a22948b968944`.
- 기존 `origin/dev`: `86eb8bd8758dd4a4175feab39beab51f127b641d`.
- 원격 백업: `backup/dev-before-docking-20261026`, SHA `86eb8bd8758dd4a4175feab39beab51f127b641d`. push 및 ls-remote 일치 확인.
- 작업 브랜치: `feature/jhlee-docking-integration`.
- 독립 worktree: `/tmp/logistics-docking-integration`. 실제 코드 수정 전에 브랜치 및 기준 SHA 보고 완료.
- 원본 `/home/woozoo/dev/Logistics_AI`의 jhlee HEAD와 기존 untracked 파일을 보존했다.
- 기존 `feature/jhlee-esp32-dock-mqtt-display`는 참고 자료로 유지. 해당 브랜치에서 분기/병합하지 않았다.
- 통합 SHA 및 최종 push/원격 재확인 결과는 최종 응답에 기록한다. 이 보고서를 포함한 feature HEAD가 배포 후보이며 jhlee/main/dev에는 commit/push하지 않는다.
- dev 교체는 승인 전까지 미실행이다. 원격 SHA가 바뀌면 변경 분석과 추가 백업 후 다시 승인받아야 한다.

샌드박스가 .git을 읽기 전용으로 제한하여 fetch/worktree/commit과 원격 검증은 필요한 범위에서 승격 실행했다. 원격 백업 push는 자동 승인 검토가 거부하여 작업을 분리했고, 사용자의 명시적 “백업·feature push 승인” 이후 진행했다. dev 교체 권한으로 해석하지 않았다.

## 함수·호출 경로 비교 결과

| 분류 | 조사 결과/처리 |
|---|---|
| 과거에 있고 jhlee에 없음 | ESP32 센서 저장, 세션 무효화, CHARGING, 센서 검증 배차, 출발 감시, 고정 지도 표시등 |
| jhlee에 이미 존재 | PrecisionDock/ArUco Action, auxiliary 취소와 이전 entry 방어, ROS 실제 도착 확인, OMX 결과 중복 방어, 주문 자동 복귀 |
| 인터페이스 조정 필요 | RealNavigation의 직접 KEV commit 및 일반 request 모두에서 도킹 검증/승인 후 증거 이전, Nav2 수락 시각, 자동 복귀 목적 유지 |
| 선택 재사용 | 85d6df9/85415b3의 MQTT 검증·generation·도킹 시도 모델 및 테스트, 6562013/3e268223의 지도 고정형 표시등 개념 |
| 충돌 위험 및 보완 | 기존 초안의 NAVIGATING 상태 감지만으로 출발 시각 측정 → Nav2 수락 콜백 사용; 취소 전 예약 보존; 재시작 보류; 짧은 주행 후속 작업 보류; navigation→order 잠금 순서 |
| 제외 | 12d5747의 별도 교통조정, 09d93d5에서 제외한 로컬 rollout 설정, 과거 브랜치 전체 merge/cherry-pick, 펌웨어 변경 |

`85d6df9`, `85415b3`, `09d93d5`, `6562013`, `3e2682230067b2d7e83ad05b839768878fbd8320`의 실제 diff 및 과거 문서/테스트를 조사했다. 과거 소스 파일을 현재 파일 위에 통째로 덮어쓰지 않고 도킹 관련 함수와 호출 지점만 이식했다.

## 변경 파일별 영향과 검증

경로는 저장소 루트 기준이다. 표의 검증은 아래 실제 명령 결과에 대응한다.

| 파일 | 기능 및 수정 이유 | 참고 원본 | 기존 영향/검증 |
|---|---|---|---|
| `.env.example` | 물리 대응과 도킹 제한 시간 설정 | 85415b3 | 교통 rollout 설정 변경 없음; 기본값 단위 테스트 |
| `backend/app/config.py` | station map 및 유한한 설정 검증 | 85415b3 | 기존 MQTT/교통 기본값 유지; Python syntax·도킹 테스트 |
| `backend/app/models/robot.py` | CHARGING·보류 사유·연결 복구 시 도킹 보존 | 85415b3 | 기존 상태 enum 확장; 재연결/비상정지 복구 테스트 |
| `backend/app/services/mqtt_manager.py` | thread-safe 센서 저장/strict 타입/retained·DUP 거절/generation/전환 시각 | 85d6df9, 85415b3 | OMX 핸들러 본문 유지; ACK·Progress·Result·중복·연결·동시 읽기 검증 |
| `backend/app/ros2/fms_ros_node.py` | PrecisionDock 실제 Result.success 확인, Nav2 수락 콜백 | 85415b3 + 신규 | 기존 Action entry·취소 계약 유지; 거짓 success/이전 결과 테스트 |
| `backend/app/ros2/ros_gateway.py` | 도킹 시도·완료·배차 정책·출발 감시·restart 보류·후속 작업 보류 | 85415b3 + 신규 | 기존 stop/cancel/request/ArUco/OMX 경로 활용; 상태·출발·예약·3대 roundtrip 테스트 |
| `backend/app/services/real_navigation.py` | 검증/승인/전송 경계의 선택적 훅 및 계획용 충전 상태 대응 | 85415b3의 상태 정규화 참고 + 신규 | KEV 직접 commit도 검증; 기존 planner·reservation·재시도 로직 유지; Real/ParallelAdmission/MAPF/예약 회귀 |
| `backend/app/services/order_dispatcher.py` | 도킹 가용성 필터와 IDLE→CHARGING→복귀 우선순위 | 85415b3 + 신규 | 기존 busy/order/pose/navigation 조건 유지; 배차 및 중복 방지 회귀 |
| `backend/app/services/order_manager.py` | 배차 tier 유지·충전 검증 후 복귀 완료·잠금 순서 통일 | 85415b3 + 신규 | 픽업/작업대 선택 유지; 취소/재시도/복귀·잠금 순서 테스트 |
| `backend/app/services/order_route_dispatcher.py` | 자동 복귀의 charging_return 인자 전달 | 85415b3 | 현재 KEV dispatch 위임 경로 보존; 일반 복귀와 직접 MAPF 복귀 목적 테스트 |
| `backend/app/routers/commands.py` | 검증된 CHARGING의 경로 미리보기 허용 | 85415b3 | 이동 시작 권한 아님; 최종 승인/전송 경계 재검증 |
| `backend/app/routers/robots.py` | Real 전용 docking-stations API | 85415b3 | simulation은 []; station/robot 대응 API 테스트 |
| `frontend/src/api/fmsApi.ts` | 도킹 DTO/API/3초 응답 제한, 기존 MapInfoDto.resolution 타입 누락 보완 | 85415b3 + 신규 | 기존 지도 런타임 동작 변경 없이 TS 오류 해소; typecheck/build |
| `frontend/src/hooks/useRobotFleet.ts` | Real만 순차 polling, 실패시 UNKNOWN, 늦은 응답 방어 | 85415b3 | 기존 로봇 연결/WS 처리 유지; typecheck/build |
| `frontend/src/FmsControlApp.tsx` | 지도에 Real 센서 props 전달 | 3e268223 | 기존 주문·비상정지·수동 UI 유지; typecheck/build |
| `frontend/src/WarehouseMap.tsx` | N0/N1/N2 고정 표시등 및 contact/완료 구분 tooltip | 6562013 | 로봇 위치 및 기존 지도 레이어 보존; typecheck/build, 브라우저 시각·실장비 반영은 미검증 |
| `tests/test_dock_mqtt.py` | 입력/세션/중복/OMX/스레드 안전 테스트 | 85415b3 + 신규 | retained가 live sample을 덮지 않는 개선 계약 검증 |
| `tests/test_charging_docking.py` | 상태/배차/출발/원자적 예약/정지·재도킹/3대 시나리오 | 85415b3 + 신규 | ROS 송신·센서는 대역; 실제 KEV/예약 실행 코드는 사용 |
| `tests/test_order_flow.py` | 기존 취소 복귀 대역에 charging_return 및 CHARGING 완료 계약 반영 | 85415b3 참고 | 기존 취소→정지 확인→알고리즘 복귀 순서 검증 유지 |
| `doc/docking-charging-integration.md` | 운영 계약·제약·실기체 체크리스트 | 과거 동명 문서 참고, 재작성 | 미검증 항목을 별도 명시 |
| `doc/docking-integration-validation-20261026.md` | 조사/영향/검증/승인 보고 | 신규 | 이 보고서 |

도킹 통합 diff에서 `backend/app/services/kev_mapf/`, `traffic_manager.py`, `mapf_execution.py`는 변경하지 않았다. KEV 점수·평가 횟수·High/Low·WAIT/REROUTE/HOLD·충돌/교착 판단은 기준 jhlee 그대로다.

## 기능별 통합 상태

| 기능 | 구현 | 검증 범위 |
|---|---|---|
| MQTT | 구현 완료 | 오프라인 검증 완료, 실제 ESP32/브로커 미검증 |
| 도킹 관제 | 구현 완료 | API·TS·build 완료, 실제 화면/센서 반영 미검증 |
| CHARGING | 구현 완료 | 성공/실패/취소/오류/시도 관계/안정화/시간초과 검증, 실제 Action 미검증 |
| 배차 | 구현 완료 | IDLE 우선/CHARGING 가용성/배터리/중복/기존 재시도 검증 |
| 출발·해제 감시 | 구현 완료 | Nav2 수락 이후 측정/정상 해제/사전 해제 거절/안전 정지/예약 보존 검증 |
| 복귀 처리 | 구현 완료 | 충전 목적·CHARGING 완료·실패 보류·정지 후 재도킹 검증 |
| KEV-MAPF 연동 | 구현 완료 | 기존 회귀 및 도킹 원자적 commit/3대 실행 검증; 실제 모델/로봇 연동 미검증 |

## 실행 명령 및 결과

ROS 타입을 사용하기 위해 아래 환경을 로드했다. `/home/woozoo/venv/robot/bin/python`은 기존 FastAPI/pytest/paho 의존성을 사용한다.

```bash
source /opt/ros/jazzy/setup.bash
source /home/woozoo/dev/Logistics_AMR/install/setup.bash
/home/woozoo/venv/robot/bin/python -m pytest -q --timeout=20 \
  tests/test_dock_mqtt.py tests/test_charging_docking.py \
  tests/test_real_kev_mapf.py tests/test_kev_parallel_admission.py \
  tests/test_fms_mapf_coordinator.py tests/test_fixed_route_reservation.py \
  tests/test_order_flow.py tests/test_kev_recovery.py \
  tests/test_kev_admission_recovery.py tests/test_kev_mapf_integration.py \
  tests/test_kev_score_priority.py tests/test_order_stress_test.py \
  tests/test_kev_traffic_route.py tests/test_kev_code_selection.py \
  tests/test_scenario_mapf.py
```

최종 결과: **260 PASS, 0 FAIL, 0 SKIP, 4.57초**. paho Callback API v1의 기존 폐기 예정 경고 85건은 프로토콜 마이그레이션 범위 밖이다.

검증한 동작:

- Station 1~3, 접촉/해제, JSON/정확한 타입/Station 불일치, retained/DUP, 재접속 UNKNOWN, 동일 레벨의 과거 증거 재사용 방지, 스레드 안전성, OMX ACK/Progress/Result.
- Action 성공과 접촉의 순서, 300ms debounce, 과거/다른 Station 접촉, Action/접촉 시간초과, 취소·비상정지·연결 해제, 이전 Action 결과 무효화.
- IDLE 우선, CHARGING만 가용한 경우, 잘못된 배터리, 재시도 우선순위, 중복 배차 방지, 픽업/작업대/자동 복귀 및 실패 복구.
- Nav2 수락 전 대기에서 10초를 소비하지 않음, 수락 후 새로운 해제 이벤트, 짧은 경로의 후속 작업 보류, MQTT 장애, 정지 완료 전 예약 유지, 정지 후 복구.
- 3대 CHARGING → 원자적 MAPF 예약 → 3개 동시 ROS 목표 → 각 Station 해제 → 목적지/ArUco → 복귀 → 새 접촉/PrecisionDock → CHARGING.
- KEV 점수 우선순위, High/Low, 병렬 승인, Coordinator, 실제 실행기, 고정 경로/예약, 교착 복구, 주문 흐름/스트레스 제어, 시나리오 MAPF.
- Real 배차에서 navigation 잠금을 order 잠금보다 먼저 획득함.

3대 센서/실행기 테스트는 ROS 송신과 센서를 제어한 대역을 사용했다. 실기체 주문 3대의 픽업→작업대→복귀 전체를 일괄 실측한 결과는 아니다. 주문 단계는 기존 order_flow 테스트군에서 별도로 검증했다.

```bash
python3 -m compileall -q backend/app tests/test_dock_mqtt.py tests/test_charging_docking.py tests/test_order_flow.py
git diff --check
cd frontend
npm run typecheck
npm run build
```

전부 PASS. Vite 8.3.0, 27 modules, build 성공. worktree에서는 기존 frontend/node_modules를 임시 연결해 사용했으며 commit 대상에 포함하지 않는다.

## 초기 실패 및 재검증

- 기본 Python에 fastapi 없음: 최초 도킹 테스트 60 PASS/1 FAIL. 기존 robot venv 사용 후 해결.
- worktree의 node_modules 부재로 TS 타입 해석 실패: 기존 의존성 임시 연결로 해결.
- 최초 Vite 호출에서 잘못된 `--root` 옵션 사용: frontend 디렉터리의 정식 npm scripts로 재실행.
- 기준 jhlee에도 `MapInfoDto.resolution` 타입 누락: 실제 API의 resolution을 DTO에 추가한 후 typecheck PASS.
- 샌드박스 안에서 기존 asyncio 테스트가 종료 처리 중 정지/시간초과: 수정하지 않은 origin/jhlee에서도 같은 스택 재현. 샌드박스 밖 동일 환경에서 지연 없이 완료. 최종 260개는 실제 완주한 결과다.
- 이전 취소 복귀 테스트 대역이 charging_return을 받지 못해 1 FAIL: 새 도킹 완료 계약에 맞춰 대역을 수정하고 재검증 PASS.
- 신규 테스트 작성 중 waits 타입·예약 출발 시각 설정 오류는 fixture를 수정했다. 구현의 예약 게이트를 우회하지 않고 최종 PASS.

## 남은 위험·미검증

1. 실 ESP32, 실제 MQTT 브로커, Nav2/PrecisionDock 물리 실행, 브라우저 시각 확인은 미실행이다. 운영 문서의 실기체 체크리스트를 현장에서 수행해야 한다.
2. heartbeat와 ESP32 재접속 시 재발행이 없어 브로커가 살아 있는 동안 ESP32만 정지하는 경우 즉시 알 수 없다. FMS 재시작 후 눌린 스위치는 재도킹/새 이벤트가 필요하다.
3. CHARGING 증거는 메모리에만 있다. 재시작하면 자동 출발하지 않고 보류한다. 연결 복구만으로 보류를 해제하지 않는다.
4. Nav2 수락은 실제 바퀴 회전을 실측한 시각이 아니다. 수락부터 10초라는 보수적 경계의 현장 확인이 필요하다.
5. stop/cancel 최종 결과가 없으면 기존 예약 보호가 남아 후속 주문이 대기할 수 있다. 안전을 위해 자동 해제하지 않는다.
6. 기존 dev와 기준 jhlee가 크게 다르다. dev 교체에는 도킹뿐 아니라 검증된 jhlee의 FMS/KEV/주문 기반 교체도 포함된다. 기존 dev 전용 코드가 사라질 수 있고, dev 의존 작업 브랜치/운용 환경의 재동기화가 필요하다.

## dev 교체의 승인 경계

최종 응답의 후보 HEAD·현재 dev SHA·백업을 보고 사용자의 명시적인 최종 승인을 기다린다. 승인 후에도 dev를 다시 fetch하고, 변경됐다면 분석·추가 백업한다. 승인된 후보 SHA 및 예상 dev SHA에 대한 `--force-with-lease` 또는 동등한 조건부 ref 갱신만 수행한다. 현재 dev는 변경하지 않았다.
