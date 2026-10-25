# 일반 주문용 MAPF 교통조정 모듈

우리가 구현한 **일반 시뮬 주문의 교통조정 기능**만 모은 폴더다.
다른 쪽에서 병합·재설계를 진행할 때 이 문서부터 읽으면 된다.
별도 Joint Action·여러 대 양보 연구·실험 실행기·실험 결과는 포함하지 않는다.

## 들어 있는 기능

| 파일 | 역할 |
| --- | --- |
| `order_traffic_service.py` | 입력 확인, 다른 로봇의 상태·예약 사본 수집, 기본·우회 경로 검사, 정책별 선택, 최신 예약에 적용, 취소·한도·실패·기록 처리 |
| `order_traffic_strategy.py` | 경로 생성 전에 KEV로 전략 검토 순서를 받고, 경로 생성·검사 후 코드로 예상 완료 시간이 짧은 경로 선택 |
| `traffic_route_provider.py` | 기존 A* 또는 기존 AI 경로 생성기에 회피 노드·엣지를 전달 |
| `common.py` | 상황과 회피 조건을 비교하기 위한 공통 식별값 계산 |
| `integration/existing-fms-support.patch` | 위 기능이 사용하는 기존 FMS의 추가 함수·인자 변경분. 병합 담당자 검토용 |

`__init__.py`는 이 폴더를 Python 패키지로 인식시키는 파일이다.
실제 구현은 이 폴더 한 곳에만 있다. 이전 위치의 같은 이름 세 파일은 기존 import를
유지하는 연결 파일이며, 설정·로그 경로가 서로 다른 두 모듈로 나뉘지 않도록
같은 모듈 객체를 참조한다.

## 현재 동작과 한계

```text
기존 주문 처리·배차
  → 이번 목적지의 기본 경로 생성
  → 이 폴더의 교통조정 호출
  → 다른 로봇의 점유·남은 경로·기존 예약 확인
  → 기본 길 유지 / 제한된 자원을 피하는 우회 검토
  → 예약 사본에서 검사
  → 최신 상태에서 다시 검사·새 요청만 예약
  → 기존 가상 주행기에서 이동
```

요청 로봇은 노드에서 정지해 있어야 한다. 다른 로봇의 기존 주행 계획·예약은 유지한다.
픽업 이동·작업대 이동·복귀에 연결되어 있으며 실기체에는 적용하지 않는다.

| 정책 | KEV와 코드의 역할 |
| --- | --- |
| `KEV` | 코드가 생성·검사한 경로 후보 중 KEV가 최종 선택 |
| `MIN_EXPECTED_TIME` | 코드가 검사된 후보 중 새 요청의 예상 완료 시간이 짧은 경로 선택 |
| `KEV_STRATEGY_ORDER` | KEV가 전략의 검토 순서 제안, 코드는 검사한 후보 중 예상 완료 시간이 짧은 경로 선택 |

이번 작업은 파일 정리다. 최근 논의한 **KEV가 통과 순서·대기·우회 행동을 직접 결정하는
새 구조**를 구현하거나 jhlee와 통합한 것은 아니다.
이전 전체 Joint Action 선택·다중 양보 탐색도 이 일반 주문 모듈의 기능으로 설명하지 않는다.

## 관제에서 호출하는 곳

- [order_route_dispatcher.py](../order_route_dispatcher.py): 경로 생성 후 이 모듈 호출. 기존 알고리즘·실기체 분기는 유지한다.
- [routers/mode.py](../../routers/mode.py): 활성 상태·경로 출처·선택 정책 조회 및 정책 변경.
- [order_manager.py](../order_manager.py): 기존 주문 처리와 배차. 이 모듈로 이동하지 않았다.

진입 함수와 인자는 다음과 같다. 기존 주문 처리기가 이 호출을 담당한다.

```python
from backend.app.services.mapf.order_traffic_service import coordinate_order_route

result = await coordinate_order_route(
    order, goal, start, path,
    graph=graph, source="EXISTING_SHORTEST_PATH", valid=valid,
)
```

`order`에는 `robot_id`, `mode`, `driving_mode`, `operation_mode`, 주문·단계 정보와
필요한 경우 `selector`, `model`이 들어간다. `path`는 출발·목적지를 포함한 노드 ID 경로다.
`valid`는 주문이 취소되거나 단계가 바뀌지 않았는지 확인하는 기존 호출자의 함수다.
시험에서는 `store`, `selector`를 전달해 가상 저장소·모델 대역을 사용할 수 있다.
`store`는 현재 FMS의 내부 필드와 잠금 규약을 사용하므로 임의의 저장소와 바로 호환되지는 않는다.

