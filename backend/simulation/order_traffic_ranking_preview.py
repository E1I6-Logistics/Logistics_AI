"""전략 순서·응답 대기 대조를 위한 별도 시뮬 진입점."""

import os

from backend.simulation.order_traffic_preview import app
from backend.app.routers import mode as mode_router
from backend.simulation.order_traffic_ranking_control import install_controls

CONTROL = install_controls(os.environ["FMS_RANKING_ABLATION_ARM"],
                           float(os.environ["FMS_RANKING_ABLATION_DELAY_SECONDS"]))
_base_snapshot = mode_router._snapshot


def snapshot():
    return {**_base_snapshot(), "ranking_ablation": CONTROL}


mode_router._snapshot = snapshot
