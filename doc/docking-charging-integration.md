# ESP32 도킹·CHARGING 통합 운영 계약

기준: `origin/jhlee` `4ee9d80e9414bbeb4b703505204a22948b968944`.
개발: `feature/jhlee-docking-integration`. 소프트웨어 테스트 완료, 실기체 검증 전.
CHARGING은 배터리가 실제로 충전된다는 뜻이 아니라 FMS가 현재 도킹 시도의 완료를 검증했다는 뜻이다.

## 물리·MQTT 계약

| Station | GPIO | 로봇 | 노드 | 토픽 |
|---|---|---|---|---|
| 1 | 18 | robot1 | N0 | `dock/station/1/status` |
| 2 | 19 | robot2 | N1 | `dock/station/2/status` |
| 3 | 21 | robot3 | N2 | `dock/station/3/status` |

Payload는 `{"station":1,"status":1}` 형태다. station/status는 bool·실수·문자열이 아닌 정확한 정수이며 토픽과 station이 일치해야 한다. status=1은 접촉, 0은 해제다. retained 및 MQTT DUP 메시지는 저장하지 않는다. 동일 접촉/해제 레벨이 반복돼도 상태 전환 시각은 갱신하지 않는다.

ESP32는 상태 변경 때만 발행하며 heartbeat와 부팅/재접속 후 재발행을 보장하지 않는다. 브로커 연결은 ESP32 장치 생존을 뜻하지 않는다. 같은 브로커 연결 동안 메시지가 없다는 사실만으로 센서 고장인지 상태 유지인지 구별할 수 없다. 메시지에 장치 세션/순번이 없으므로 DUP 표시가 없는 동일 내용 재전송과 새 이벤트를 완벽히 구분할 수도 없다. 펌웨어는 변경하지 않았다.

`MQTTManager`는 별도 잠금으로 센서 사본을 제공한다. MQTT 콜백은 센서 저장만 하며 로봇 상태·주행 명령을 실행하지 않는다. 브로커 연결/해제마다 generation을 증가시키고 이전 표본을 모두 제거한다. 재접속 직후 모든 센서는 UNKNOWN이다. 새 표본이 들어와도 이전 generation의 도킹 증거는 복구되지 않는다. OMX ACK/Progress/Result와 취소 처리는 기존 코드 그대로 유지한다.

## 도킹 완료

1. 지정 충전 노드를 향한 `NavigationType.CHARGING` 주행을 기존 FMS 경로로 수행한다.
2. 기존 실제 좌표·도착 검증을 거친 뒤 지정 Station/generation/시작 시각을 기록하고 PrecisionDock을 요청한다.
3. ROS GoalStatus가 SUCCEEDED이고 `PrecisionDock.Result.success`도 true여야 한다. 늦은 이전 Action 결과는 기존 auxiliary entry 식별자로 차단한다.
4. 현재 시도 시작 이후 접촉이 시작됐고 같은 generation에서 300ms 이상 안정됐는지 FMS 실행 루프가 확인한다.
5. 연결·DOCKING 상태·충전 목적이 여전히 유효할 때만 CHARGING으로 바꾼다.

Action 기본 제한은 60초, 성공 후 접촉 확인 제한은 10초다. 실패·취소·시간초과·로봇 연결 해제·MQTT 세션 변경·비상정지는 성공으로 처리하지 않는다. 도킹을 시작한 로봇은 검증 완료 전까지 미검증 보류를 유지하므로 비상정지 해제로 자동 IDLE이 되지 않는다.

FMS 재시작 후 지정 스테이션에서 발견된 로봇은 `dock_restart_unverified`로 PAUSED된다. 센서 UNKNOWN이나 이전 접촉으로 CHARGING을 재구성하지 않는다. 보류 사유는 기존 로봇 관제 상태/일시정지 설명에 표시된다.

운영자는 로봇·스위치를 확인한 후 기존 충전소 복귀 명령으로 재도킹한다. 이미 스위치가 눌려 있으면 새로운 접촉 이벤트가 없으므로 스위치 해제 후 재접촉이 필요하다. 해제만으로 출발하거나 보류가 풀리지 않는다. 직접 속도 명령으로 우회할 수 없다. 취소가 끝나지 않았으면 재도킹도 기존 취소 완료 절차를 기다린다. 새로운 도킹 성공 증거가 확인돼야 자동 배차가 다시 가능하다.

## 배차·출발 경계

가용성 필터 후 IDLE → 검증된 CHARGING → 기존 정책상 재배차 가능한 복귀 로봇 순으로 선택한다. 재시도 로봇 회피는 이 우선순위 내부에서 적용한다. busy/order 점유, 최신 위치, ROS 등록, 주행/취소/예약 상태, 유효한 배터리(0 초과 100 이하), 현재 센서 증거를 확인한다. 센서 가용성 정책은 Gateway에 있고 로봇 선택은 기존 order_dispatcher에 남긴다.

기존 `dispatch_order_route`의 개별 경로 생성과 KEV-MAPF 승인 경로를 사용한다. 실기체 경로에는 두 진입점이 있다.

- 일반 경로: Gateway → RealNavigation.request → TrafficManager 예약 → 기존 실행 루프.
- KEV 경로: ParallelAdmission → MapfExecution 원자적 commit → RealNavigation.validate_pending/after_mapf_commit → 기존 실행 루프.

