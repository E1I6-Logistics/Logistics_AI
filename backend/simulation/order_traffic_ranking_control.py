"""전략 순서 제안만 교체하는 시뮬 대조군. 일반 관제에서는 설치하지 않는다."""

import math
from time import sleep

ARMS = ("LIVE_KEV", "FIXED_ORDER", "FIXED_ORDER_DELAYED")


class FixedRankingSelector:
    name = "fixed_order_control"
    api_model = None
    last_inference = None

    def __init__(self, delay_seconds=0):
        if not math.isfinite(delay_seconds) or not 0 <= delay_seconds <= 5:
            raise ValueError("대조군 대기는 0~5초여야 합니다.")
        self.delay_seconds = delay_seconds

    def select_choice(self, state, criteria, instructions, **kwargs):
        keys = list(criteria)
        if not keys:
            raise ValueError("전략이 없습니다.")
        # 호출자는 원래와 같은 asyncio.to_thread를 사용한다. 이벤트 루프를 멈추지 않는다.
        if self.delay_seconds:
            sleep(self.delay_seconds)
        weights = list(range(len(keys), 0, -1))
        return dict(choice=keys[0], probabilities={key:weight/sum(weights) for key,weight in zip(keys,weights)},
                    provider="FIXED_ORDER_CONTROL", model_request_sent=False)


def install_controls(arm, delay_seconds):
    if arm not in ARMS:
        raise ValueError("대조 조건을 확인하세요.")
    effective_delay = delay_seconds if arm == "FIXED_ORDER_DELAYED" else 0
    fixed = FixedRankingSelector(effective_delay)
    from backend.app.services import order_traffic_service as common
    from backend.app.services import order_traffic_strategy as strategy

    original_select = common._select_choice_recorded

    def recorded(selector, state, candidates, instructions, metadata):
        return original_select(selector, state, candidates, instructions,
            dict(metadata, ranking_ablation_arm=arm,
                 ranking_source="MODEL" if arm == "LIVE_KEV" else "FIXED_CONTROL",
                 model_request_sent=arm == "LIVE_KEV", artificial_delay_seconds=effective_delay))

    common._select_choice_recorded = recorded
    if arm != "LIVE_KEV":
        original_coordinate = strategy.coordinate_strategy_route

        async def coordinate(*args, **kwargs):
            # 후보 생성·순서 변환·예약 검사·최종 선택·실행 함수는 그대로 사용한다.
            kwargs["selector"] = fixed
            return await original_coordinate(*args, **kwargs)

        strategy.coordinate_strategy_route = coordinate
    return dict(arm=arm, model_used=arm == "LIVE_KEV", artificial_delay_seconds=effective_delay,
                fixed_order="KEEP_PATH_THEN_GENERATED_REROUTES", production_code_unchanged=True)
