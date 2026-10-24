"""저장 요청의 입력 표현·후보 순서 대조와 새 복귀 배치의 격리 탐색 비교.

--run만 실제 KEV에 요청한다. 기존 관제·주문·로봇에 명령하지 않는다.
"""

import argparse
from collections import defaultdict
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path

from backend.app.config import BASE_DIR
from backend.app.services.traffic_joint_actions import Step, TrafficSituation, fingerprint
from backend.app.services.traffic_recovery_guidance import INSTRUCTIONS
from backend.app.services.traffic_recovery_input import INPUT_STYLES, format_guidance_state, path_occupancy_facts
from backend.simulation.traffic_recovery_comparison import comparison_cases, run_comparison


def source_records(source):
    report = json.loads(source.read_text())
    status = report.get("final_status", report)
    calls = status["coordination"]["recovery_search"]["guidance_calls"]
    payload = deepcopy(status.get("traffic_input") or report.get("traffic_input"))
    if payload is None:
        raise ValueError("traffic_input이 포함된 run-실행ID.json 기록을 사용하세요.")
    for key in ("recovery_search", "recovery_strategy", "recovery_guidance"):
        payload["config"].pop(key, None)
    scene = TrafficSituation(payload)
    rows = []
    for call in calls:
        request = call["request"]
        state = call.get("planning_state", request["state"])
        if not isinstance(state, dict):
            raise ValueError("저장된 원래 계획 상태가 필요합니다.")
        question = request["questions"]["traffic_recovery_move"]
        criteria = question["criteria"]
        prefix = [Step(p["robot_id"], p["action"], tuple(p["path"])) for p in state["chosen_prefix"]]
        reasons = {}
        for key, description in criteria.items():
            p = json.loads(description)
            plan = scene._plan("DIAGNOSTIC", "DIAGNOSTIC", [*prefix, Step(p["robot_id"], p["action"], tuple(p["path"]))])
            reasons[key] = scene._preflight(plan, require_goal_restoration=False)
        choice = call["result"]["choice"]
        rows.append(dict(call=call["call"], state=deepcopy(state), criteria=deepcopy(criteria),
                         original_choice=choice, original_check=reasons[choice] or "VALID",
                         current_path_occupants=path_occupancy_facts(state, criteria),
                         assessment_only_reasons=reasons, original_request_sha256=fingerprint(request)))
    return rows


def replay(records, selector, repeats):
    rows = []
    # 2회차는 표현의 실행 순서를 반대로 하여 항상 같은 표현만 먼저 요청하지 않는다.
    for repeat in range(repeats):
        styles = INPUT_STYLES if repeat % 2 == 0 else tuple(reversed(INPUT_STYLES))
        for source in records:
            for style in styles:
                for order in ("original", "reverse"):
                    criteria = deepcopy(source["criteria"])
                    if order == "reverse":
                        criteria = dict(reversed(list(criteria.items())))
                    state = format_guidance_state(source["state"], source["criteria"], style)
                    before = getattr(selector, "last_inference", None)
                    result = selector.select_choice(state, criteria, INSTRUCTIONS, question_id="traffic_recovery_move")
                    choice = result["choice"]
                    if choice not in source["assessment_only_reasons"]:
                        raise ValueError("허용되지 않은 모델 응답")
                    inference = getattr(selector, "last_inference", None)
                    rows.append(dict(repeat=repeat + 1, source_call=source["call"], input_style=style,
                                     candidate_order=order, choice=choice,
                                     immediate_check=source["assessment_only_reasons"][choice] or "VALID",
                                     request=dict(state=state, criteria=criteria, instructions=INSTRUCTIONS),
                                     result=deepcopy(result), inference=deepcopy(inference) if inference is not before else None))
        print("REPLAY", repeat + 1, "requests", len(rows), flush=True)
    totals = defaultdict(lambda: dict(requests=0, immediate_valid=0))
    for row in rows:
        summary = totals[f"{row['input_style']}/{row['candidate_order']}"]
        summary["requests"] += 1
        summary["immediate_valid"] += row["immediate_check"] == "VALID"
    return dict(requests=rows, summary=dict(totals),
                metric="NEXT_MOVE_PREFIX_RESERVATION_VALIDITY_NOT_COMPLETE_PLAN_QUALITY")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--source", type=Path, default=Path(BASE_DIR) / "logs/traffic-joint-actions/run-fe5556d83fea49adac74f76bbf0673ce.json")
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 1 <= args.repeats <= 3:
        parser.error("repeats는 1~3입니다.")
    records = source_records(args.source)
    if not args.run:
        print(json.dumps(dict(source=str(args.source), saved_requests=len(records),
                              planned_replay_requests=len(records) * len(INPUT_STYLES) * 2 * args.repeats), ensure_ascii=False, indent=2))
        return
    from backend.app.services.route_selector.kev_selector import KevSelector

    output = args.output or Path(BASE_DIR) / "logs/traffic-recovery" / datetime.now().strftime("input-diagnosis-%Y%m%d-%H%M%S")
    output.mkdir(parents=True, exist_ok=True)
    report = dict(source=str(args.source), source_sha256=fingerprint(json.loads(args.source.read_text())),
                  source_analysis=records, live_fms_commands_sent=False)
    try:
        report["replay"] = replay(records, KevSelector(model="kev-08b", timeout_seconds=2.0), args.repeats)
        cases = comparison_cases()
        loops = dict(code=run_comparison(cases, strategies=("DEPTH_FIRST",)))
        for style in INPUT_STYLES:
            loops[style] = run_comparison(cases, strategies=("KEV_GUIDED",), guidance_options=dict(input_style=style))
        report["new_workstation_return_search"] = loops
    finally:
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print("REPORT", str(output.resolve()), flush=True)
    print("REPLAY_SUMMARY", json.dumps(report["replay"]["summary"], ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
