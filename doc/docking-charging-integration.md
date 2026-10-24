# ESP32 도킹 센서와 CHARGING 연동 결과

기준일: 2026-10-24. 작업 브랜치: `woozoo`.
작업 시작 시 GitHub 원격과 로컬 HEAD는 모두 `85d6df994497dd6dc66b0717bb14e459c8c399b8`이었다.

## 확정한 운영 정책

- 사용자가 확인한 대응은 robot1 ↔ Station 1, robot2 ↔ Station 2, robot3 ↔ Station 3이다.
- 충전 노드 대응은 기존 robot1 → N0, robot2 → N1, robot3 → N2를 유지한다.
- `CHARGING`은 관제 표시용 도킹 완료 상태다. 실제 충전을 수행하지 않는다.
- 센서는 상태 변화 시 발행하는 것으로 전달받았다. 주기적인 heartbeat를 가정하지 않는다.
- Real 배차 우선순위는 IDLE → CHARGING → 기존 복귀 중 로봇 재배차다.
  기존의 직전 로봇을 뒤로 보내는 재시도 정책은 각 우선순위 안에서만 적용한다.
- 출발에는 기존 Nav2 정지 확인·위치 검사·경로 생성·예약 경로를 사용한다. 별도 Undock 명령은 추가하지 않았다.

## 구현 동작

수동 충전 요청 또는 주문 후 명시적인 충전 복귀 목적의 주행이 끝나면 `DOCKING`으로 전환한다.
PrecisionDock의 ROS 종료 상태와 Result의 `success`를 확인한 후, 이번 도킹 시작 이후 수신한
해당 Station의 접촉이 기본 300ms 이상 유지되면 `CHARGING`으로 전환한다.
센서와 Action의 도착 순서는 모두 지원한다. 작업대 ArUco 정렬은 별도 흐름을 유지한다.

Action 실패, 취소, 비상정지, 로봇 연결 해제, MQTT 연결 세대 변경은 완료 판정을 허용하지 않는다.
Action은 기본 60초, 성공 후 접촉 확인은 기본 10초의 제한 시간을 적용한다.
실패는 기존 `pause(reason_code, detail)` 경로에 기록한다.

충전 위치의 로봇은 연결·기존 작업·위치 최신성·센서 신뢰도와 양수인 유효 배터리 값이 확인되면
IDLE 다음 배차 후보가 된다. 별도의 최소 배터리 잔량 정책은 추가하지 않았다.
새 주문을 시작해도 기존 정지 확인과 경로 예약을 통과한다. 실제 NAVIGATING 시작 후
기본 10초 안에 접촉 해제가 없거나 센서를 신뢰할 수 없으면 기존 정지 절차로 중단한다.
속도 0 명령은 CHARGING을 IDLE로 바꾸지 않는다. 충전 위치에서의 직접 수동 이동은 경로 주행을 안내한다.

자동 복귀는 단순 IDLE 도착으로 완료하지 않고 CHARGING을 기다린다.
복귀 경로 생성 실패도 성공으로 기록하지 않고 `return_error`를 남긴다.
기존 복귀 중 재배차 정책은 유지하며, 그 경우 이전 복귀가 새 작업으로 대체된다.

`GET /api/robots/docking-stations`로 Station 1~3의 접촉·수신 시각·연동 로봇·도킹 완료 여부를 전달한다.
기존 로봇 API와 WebSocket의 상태 전송을 재사용한다. Station API는 Real 화면에서 1초 간격으로 조회한다.
충전 노드 N0·N1·N2 왼쪽 빈 공간의 표시등 세 개가 미접촉은 빨강, 접촉은 초록,
UNKNOWN은 회색으로 표시된다. 별도의 센서 텍스트 패널은 표시하지 않는다.
요청 실패 또는 3초 제한 시간 초과 시 UNKNOWN으로 표시한다. Simulation 응답에는 실제 센서 목록을 넣지 않는다.

## 변경 파일과 영향

