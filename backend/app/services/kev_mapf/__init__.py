"""[모듈: Kev MAPF 패키지 통합 진입점]
- 구성 모듈:
  1. kev_graph.py: GeoJSON 그래프 래퍼 및 경로 유효성 검증
  2. kev_client.py: Kev AI 서버 통신 클라이언트(LiveKEV) 및 4단계 지침 상수
  3. kev_conflict.py: 다중 로봇 공유 구간 탐색 및 우선순위 순환(Cycle) 감지
  4. kev_engine.py: 4단계 의사결정 기반 MAPF 충돌 해결 코어 엔진(KevMAPF)
  5. kev_adapter.py: FMS/Dispatcher 연동 DTO 변환 및 통합 계획 함수
  6. kev_coordinator.py: Active/Pending 경로 상태 관리 및 WAIT/REROUTE/HOLD 분류기
  7. kev_dispatcher.py: 시뮬레이션 KEV-MAPF 경로 준비·검증 및 FMS 배차 실행 게이트
"""

from .kev_adapter import (
    mapf_robot_from_route_result,
    parse_robot_state,
    plan_mapf_from_route_results,
    preview_order_route_for_mapf,
)
from .kev_client import (
    KEV_ACTION_INSTRUCTIONS,
    KEV_PRIORITY_INSTRUCTIONS,
    KEV_PRIORITY_SCORE_INSTRUCTIONS,
    KEV_WAIT_INSTRUCTIONS,
    KEV_ZONE_INSTRUCTIONS,
    LiveKEV,
)
from .kev_conflict import (
    first_zone_index,
    group_conflict_zones,
    would_cycle,
    zone_candidates,
)
from .kev_coordinator import (
    FmsMapfCoordinator,
    _is_valid_suffix,
    fms_mapf_coordinator,
    is_fms_reviewable_advisory,
)
from .kev_dispatcher import (
    dispatch_prepared_order_route,
    dispatch_simulation_mapf,
    prepare_order_route,
    review_prepared_mapf_candidate,
    validate_prepared_order_route,
)
from .kev_engine import KevMAPF
from .kev_graph import Graph, _node, _node_sort
from .kev_traffic import live_traffic, mapf_traffic

__all__ = [
    "Graph",
    "_node",
    "_node_sort",
    "LiveKEV",
    "KEV_ZONE_INSTRUCTIONS",
    "KEV_PRIORITY_SCORE_INSTRUCTIONS",
    "KEV_PRIORITY_INSTRUCTIONS",
    "KEV_ACTION_INSTRUCTIONS",
    "KEV_WAIT_INSTRUCTIONS",
    "zone_candidates",
    "group_conflict_zones",
    "first_zone_index",
    "would_cycle",
    "KevMAPF",
    "mapf_robot_from_route_result",
    "parse_robot_state",
    "plan_mapf_from_route_results",
    "preview_order_route_for_mapf",
    "FmsMapfCoordinator",
    "fms_mapf_coordinator",
    "is_fms_reviewable_advisory",
    "_is_valid_suffix",
    "prepare_order_route",
    "validate_prepared_order_route",
    "dispatch_prepared_order_route",
    "review_prepared_mapf_candidate",
    "dispatch_simulation_mapf",
    "live_traffic",
    "mapf_traffic",
]
