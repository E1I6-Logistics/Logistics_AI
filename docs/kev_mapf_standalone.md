# KEV MAPF API

실제 구현은 `backend/app/services/kev_mapf/`에 있다. 이전 독립 실행용 호환 모듈은 제거했으며, 현재 코드는 패키지의 실제 API를 직접 사용한다. 전체 흐름과 KEV/FMS 역할 구분은 [전체 구조](kev_mapf_flowchart.md)에 정리한다.

## 운용 진입점

| 함수·클래스 | 역할 |
| --- | --- |
| `kev_dispatcher.dispatch_mapf()` | 주문·수동 AI 이동의 경로 준비, 출발 전 MAPF와 FMS 반영 |
| `kev_adapter.plan_mapf_from_route_results()` | 준비된 여러 경로를 입력받아 KEV MAPF 자문 계획 생성 |
| `kev_coordinator.FmsMapfCoordinator` | Active·Pending 요청 등록과 상태 관리 |
| `kev_parallel_admission.ParallelAdmission` | 공통 계획의 여러 경로·예약 검증 및 일괄 반영 |
| `kev_recovery.KevRecovery` / `KevRecoveryPlanner` | 주행 감시와 주입 가능한 KEV 복구 판단 |
| `services.mapf_execution.MapfExecution` | 실제·모의 실행 공통 상태·예약 반영·복구 계약 |

우선순위는 `score` 전용이다. `LiveKEV(model="kev-08b")`가 관련 로봇을 요청 한 번으로 점수 평가하며, 구역·행동·대기 위치·경로 선택은 필요한 선택 호출을 사용한다. 기존 독립 실행용 `repo`, `deadline`, `max_steps` 초기화 인자는 제거했다.

테스트 전용 직접 출발 함수와 별도 주문 preview 래퍼도 제거했다. 주문·수동 이동은 공통 운용 진입점을 사용하고, 초기 경로 생성은 기존 `llm_route_preview.preview_llm_route()`를 사용한다.

## 검증

- 회귀 테스트: `tests/test_kev_mapf_integration.py`, `tests/test_kev_parallel_admission.py`, `tests/test_kev_recovery.py`.
- 실제 KEV와 실행 중 시뮬레이션 FMS 검증: `tools/verify_kev_mapf_fms.py`.
- 검증 도구는 시뮬레이션 로봇의 위치와 주문을 변경하므로 시험할 FMS에서 명시적으로 실행한다.
