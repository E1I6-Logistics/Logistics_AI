"""저장된 KEV 답변을 사후 분석한다. 모델 요청·관제 명령은 보내지 않는다.

현재 한 단계 검사뿐 아니라, 주어진 동작으로 마지막 도착·복귀까지 이어질 수
있는지도 전부 확인한다. 이 정답 계산 비용은 온라인 방식의 성능으로 세지 않는다.
"""

import argparse
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path

from backend.app.config import BASE_DIR
from backend.app.services.traffic_joint_actions import Step, TrafficSituation, fingerprint
from backend.app.services.traffic_recovery_search import allowed_moves


class CompletionAssessment:
    """작은 정지 상태 시험의 사후 정답 계산기. 공급 경로·1회 양보 계약에 한정한다."""

    def __init__(self, payload):
        self.max_steps = payload["config"]["recovery_search"]["max_steps"]
        value = deepcopy(payload)
        for key in ("recovery_search", "recovery_strategy", "recovery_guidance"):
            value["config"].pop(key, None)
        self.scene = TrafficSituation(value)
        self.robot_ids = tuple(sorted(self.scene.requests, key=lambda rid: (self.scene.requests[rid]["request_order"], rid)))
        self.moves = allowed_moves(self.scene, self.robot_ids)
        self.cache = {}
        self.reservation_checks = 0

    def stages(self, steps):
        stages = [0] * len(self.robot_ids)
        for step in steps:
            matches = [m for m in self.moves if m.step == step and stages[m.robot_index] == m.from_stage]
            if len(matches) != 1:
                raise ValueError("저장된 이동이 공급 동작 또는 1회 양보 계약과 맞지 않습니다.")
            move = matches[0]
            stages[move.robot_index] = move.to_stage
        return tuple(stages)

    def complete(self, stages):
        return all(stage == 2 or (stage == 0 and self.scene.requests[rid]["current_node"] == self.scene.requests[rid]["node"])
                   for rid, stage in zip(self.robot_ids, stages))

    def check(self, steps, *, complete):
        self.reservation_checks += 1
        return self.scene._preflight(self.scene._plan("ASSESSMENT_ONLY", "ASSESSMENT_ONLY", steps),
                                     require_goal_restoration=complete)

    def continuation(self, steps):
        """가능하면 최종 계획 한 개를 반환한다. 실패로 확정할 때는 모든 후속 동작을 확인한다."""
        if steps in self.cache:
            return self.cache[steps]
        stages = self.stages(steps)
        if self.complete(stages):
            result = steps if self.check(steps, complete=True) is None else None
        else:
            result = None
            if len(steps) < self.max_steps:
                for move in self.moves:
                    if stages[move.robot_index] != move.from_stage:
                        continue
                    next_steps = (*steps, move.step)
                    if self.check(next_steps, complete=False) is None:
                        result = self.continuation(next_steps)
                        if result is not None:
                            break
        self.cache[steps] = result
        return result

    def assess(self, state, criteria, choice):
        prefix = tuple(Step(p["robot_id"], p["action"], tuple(p["path"])) for p in state["chosen_prefix"])
        stages = self.stages(prefix)
        # 다른 상태에 저장된 후보를 잘못 결합해 평가하지 않는다.
        expected_positions = {rid: self.scene.requests[rid]["current_node"] for rid in self.robot_ids}
        for step in prefix:
            expected_positions[step.robot_id] = step.path[-1]
        supplied_positions = {r["robot_id"]: r["current_node"] for r in state["robots"]}
        if expected_positions != supplied_positions:
            raise ValueError("저장된 가상 위치와 선택 전 이동 기록이 다릅니다.")
        assessment = {}
        for key, description in criteria.items():
            move = json.loads(description)
            step = Step(move["robot_id"], move["action"], tuple(move["path"]))
            if not any(m.step == step and stages[m.robot_index] == m.from_stage for m in self.moves):
                raise ValueError("현재 상태에서 허용하지 않은 후보입니다.")
            steps = (*prefix, step)
            reason = self.check(steps, complete=False)
            witness = None if reason else self.continuation(steps)
            category = "IMMEDIATE_REJECTED" if reason else ("CAN_COMPLETE" if witness is not None else "NO_COMPLETE_PLAN")
            assessment[key] = dict(category=category, immediate_reason=reason, move=move,
                                   witness=[dict(robot_id=s.robot_id, action=s.action, path=list(s.path)) for s in witness] if witness else None)
        if choice not in assessment:
            raise ValueError("선택한 후보가 저장된 후보에 없습니다.")
        viable = [key for key, value in assessment.items() if value["category"] == "CAN_COMPLETE"]
        selected = assessment[choice]
        return dict(choice=choice, category=selected["category"], selected_move=selected["move"],
                    offered_complete_choices=viable,
                    avoidable_with_offered_choice=bool(viable) and selected["category"] != "CAN_COMPLETE",
                    candidates=assessment)