RealNavigation의 선택적 훅으로 양쪽 경계에서 도킹 신뢰를 검증하고, 실제 승인된 요청에만 출발 감시를 넘긴다. 검증된 충전 로봇의 계획용 상태만 IDLE로 표현하며 Robot의 CHARGING 상태와 도킹 증거를 보존한다. Nav2 전송 직전에 센서 신뢰를 다시 확인한다. KEV Score/고정 순위/WAIT/REROUTE/HOLD·충돌 탐지·교착 복구 알고리즘과 TrafficManager/MapfExecution 코드는 변경하지 않았다.

실기체 배차의 잠금은 navigation → order 순서로 통일했다. 기존 MQTT 결과 콜백과 반대 순서로 잠금을 획득하는 교착을 방지한다. 잠금 안에서는 await를 수행하지 않는다.

출발 제한 시간은 요청/예약/전송 시각이 아니라 Nav2 목표 수락 콜백부터 측정한다. 이는 실제 Nav2 실행 시작의 보수적 관측점이며 바퀴 회전 실측은 아니다. 수락 전에 센서 해제를 요구하지 않는다. 수락 이후의 새로운 해제 전환만 인정한다. 이전 해제의 반복 메시지를 해제 증거로 재사용하지 않는다.

수락 후 10초 내 해제가 없거나 MQTT/로봇 신뢰를 잃으면 해당 로봇만 PAUSED 및 기존 Nav2 stop/cancel 경로로 정지한다. 취소 최종 결과 전에는 예약을 해제하지 않는다. 최종 결과 후에도 측정 점유/기존 보호 정책에 따라 정리한다. 취소 시간초과는 자동 성공으로 취급하지 않는다.

짧은 주행이 센서 해제보다 먼저 끝나면 후속 ArUco/OMX 작업을 보류한다. 정상 해제를 확인한 뒤 동일 목표·주문일 때만 후속 작업을 이어간다. 시간초과 시 후속 작업 없이 정지한다.

자동 주문 복귀도 충전 목적을 유지한다. N0/N1/N2에서 IDLE이라는 사실만으로 복귀 완료로 처리하지 않고 검증된 CHARGING을 기다린다. KEV 승인으로 들어온 복귀도 기존 주문 복귀 문맥으로 충전 목적을 유지한다.

## 관제 API·화면

`GET /api/robots/docking-stations`는 Real 모드에서 Station 1~3 배열을 반환한다. Simulation에서는 빈 배열이다.

필드: station, contact(CONTACT/RELEASED/UNKNOWN), received_at_unix, age_s, stable, broker_connected, freshness_policy, robot_id, docking_complete.

contact는 접촉 표본이고 docking_complete는 검증된 도킹 완료이므로 서로 다르다. React는 Real 모드에서만 약 1초 간격으로 조회한다. API 실패/3초 응답 제한 초과는 UNKNOWN으로 표시한다. N0/N1/N2 옆의 고정 표시등은 접촉 초록·미접촉 빨강·UNKNOWN 회색이다. 로봇 좌표와 독립적이며 모드 변경 시 이전 요청 응답을 적용하지 않는다. 기존 지도·주문·로봇·수동 제어·비상정지 UI를 유지했다.

## 설정

`.env.example`의 FMS_DOCK_STATION_MAP, FMS_DOCK_DEBOUNCE_S=0.3, FMS_DOCK_ACTION_TIMEOUT_S=60, FMS_DOCK_CONTACT_TIMEOUT_S=10을 참고한다. 물리 대응이 확정된 현재 환경에서는 기본 station map을 사용한다.

FMS_DOCK_SENSOR_MAX_AGE_S=0은 이벤트 방식이다. 양수는 운영자가 선택한 수신 만료 정책일 뿐 heartbeat를 추가하지 않는다. 현재 펌웨어에서는 장시간 상태 유지 시 UNKNOWN이 될 수 있으므로 기본값 0을 유지한다.

## 실기체 체크리스트 — 전 항목 미실행

- [ ] GPIO18/19/21의 실제 switch 접촉·해제와 Station/robot/노드 대응 확인
- [ ] 실제 브로커 구독, JSON 이벤트, retained 거절 및 OMX 동시 운용
- [ ] N0/N1/N2 관제 표시등의 색상·고정 위치·모드 변경·API 장애 확인
- [ ] PrecisionDock 실제 성공/실패/취소와 Result.success 확인
- [ ] 실제 접촉 300ms 안정화와 CHARGING 전환
- [ ] FMS 시작 시 이미 눌린 스위치: 자동 배차 금지, 재도킹으로 복구
- [ ] 브로커 해제/재연결, ESP32 전원만 꺼진 경우의 관측 한계 확인
- [ ] 3대 실제 주문 접수 → 픽업 → 작업대 → 복귀 → CHARGING
- [ ] IDLE 우선·CHARGING 재배차, 기존 KEV 승인/예약 동작 확인
- [ ] Nav2 수락 후 정상 switch 해제 및 다음 주문
- [ ] 해제 미수신/MQTT 장애 시 안전 정지, 취소 지연·실패 시 예약 보존
- [ ] Action 60초/접촉 10초/해제 10초 시간초과
- [ ] 도킹/출발 중 로봇 연결 해제·비상정지·취소 및 운영자 재도킹 복구

오프라인 검증 결과와 변경 파일별 영향은 `docking-integration-validation-20261026.md`에 기록한다. 실제 장비·브로커·물리 Nav2 동작을 검증한 것으로 해석하면 안 된다.
