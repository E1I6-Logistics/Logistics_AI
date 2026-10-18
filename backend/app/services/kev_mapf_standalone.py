"""[모듈: kev_mapf_standalone.py - KEV-MAPF 하위 호환성 파사드(Facade)]
- 역할: 기존 외부 호출자(`fms_mapf_coordinator`, `order_route_dispatcher`, 테스트 코드 등)와의
  인터페이스 호환성을 100% 유지하면서, 실제 구현은 `kev_mapf/` 하위 모듈들로 위임한다.
- 하위 모듈 구성:
  - kev_graph.py: 유향 GeoJSON 그래프 래퍼 및 경로 유효성 검증
  - kev_client.py: Kev API 통신 클라이언트(LiveKEV) 및 4단계 지침 상수
  - kev_conflict.py: 다중 로봇 공유 구간 탐색 및 우선순위 순환(Cycle) 감지
  - kev_engine.py: 4단계 의사결정 기반 MAPF 충돌 해결 코어 엔진(KevMAPF)
  - kev_adapter.py: FMS/Dispatcher 연동 DTO 변환 및 통합 계획 함수
"""

from __future__ import annotations

from .kev_mapf import (
    KEV_ACTION_INSTRUCTIONS,
    KEV_PRIORITY_INSTRUCTIONS,
    KEV_PRIORITY_SCORE_INSTRUCTIONS,
    KEV_WAIT_INSTRUCTIONS,
    KEV_ZONE_INSTRUCTIONS,
    Graph,
    KevMAPF,
    LiveKEV,
    _node,
    _node_sort,
    first_zone_index,
    group_conflict_zones,
    mapf_robot_from_route_result,
    parse_robot_state,
    plan_mapf_from_route_results,
    preview_order_route_for_mapf,
    would_cycle,
    zone_candidates,
)

# 테스트 코드 하위 호환을 위해 tests.mock.kev_mock에서 지연 참조 또는 re-export 지원
try:
    from tests.mock.kev_mock import MockKEV, demo_data
except ImportError:
    pass

__all__ = [
    "Graph",
    "_node",
    "_node_sort",
    "LiveKEV",
    "MockKEV",
    "demo_data",
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
]
