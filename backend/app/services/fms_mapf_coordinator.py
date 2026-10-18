"""[모듈: fms_mapf_coordinator.py - KEV-MAPF Coordinator 하위 호환성 파사드(Facade)]
- 역할: 기존 외부 참조 경로(`backend.app.services.fms_mapf_coordinator`)를 유지하며,
  실제 구현은 `backend/app/services/kev_mapf/kev_coordinator.py`로 위임한다.
"""

from .kev_mapf.kev_coordinator import (
    FmsMapfCoordinator,
    _blocked_response,
    _is_valid_suffix,
    fms_mapf_coordinator,
    is_fms_reviewable_advisory,
)

__all__ = [
    "FmsMapfCoordinator",
    "fms_mapf_coordinator",
    "is_fms_reviewable_advisory",
    "_is_valid_suffix",
    "_blocked_response",
]
