"""별도 입력 진단용 선택기. 운영 설정·경로·예약·후보 집합을 변경하지 않는다."""

from copy import deepcopy
import json


CONCISE_INSTRUCTIONS = (
    "현재 가상 상태에서 검사할 동작 하나를 고르세요. 후보는 예약 검사 전이며, 완성 계획 전에는 실제 이동하지 않습니다. "
    "시간보다 제약을 먼저 지키세요. current_other_occupants가 있으면 먼저 길을 비우고, "
    "finishes_on_required_nodes가 있으면 다른 로봇의 통과 전 복귀하지 마세요. "
    "각 로봇은 양보 한 번만 가능하고, 완료한 로봇은 다시 움직일 수 없습니다. "
    "유용한 이동이 남으면 진행하고, 되돌릴 수 없는 막힘이나 모든 후보가 막힌 경우에만 BACKTRACK으로 현재 순서를 미루세요. "
    "이는 실제 후진이 아니며 남은 순서를 보관합니다. 모든 원래 이동과 양보 로봇 복귀까지 계획하세요. "
    "가능한 이동끼리는 평균 도착 최소 예상값, 복귀까지의 최소 예상값, 이동 시간을 비교하세요. "
    "평균은 원래 이동 요청만 셉니다. 값은 지도 거리와 일정 속도 기준이며 다른 로봇의 선행 이동·회전·예약 대기·추론을 무시한 낙관값입니다. "
    "null은 모름입니다. 관계 정보는 예약 허가나 완성 정답이 아닙니다. 첫 완성 계획 탐색이며 전체 시간 최적 보장은 없습니다. "
    "후보 이름이나 표시 순서는 우선순위가 아닙니다."
)


def concise_dependency_request(state, criteria):
    """선택지에 있는 이동 정보를 상태에서만 중복 제거한다. 모든 후보와 다른 사실은 보존한다."""
    result = deepcopy(state)
    moves = result['dependency_facts'].pop('offered_moves')
    if set(criteria) - {'BACKTRACK'} != set(moves):
        raise ValueError('상태와 선택지의 이동 목록이 다릅니다.')
    for key, move in moves.items():
        if json.loads(criteria[key]) != move:
            raise ValueError('상태와 선택지의 이동 정보가 다릅니다.')
    return result, deepcopy(criteria), CONCISE_INSTRUCTIONS


class ConciseDependencySelector:
    """격리 시험에서만 사용한다. 기존 탐색기·검사·한도는 그대로 사용한다."""

    def __init__(self, selector):
        self.selector = selector

    def __getattr__(self, name):
        return getattr(self.selector, name)

    def select_choice(self, state, criteria, instructions, **kwargs):
        state, criteria, instructions = concise_dependency_request(state, criteria)
        return self.selector.select_choice(state, criteria, instructions, **kwargs)