def summary(rows):
    return dict(answer_count=len(rows), categories=dict(Counter(r["assessment"]["category"] for r in rows)),
                avoidable_with_offered_choice=sum(r["assessment"]["avoidable_with_offered_choice"] for r in rows),
                no_offered_complete_choice=sum(not r["assessment"]["offered_complete_choices"] for r in rows))


def analyze(report):
    source = json.loads(Path(report["source"]).read_text())
    if fingerprint(source) != report["source_sha256"]:
        raise ValueError("원본 실행 기록이 비교 시험 이후 변경됐습니다.")
    payload = source.get("traffic_input") or source.get("final_status", {}).get("traffic_input")
    if payload is None:
        raise ValueError("원본 실행 입력이 필요합니다.")
    evaluator = CompletionAssessment(payload)
    source_rows = []
    for row in report["source_analysis"]:
        source_rows.append(dict(source_call=row["call"], assessment=evaluator.assess(row["state"], row["criteria"], row["original_choice"])))
    saved = {row["call"]: row for row in report["source_analysis"]}
    replay_rows = []
    for row in report["replay"]["requests"]:
        original = saved[row["source_call"]]
        replay_rows.append(dict(source_call=row["source_call"], repeat=row["repeat"], id_style=row["id_style"],
                                candidate_order=row["candidate_order"],
                                assessment=evaluator.assess(original["state"], original["criteria"], row["canonical_choice"])))
    loop_rows = []
    evaluators = {}
    # 저장된 공통 입력을 사용해 이후 시나리오 설정 변경의 영향을 받지 않는다.
    for row in report["closed_loop"]["runs"]:
        name = row["case"]
        if name not in evaluators:
            case_input = report["closed_loop"]["code_baseline"]["inputs"][name]
            if fingerprint(case_input) != row["common_input_sha256"]:
                raise ValueError("저장된 공통 입력 해시가 다릅니다.")
            evaluators[name] = CompletionAssessment(case_input)
        for call in row["search"]["guidance_calls"]:
            question = call["request"]["questions"]["traffic_recovery_move"]
            loop_rows.append(dict(case=name, repeat=row["repeat"], id_style=row["id_style"],
                                  candidate_order=row["candidate_order"], call=call["call"],
                                  assessment=evaluators[name].assess(call["request"]["state"], question["criteria"], call["result"]["choice"])))
    groups = {}
    for rows, keys, label in ((replay_rows, ("id_style", "candidate_order"), "replay"),
                              (loop_rows, ("case", "id_style", "candidate_order"), "closed_loop")):
        group_names = sorted({tuple(row[k] for k in keys) for row in rows})
        groups[label] = {"/".join(group): summary([row for row in rows if tuple(row[k] for k in keys) == group]) for group in group_names}
    return dict(model_requests_sent=0, live_fms_commands_sent=False,
                scope="OFFLINE_SUPPLIED_PATHS_ONE_YIELD_PER_ROBOT_WITHIN_MAX_STEPS",
                oracle_limits="EXHAUSTIVE_AFTER_THE_FACT_NOT_SUBJECT_TO_ONLINE_CHECK_CALL_OR_TIME_BUDGET",
                source_rows=source_rows, source_summary=summary(source_rows), replay_rows=replay_rows,
                closed_loop_rows=loop_rows, groups=groups,
                offline_reservation_checks=evaluator.reservation_checks + sum(e.reservation_checks for e in evaluators.values()))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(BASE_DIR) / "logs/traffic-recovery/candidate-diagnosis-20261020-193514/report.json")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = json.loads(args.source.read_text())
    result = analyze(report)
    result.update(source=str(args.source.resolve()), source_sha256=fingerprint(report))
    output = args.output or args.source.with_name("answer-analysis.json")
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(dict(source=result["source_summary"], groups=result["groups"], report=str(output.resolve())), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