일반적인 성공 결과에는 `success`, `status`, `route`, `traffic_coordination`이 들어간다.
`status=WAITING`은 예약한 출발 시각을 기다린다는 뜻이다. 시작·목적지가 같으면
이동 없이 성공하고 `route=None`이며, 교통조정 상세가 없는 결과일 수 있다.
입력·상태·한도 오류는 예외로 기존 주문 처리기에 전달하고, 취소는 취소 예외를 유지한다.

## 다른 쪽으로 가져갈 때 필요한 기존 FMS 기능

이 폴더는 **프로젝트 내부 모듈**이다. 폴더만 복사해 독립 실행하는 라이브러리는 아니다.
아래 의존성을 대상 브랜치와 맞춰야 한다. 기존 파일 전체를 우리 버전으로 덮어쓰지 않는다.

| 기존 파일 | 확인할 연결 |
| --- | --- |
| [traffic_manager.py](../traffic_manager.py) | `preview_new_route()`, `_new_route_trial()`, `commit_new_route()`, 기존 `_planning_copy()`·예약·보호 자원 처리 |
| [route_service.py](../route_service.py) | `plan_route()`의 `blocked_nodes`, `blocked_edges` 인자와 `constrained_route_graph()` |
| [llm_route_preview.py](../llm_route_preview.py) | 기존 AI 경로 생성에도 같은 회피 인자를 전달하는 연결 |
| [mock_data.py](../mock_data.py) | `mock_fms`, `_robots`, `_traffic`, `_lock`, 로봇 상태·연결 상태·이동 요청 식별값 |
| [reservation.py](../reservation.py), [occupancy.py](../occupancy.py) | 노드·물리 엣지 예약과 실제 점유 해석 |
| [pathfinding.py](../pathfinding.py), [route_graph.py](../route_graph.py) | 지도·노드 좌표·유향 엣지·거리 계산 |
| [mode_service.py](../mode_service.py), [config.py](../../config.py) | 시뮬·AI 모드 확인, 프로젝트 루트·로그 경로 |
| [route_selector/kev_selector.py](../route_selector/kev_selector.py) | 기존 KEV 서버 클라이언트의 `select_choice()` |

`integration/existing-fms-support.patch`는 기존 기능 공유 커밋
`12d5747de4913c92c1acc797cec9211312f5cb0b`에서 추가한 위 세 공통 파일의 변경분이다.
원래 기반 커밋과 다른 브랜치에서는 바로 적용되지 않거나 기존 변경과 겹칠 수 있으므로
필요한 함수·인자를 옮기는 참고 자료로 사용한다. 자동 적용하지 않았다.
우리 추가 교통조정과 jhlee 교통조정을 같은 이동 요청에 중복 실행하지 않도록
최종 예약·출발을 담당할 곳을 한 곳으로 정해야 한다.

## 기존 실행 설정

폴더 정리로 활성 조건이나 기본값은 바꾸지 않았다.

```bash
export FMS_ORDER_TRAFFIC_ENABLED=1
export FMS_ORDER_ROUTE_SOURCE=EXISTING_SHORTEST_PATH
export FMS_TRAFFIC_SELECTION_POLICY=KEV_STRATEGY_ORDER
```

교통조정 활성값의 코드 기본은 `0`, 정책 기본은 `KEV`다.
재시도·우회 생성·모델 호출 한도와 로그 형식도 유지한다.
로그는 프로젝트 루트의 `logs/order-traffic/`에 기록한다.

## 제외한 코드와 확인 방법

`traffic_joint_actions.py`, `traffic_situation_adapter.py`, `traffic_recovery_*.py` 및
`backend/simulation/`의 연구·비교 코드는 기존 위치에 둔다. 일반 주문 모듈은
Joint Action 연구 파일에서 공통 함수를 가져오던 의존성도 제거했다.

기존 [일반 주문 연결 설명](../../../../doc/order-traffic-integration.md)과
[기능 전체 안내](../../../../doc/kev-mapf-implementation-map.md)는 그대로 참고할 수 있다.
과거 실험 문서의 파일 경로·소스 사본은 당시 기록으로 보존한다.
과거 파일 본문을 직접 읽는 로컬 분석 도구는 새 실제 구현 경로를 사용해야 한다.

관련 자동 검사는 기존 `tests/test_order_traffic_service.py`,
`tests/test_order_traffic_strategy.py`, `tests/test_fixed_route_reservation.py` 등에 있다.
이전·새 import 경로의 상태 공유는 `tests/test_mapf_package.py`에서 확인한다.
실험 실행기와 자동 검사 파일은 이 배포 폴더에 복사하지 않았다.