| 파일 | 핵심 변경 | 기존 기능 영향 |
|---|---|---|
| `backend/app/config.py`, `.env.example` | Station 매핑, 안정화·제한 시간·선택적 heartbeat 설정 | 브로커 환경변수 유지 |
| `backend/app/services/mqtt_manager.py` | `_on_dock_message`, `get_dock_snapshot`, `dock_contact_valid`, 연결 무효화 | OMX ACK/Progress/Result 경로 유지 |
| `backend/app/models/robot.py` | CHARGING, 도킹 문맥, 일시정지 사유 | 도킹 로봇 재연결 시 자동 IDLE 방지 |
| `backend/app/ros2/ros_gateway.py` | 도킹 시도·완료·출발 접촉 해제 관리 | 기존 Nav2·ArUco·OMX 호출 사용 |
| `backend/app/ros2/fms_ros_node.py` | PrecisionDock Result.success 검사 | Action의 개별 실행 식별·취소 처리 유지 |
| `backend/app/services/real_navigation.py` | 고정 경로 요청의 정지된 계획 상태 정규화 | 위치 동기화·실행 종료·예약 검증 유지; Robot을 IDLE로 변경하지 않음 |
| `backend/app/services/order_dispatcher.py` | IDLE 우선, CHARGING 다음 후보 | 기존 가용성·위치 검사 유지 |
| `backend/app/services/order_manager.py` | 최종 배차 우선순위, 복귀 완료·실패 기록 | 기존 재시도·취소·픽업 정책 보존 |
| `backend/app/services/order_route_dispatcher.py` | `charging_return` 문맥 전달 | Algorithm/AI 경로 선택 및 고정 경로 보존 |
| `backend/app/routers/robots.py`, `websocket.py` | 센서 API와 기존 점유 정보 전달 | 기존 로봇 응답 형태 유지 |
| `backend/app/routers/commands.py` | CHARGING에서 AI 경로 미리보기 허용 | 실제 출발은 기존 실행 경로에서 검사 |
| `frontend/src/api/fmsApi.ts`, `hooks/useRobotFleet.ts` | 센서 조회 및 확인 불가 처리 | 기존 로봇 통신 재사용 |
| `frontend/src/FmsControlApp.tsx`, `frontend/src/WarehouseMap.tsx` | CHARGING 구분, 스테이션 왼쪽 표시등 세 개 | 텍스트 패널 제거, 기존 관제 제어 유지 |
| `start_fms.sh` | 실제 설정 브로커 연결 검사, 외부 브로커일 때 로컬 MQTT 시작 생략 | Zenoh 시작 흐름 유지; 실행하지 않고 구문 검사만 수행 |
| `tests/test_dock_mqtt.py`, `tests/test_charging_docking.py` | 새 센서·상태·배차·예약 테스트 | ROS 전송·센서만 모의 처리 |
| `tests/test_order_flow.py` | 실제 취소 후 복귀 완료를 CHARGING으로 검증 | Simulation 테스트 유지 |

## 소프트웨어 검증

아래 두 묶음에서 총 **147개 통과**했다. 실기체 검증 결과는 아니다.

1. `test_dock_mqtt.py`, `test_charging_docking.py`, `test_order_flow.py`, `test_fixed_route_reservation.py`: **99 passed**.
2. `test_pickup_queue.py`, `test_traffic_joint_actions.py`, `test_traffic_recovery_motion.py`,
   `test_traffic_recovery_dependencies.py`, `test_traffic_situation_adapter.py`: **48 passed**.

커밋 준비 시에는 HEAD에 도킹 변경만 적용한 별도 사본에서도 위 첫 묶음 99개와 프론트엔드 타입 검사·빌드가 통과했다.
기존 미커밋 교통 작업은 이 사본과 커밋 대상에서 제외했다.

`npm run typecheck`, `npm run build`, `bash -n start_fms.sh`, `git diff --check`도 통과했다.
기존 Paho Callback API v1 사용 경고와 기존 TestSelector 수집 경고가 남아 있다.

