"""저장된 부하 시험의 경로·배차 분기를 분석한다. 서버·모델을 호출하지 않는다."""

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
from statistics import mean

POLICIES = ("MIN_EXPECTED_TIME", "KEV_STRATEGY_ORDER")


def read(path):
    return json.loads(path.read_text())


def lines(path):
    if path.exists():
        with path.open() as stream:
            for line in stream:
                yield json.loads(line)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def file_hash(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def minimum_choice(candidates):
    return min(candidates, key=lambda key: (
        candidates[key]["estimated_metrics"]["new_request_completion_seconds"],
        candidates[key]["estimated_metrics"]["total_distance_m"], key,
    ))


def observe(samples, final_orders):
    """API 관측 구간을 보존한다. 실제 사건의 정확한 시각이나 원자적 상태로 취급하지 않는다."""
    moves, dispatched, stations, completed, phases, frames = {}, {}, {}, {}, {}, []
    previous = 0.0
    for sample in samples:
        now = sample["at_seconds"]
        if now < previous:
            raise ValueError("관측 시각이 역전됐습니다.")
        interval = dict(after_seconds=previous, by_seconds=now)
        rows = sample["orders"]["orders"]
        for row in rows:
            name = row["order_name"]
            if row.get("robot_id") is not None:
                dispatched.setdefault(name, dict(**interval, robot_id=row["robot_id"]))
            if row.get("workstation_node") is not None:
                stations.setdefault(name, dict(**interval, node=row["workstation_node"]))
            if row["status"] == "COMPLETED":
                completed.setdefault(name, now)
            signature = (row["status"], row["phase"], row.get("pickup_index"))
            history = phases.setdefault(name, [])
            if not history or tuple(history[-1]["signature"]) != signature:
                history.append(dict(**interval, signature=signature))
            applied = [event for event in row.get("traffic_coordination_history", []) if event["state"] == "APPLIED"]
            for ordinal, event in enumerate(applied):
                key = (name, ordinal)
                if key in moves and fingerprint(moves[key]["event"]) != fingerprint(event):
                    raise ValueError("기록된 이동 이력이 바뀌었습니다.")
                moves.setdefault(key, dict(**interval, event=event))
        frames.append(dict(at_seconds=now,
            orders=[{key:row.get(key) for key in ("order_name", "robot_id", "status", "phase", "pickup_index", "workstation_node")}
                    | {"traffic_state":(row.get("traffic_coordination") or {}).get("state")} for row in rows],
            robots=[{key:row.get(key) for key in ("robot_id", "status", "occupied_node", "occupied_edge", "goal_node")} for row in sample["robots"]]))
        previous = now
    expected = {(row["order_name"], index):event for row in final_orders
                for index, event in enumerate(e for e in row.get("traffic_coordination_history", []) if e["state"] == "APPLIED")}
    if moves.keys() != expected.keys() or any(fingerprint(value["event"]) != fingerprint(expected[key]) for key,value in moves.items()):
        raise ValueError("관측 이동과 최종 이동 이력이 일치하지 않습니다.")
    return dict(moves=moves, dispatched=dispatched, stations=stations, completed=completed, phases=phases, frames=frames)


def first_difference(code, kev, field):
    if code.keys() != kev.keys():
        raise ValueError("대조할 사건이 한쪽에만 있습니다.")
    different = [name for name in code if code[name][field] != kev[name][field]]
    if not different:
        return None
    name = min(different, key=lambda key:min(code[key]["by_seconds"], kev[key]["by_seconds"]))
    return dict(order_name=name, code=code[name], kev=kev[name])


def compare_moves(code, kev):
    """작업대·복귀 목적지가 달라도 같은 주문의 같은 번째 이동으로 대조한다."""
    if code.keys() != kev.keys():
        raise ValueError("주문별 이동 개수가 다릅니다.")
    differences = []
    for key in code:
        a, b = code[key], kev[key]
        changed = [field for field in ("robot_id", "goal_node", "path") if a["event"][field] != b["event"][field]]
        if changed:
            differences.append(dict(order_name=key[0], leg_index=key[1], changed=changed, code=a, kev=b))
    return sorted(differences, key=lambda row:min(row["code"]["by_seconds"], row["kev"]["by_seconds"]))


def strategy_review(events, trace):
    counts = Counter()
    requests = {row["request_id"]:row for row in trace if row["event"] == "REQUEST"}
    responses = [row for row in trace if row["event"] == "RESPONSE"]
    if len(requests) != len(responses) or any(row["event"] == "ERROR" for row in trace):
        raise ValueError("모델 요청·응답이 완결되지 않았습니다.")
    for row in responses:
        if not row["exact_transport_request_saved"] or row["inference"]["request"] != requests[row["request_id"]]["request"]:
            raise ValueError("모델 전송 원문 대조에 실패했습니다.")
    for event in events:
        counts["applied_routes"] += 1
        counts["model_calls"] += event["kev_calls"]
        counts["retry_routes"] += event["retries"] > 0
        counts["code_minimum_matches"] += minimum_choice(event["candidates"]) == event["choice"]
        for batch in event["strategy_batches"]:
            counts["batches"] += 1
            counts["all_strategy_ids_attempted"] += set(batch["strategy_order"]) == {row["strategy"] for row in batch["attempts"]}
            for attempt in batch["attempts"]:
                counts[attempt["result"]] += 1
    if counts["model_calls"] != len(responses):
        raise ValueError("이동 기록과 모델 호출 수가 다릅니다.")
    return dict(counts)


def load_run(folder):
    report = read(folder / "trial/report.json")
    if report["state"] != "COMPLETED":
        raise ValueError("미완료 운행은 이 완료 원인 분석에 섞지 않습니다.")
    observed = observe(lines(folder / "trial/observations.jsonl"), report["orders"])
    events = list(lines(folder / "order-traffic/decisions.jsonl"))
    if Counter(fingerprint(row) for row in events) != Counter(fingerprint(row["event"]) for row in observed["moves"].values()):
        raise ValueError("이동 로그와 관측 이동 이력이 다릅니다.")
    names = {row["order_id"]:row["order_name"] for row in report["orders"]}
    submissions = {names[row["order_id"]]:row["at_seconds"] for row in report["submitted"]}
    durations = {name:observed["completed"][name]-submissions[name] for name in submissions}
    return dict(report=report, observed=observed, events=events,
                order_seconds=durations, mean_order_seconds=mean(durations.values()),
                trace=list(lines(folder / "order-traffic/selection-inferences.jsonl")))


def prior_return_context(assignment, runs):
    if assignment is None:
        return None
    new_robot = assignment["kev"]["robot_id"]
    rows = runs[POLICIES[1]]["report"]["orders"]
    index = next(i for i,row in enumerate(rows) if row["order_name"] == assignment["order_name"])
    previous = [row for row in rows[:index] if row["robot_id"] == new_robot]
    if not previous:
        return None
    name = previous[-1]["order_name"]
    result = dict(previous_order=name, next_order=assignment["order_name"], robot_id=new_robot, runs={})
    for label,policy in (("code",POLICIES[0]),("kev",POLICIES[1])):
        run = runs[policy]
        matching = [(key,value) for key,value in run["observed"]["moves"].items()
                    if key[0] == name and value["event"]["phase"] == "RETURNING"]
        if not matching:
            continue
        key,value = matching[-1]
        order = next(row for row in run["report"]["orders"] if row["order_name"] == name)
        responses = [row for row in run["trace"] if row["event"] == "RESPONSE"
                     and row["order_id"] == order["order_id"] and row["phase"] == "RETURNING"]
        result["runs"][label] = dict(observed_interval={k:value[k] for k in ("after_seconds","by_seconds")},
            event=value["event"], model_responses=[dict(elapsed_seconds=row["elapsed_seconds"],
                suggested_strategy=row["result"]["choice"], model_input=row["inference"]["request"]["state"])
                for row in responses])
    return result


def review(source):
    report = read(source / "load-comparison.json")
    if report["state"] != "COMPLETED_LIMITED_REPEATS":
        raise ValueError("전체 부하 비교가 완료되지 않았습니다.")
    pairs, totals = [], Counter()
    for pair in report["pairs"]:
        folder = source / pair["id"]
        comparison = read(folder / "comparison.json")
        if not all(comparison["checks"].values()):
            raise ValueError("비교 조건 대조에 실패한 주문표입니다.")
        runs = {policy:load_run(folder / policy) for policy in POLICIES}
        a, b = (runs[policy] for policy in POLICIES)
        if a["report"]["schedule"] != b["report"]["schedule"]:
            raise ValueError("주문표가 다릅니다.")
        oa, ob = a["observed"], b["observed"]
        changed = compare_moves(oa["moves"], ob["moves"])
        assignment = first_difference(oa["dispatched"], ob["dispatched"], "robot_id")
        station = first_difference(oa["stations"], ob["stations"], "node")
        windows = {}
        if assignment:
            for label, observed in (("code", oa), ("kev", ob)):
                when = assignment[label]["by_seconds"]
                windows[label] = [frame for frame in observed["frames"] if when-2.5 <= frame["at_seconds"] <= when+1]
        counts = strategy_review(b["events"], b["trace"])
        totals.update(counts)
        summaries = {row["policy"]:row["summary"] for row in comparison["trials"]}
        if any(abs(runs[policy]["mean_order_seconds"]-summaries[policy]["mean_order_seconds"]) > 1e-8 for policy in POLICIES):
            raise ValueError("주문별 시간 재집계가 원래 보고서와 다릅니다.")
        pairs.append(dict(id=pair["id"], case=pair["case"], seed=pair["seed"],
            first_assignment_difference=assignment, first_movement_difference=changed[0] if changed else None,
            first_workstation_difference=station, movement_differences=changed, dispatch_windows=windows,
            order_timelines={label:run["observed"]["phases"] for label,run in (("code",a),("kev",b))},
            strategy_counts=counts, mean_order_seconds=dict(code=a["mean_order_seconds"],kev=b["mean_order_seconds"]),
            order_seconds={label:run["order_seconds"] for label,run in (("code",a),("kev",b))},
            code_minus_kev_order_seconds={name:a["order_seconds"][name]-b["order_seconds"][name] for name in a["order_seconds"]},
            prior_return_context=prior_return_context(assignment,runs), whole_run_summaries=summaries))
    return dict(scope="OFFLINE_SAVED_LOAD_TRIAL_REVIEW", no_fms_commands=True, new_model_requests=0,
        source=str(source), pairs=pairs, strategy_totals=dict(totals),
        limitations=["SAMPLED_EVENT_INTERVALS_NOT_EXACT_TIMESTAMPS", "ORDERS_AND_ROBOTS_HTTP_READS_NOT_ATOMIC",
                     "NO_SAME_STATE_COUNTERFACTUAL_EXECUTION", "NO_LATENCY_MATCHED_CONTROL",
                     "NO_CAUSAL_ATTRIBUTION_OF_TOTAL_TIME_GAIN", "NO_ORIGINAL_DETAILED_RESERVATION_TABLE_IN_MODEL_INPUT"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source, output = args.source.resolve(), args.output.resolve()
    if output.exists():
        parser.error("이전 결과를 덮어쓰지 않습니다. 새 출력 경로를 지정하세요.")
    if output.is_relative_to(source):
        parser.error("운행 원문 폴더 밖에 분석 결과를 저장하세요.")
    paths = [source / "load-comparison.json"]
    for pair in read(source / "load-comparison.json")["pairs"]:
        folder = source / pair["id"]
        paths.append(folder / "comparison.json")
        for policy in POLICIES:
            paths.extend(folder / policy / relative for relative in (
                "trial/report.json", "trial/observations.jsonl", "order-traffic/decisions.jsonl"))
            trace = folder / policy / "order-traffic/selection-inferences.jsonl"
            if trace.exists():
                paths.append(trace)
    hashes = {str(path):file_hash(path) for path in paths}
    result = review(source)
    result["original_files_unchanged"] = all(file_hash(Path(path)) == sha for path,sha in hashes.items())
    if not result["original_files_unchanged"]:
        raise ValueError("분석 중 운행 원문이 바뀌었습니다.")
    result["source_sha256"] = hashes
    result["review_tool_sha256"] = file_hash(Path(__file__))
    output.mkdir(parents=True)
    (output / "review.json").write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n")
    print(json.dumps(dict(pairs=len(result["pairs"]),strategy_totals=result["strategy_totals"],
                         original_files_unchanged=result["original_files_unchanged"],new_model_requests=0),ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
