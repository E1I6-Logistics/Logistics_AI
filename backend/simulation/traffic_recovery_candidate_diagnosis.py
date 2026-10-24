"""후보 ID와 표시 순서를 독립 변경하는 격리 시험. --run만 실제 KEV에 요청한다.

후보 내용·상태·지시문·검사기는 유지한다. 일반 관제나 로봇 명령 API는 호출하지 않는다.
"""

import argparse
from collections import defaultdict
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from backend.app.config import BASE_DIR
from backend.app.services.traffic_joint_actions import TrafficSituation, fingerprint
from backend.app.services.traffic_recovery_guidance import INSTRUCTIONS
from backend.app.services.traffic_recovery_search import allowed_moves
from backend.simulation.traffic_recovery_comparison import comparison_cases, run_comparison
from backend.simulation.traffic_recovery_input_diagnosis import source_records


CONDITIONS = (("original", "original"), ("shifted", "original"),
              ("original", "reverse"), ("shifted", "reverse"))


def cyclic_labels(keys):
    """전체 동작 ID를 한 칸씩 바꾼다. 후보가 줄어도 같은 동작의 ID 대응을 유지한다."""
    keys = sorted(set(keys), key=lambda key: int(key[1:]))
    if len(keys) < 2:
        raise ValueError("ID 비교에는 두 개 이상의 동작이 필요합니다.")
    return dict(zip(keys, keys[1:] + keys[:1]))


class CandidatePresentationSelector:
    """시험용 요청 변환기. 모델 선택을 원래 이동 ID로 되돌려 기존 탐색기에 전달한다."""

    def __init__(self, selector, labels, *, id_style, order):
        if (id_style, order) not in CONDITIONS:
            raise ValueError("지원하지 않는 후보 표시 조건입니다.")
        if len(set(labels.values())) != len(labels):
            raise ValueError("후보 ID 대응은 일대일이어야 합니다.")
        self.selector, self.labels = selector, dict(labels)
        self.id_style, self.order = id_style, order
        self.name = getattr(selector, "name", "test")
        self.api_model = getattr(selector, "api_model", None)
        self.calls = []
        self.last_inference = None

    def select_choice(self, state, criteria, instructions, **kwargs):
        items = list(criteria.items())
        if self.order == "reverse":
            items.reverse()
        mapping = {key: self.labels[key] if self.id_style == "shifted" else key for key in criteria}
        presented = {mapping[key]: value for key, value in items}
        reverse = {value: key for key, value in mapping.items()}
        record = dict(id_style=self.id_style, candidate_order=self.order,
                      canonical_to_presented=mapping, canonical_criteria=deepcopy(criteria),
                      presented_criteria=deepcopy(presented), state_sha256=fingerprint(state))
        self.calls.append(record)
        # 검사 결과나 정답 ID를 추가하지 않고 상태·지시문·각 후보 설명을 그대로 전달한다.
        result = self.selector.select_choice(deepcopy(state), presented, instructions, **kwargs)
        if result.get("choice") not in reverse:
            raise ValueError("표시한 후보에 없는 모델 응답입니다.")
        normalized = deepcopy(result)
        normalized["choice"] = reverse[result["choice"]]
        if "probabilities" in normalized:
            if set(normalized["probabilities"]) - set(reverse):
                raise ValueError("표시한 후보에 없는 확률 응답입니다.")
            normalized["probabilities"] = {reverse[k]: v for k, v in normalized["probabilities"].items()}
        record.update(presented_result=deepcopy(result), canonical_result=deepcopy(normalized),
                      actual_inference=deepcopy(getattr(self.selector, "last_inference", None)))
        self.last_inference = deepcopy(record)
        return normalized


def diagnosis_cases():
    cases = comparison_cases()
    base = cases["two_yields"]
    other = deepcopy(base)
    other["snapshot_id"] = "candidate-diagnosis-workstation4"
    placements = {"robot1": ("4", "0", ["4", "12", "8", "10", "0"]),
                  "robot2": ("12", "12", ["12"]), "robot3": ("10", "10", ["10"])}
    for request in other["route_requests"]:
        start, goal, path = placements[request["order"]["robot_id"]]
        request.update(current_node=start, node=goal, result=dict(path=path))
    other["config"].update(
        zone_occupancy={"node:4": "robot1", "node:12": "robot2", "node:10": "robot3"},
        wait_allowed_nodes=["4", "12", "10", "11", "9", "0"],
        yield_paths={"robot2": dict(out=["12", "11"], resume=["11", "12"]),
                     "robot3": dict(out=["10", "9"], resume=["9", "10"])})
    return {"workstation3_return": base, "workstation4_return": other,
            "missing_second_yield": cases["missing_second_yield"]}


def scene_labels(payload):
    value = deepcopy(payload)
    for key in ("recovery_search", "recovery_strategy", "recovery_guidance"):
        value["config"].pop(key, None)
    scene = TrafficSituation(value)
    robot_ids = tuple(sorted(scene.requests, key=lambda rid: (scene.requests[rid]["request_order"], rid)))
    return cyclic_labels([f"M{i + 1}" for i, _ in enumerate(allowed_moves(scene, robot_ids))])