추가로 전체 `tests` 실행을 시도했지만 전체 통과를 확인하지는 못했다.
기본 환경의 selector benchmark 두 모듈은 CompactRouteGraph 입력을 GeoJSON으로 읽으면서 수집에 실패했다.
명시적으로 원본 `LLM_ROUTE_GRAPH=test.geojson`을 사용하면 수집은 진행된다.
이후 광범위 실행은 대기가 계속되어 중단했다. 별도로 `test_llm_goal_dispatch`의 AnyIO threadpool 대기는
HEAD의 수정 전 `commands.py`를 메모리에서 실행한 상태에서도 제한 시간 초과로 재현했다.
이 기존 문제를 고치기 위한 무관한 코드 변경은 하지 않았다.

실행 시 ROS2 Jazzy 환경과 커스텀 메시지가 설치된 AMR workspace 환경을 먼저 source한다.
실제 테스트에는 프로젝트에서 사용하는 Python 환경의 pytest/Paho/ROS 메시지를 사용했다.
테스트에서 FmsRosNode는 생성하지 않으므로 실제 로봇으로 ROS 명령을 보내지 않는다.

## 실제 장비에서 남은 확인

1. ESP32 펌웨어의 발행 대상과 `FMS_MQTT_BROKER_IP/PORT`가 동일한지 확인한다. 실제 브로커 주소는 전달받지 않았다.
2. Retain 사용 여부, 접촉 해제 이벤트 전달, 재연결 직후 초기 상태 발행 여부를 확인한다.
3. Station별로 접촉/해제를 조작해 올바른 Station만 갱신되는지 확인한다. 수동 스위치 조작만으로 로봇 상태가 바뀌면 안 된다.
4. 수동 충전소 이동으로 Action 성공과 접촉을 확인하고 CHARGING 표시를 검증한다.
5. 접촉 없음·잘못된 Station·Action 실패·MQTT 재연결·취소·비상정지에서 잘못 완료되지 않는지 확인한다.
6. 주문 완료 → 자동 복귀 → 정밀 도킹 → CHARGING까지 확인한다.
7. IDLE과 CHARGING 로봇이 함께 있을 때 IDLE이 먼저 선택되고, 모두 배정되면 CHARGING이 선택되는지 확인한다.
8. CHARGING 출발 시 기존 Nav2 경로 예약이 유지되고 스위치가 해제되는지 확인한다. 300ms 안정화와 각 제한 시간을 실측에 맞게 조정한다.
9. 실제 연결 환경에서 Real/Simulation 전환과 센서 연결 해제 표시를 확인한다.
   격리된 지도 미리보기에서는 스테이션 왼쪽 표시등 세 개의 배치와 모의 접촉 상태에 따른 빨강·초록 전환을 브라우저에서 확인했다.

센서 payload에는 장치 시각·메시지 일련번호·heartbeat가 없다. 따라서 재발행이 없는 동안
ESP32 자체의 생존 여부와 네트워크에서 지연된 개별 메시지의 원래 생성 시각까지 증명할 수는 없다.
현재는 재연결 세대, retain/dup, FMS 수신 시각, 도킹 이전부터 유지된 접촉을 구분해 방어한다.
Retain 접촉은 UNKNOWN으로 표시하며, 이어지는 동일 접촉도 새 접촉 증거로 쓰지 않는다.
이 경우 실제 해제 후 새 접촉이 필요하다. 향후 장치 heartbeat/순번을 추가하면 신뢰성 판정을 보강할 수 있다.

## Git 및 검토

기존 미커밋 변경을 보존한 상태에서 작업했다. 변경 대상 중 일부에는 작업 전 변경도 포함돼 있으므로
전체 `git diff`에는 이번 작업 이외의 내용도 보인다. 시작 시 사본과 비교한 이번 작업의 검토용 패치는
`/tmp/fms-docking-review.patch`에 별도로 저장했다.

최초 구현 보고 시에는 스테이징·커밋·push를 실행하지 않았다. 이후 사용자가 이번 변경의 커밋과 push를 승인했다.
도킹 연동 변경만 `woozoo`에 커밋하고 push하며, 기존 미커밋 작업은 남긴다. main merge나 강제 push는 하지 않는다.