def replay(records, selector, repeats):
    labels = cyclic_labels(key for row in records for key in row["criteria"])
    rows = []
    for repeat in range(repeats):
        for source in records:
            # 요청 묶음마다 시작 조건을 회전시켜 한 조건만 항상 먼저 요청하지 않는다.
            offset = (repeat + source["call"] - 1) % len(CONDITIONS)
            conditions = CONDITIONS[offset:] + CONDITIONS[:offset]
            if repeat % 2:
                conditions = tuple(reversed(conditions))
            for id_style, order in conditions:
                proxy = CandidatePresentationSelector(selector, labels, id_style=id_style, order=order)
                result = proxy.select_choice(source["state"], source["criteria"], INSTRUCTIONS,
                                             question_id="traffic_recovery_move")
                rows.append(dict(repeat=repeat + 1, source_call=source["call"], id_style=id_style,
                                 candidate_order=order, canonical_choice=result["choice"],
                                 immediate_check=source["assessment_only_reasons"][result["choice"]] or "VALID",
                                 presentation=deepcopy(proxy.calls[-1])))
        print("REPLAY", repeat + 1, "requests", len(rows), flush=True)
    totals = defaultdict(lambda: dict(requests=0, immediate_valid=0))
    paired = {(r["repeat"], r["source_call"], r["id_style"], r["candidate_order"]): r["canonical_choice"] for r in rows}
    effects = []
    for factor, fixed_values in (("id_style", ("original", "reverse")),
                                 ("candidate_order", ("original", "shifted"))):
        for fixed in fixed_values:
            pairs = []
            for repeat in range(1, repeats + 1):
                for source in records:
                    key = (repeat, source["call"])
                    if factor == "id_style":
                        pair = (paired[(*key, "original", fixed)], paired[(*key, "shifted", fixed)])
                    else:
                        pair = (paired[(*key, fixed, "original")], paired[(*key, fixed, "reverse")])
                    pairs.append(pair)
            effects.append(dict(changed_factor=factor, fixed_other_factor=fixed,
                                pairs=len(pairs), different_semantic_choice=sum(a != b for a, b in pairs)))
    for row in rows:
        total = totals[f"{row['id_style']}/{row['candidate_order']}"]
        total["requests"] += 1
        total["immediate_valid"] += row["immediate_check"] == "VALID"
    return dict(label_permutation=labels, requests=rows, summary=dict(totals), paired_effects=effects,
                metric="NEXT_MOVE_PREFIX_VALIDITY_NOT_COMPLETE_PLAN_QUALITY")


def closed_loop(cases, selector_factory, repeats):
    baseline = run_comparison(cases, strategies=("DEPTH_FIRST",))
    rows = []
    for repeat in range(repeats):
        conditions = CONDITIONS if repeat % 2 == 0 else tuple(reversed(CONDITIONS))
        for name, payload in cases.items():
            labels = scene_labels(payload)
            for id_style, order in conditions:
                proxy = CandidatePresentationSelector(selector_factory(), labels, id_style=id_style, order=order)
                result = run_comparison({name: payload}, selector_factory=lambda: proxy, strategies=("KEV_GUIDED",))
                row = result["runs"][0]
                row.update(repeat=repeat + 1, id_style=id_style, candidate_order=order,
                           label_permutation=labels, presentations=deepcopy(proxy.calls))
                rows.append(row)
    return dict(code_baseline=baseline, runs=rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--source", type=Path, default=Path(BASE_DIR) / "logs/traffic-joint-actions/run-82971e17a43a4d90b0086bd9b97ee8f9.json")
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 1 <= args.repeats <= 3:
        parser.error("repeats는 1~3입니다.")
    records, cases = source_records(args.source), diagnosis_cases()
    preview = dict(source=str(args.source), saved_requests=len(records),
                   planned_replay_requests=len(records) * 4 * args.repeats,
                   planned_code_searches=len(cases), planned_guided_searches=len(cases) * 4 * args.repeats,
                   cases={name: dict(input_sha256=fingerprint(case), label_permutation=scene_labels(case)) for name, case in cases.items()},
                   live_fms_commands_sent=False)
    if not args.run:
        print(json.dumps(preview, ensure_ascii=False, indent=2))
        return
    from backend.app.services.route_selector.kev_selector import KevSelector

    now = datetime.now(ZoneInfo("Asia/Seoul"))
    output = args.output or Path(BASE_DIR) / "logs/traffic-recovery" / now.strftime("candidate-diagnosis-%Y%m%d-%H%M%S")
    output.mkdir(parents=True, exist_ok=True)
    report = dict(**preview, started_at=now.isoformat(), source_analysis=records,
                  source_sha256=fingerprint(json.loads(args.source.read_text())))
    try:
        factory = lambda: KevSelector(model="kev-08b", timeout_seconds=2.0)
        report["replay"] = replay(records, factory(), args.repeats)
        # 요청 재현 기록을 먼저 저장해 후속 시험이 실패해도 보존한다.
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        report["closed_loop"] = closed_loop(cases, factory, args.repeats)
    finally:
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print("REPORT", str(output.resolve()), flush=True)
    print("SUMMARY", json.dumps(dict(replay=report["replay"]["summary"], effects=report["replay"]["paired_effects"]), ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
