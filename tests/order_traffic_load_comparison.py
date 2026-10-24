"""고정 주문표로 코드·새 KEV의 세 부하 상황을 두 번씩 비교한다.

기본은 주문표 준비만 한다. --run이면 별도 시뮬 서버를 순서대로 실행한다.
한 시험이라도 실패하거나 조건 대조가 틀리면 남은 실행을 중단하고 기록을 남긴다.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
from random import Random
import subprocess
import sys
from time import monotonic

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests.order_traffic_strategy_smoke import load_schedule

CATALOG = ROOT / "tests/order_traffic_load_cases.json"
POLICIES = ["MIN_EXPECTED_TIME", "KEV_STRATEGY_ORDER"]


def make_orders(case, seed, count):
    rng = Random(seed)
    orders = []
    for index in range(count):
        items = dict.fromkeys("ABCD", 0)
        if case["items"] == "AB":
            quantity = rng.randint(1, 5)
            for _ in range(quantity):
                items[rng.choice("AB")] += 1
        else:
            quantity = rng.randint(2, 5)
            items[rng.choice("AB")] += 1
            items[rng.choice("CD")] += 1
            for _ in range(quantity - 2):
                items[rng.choice("ABCD")] += 1
        at = max(0, index - case["initial_orders"] + 1) * case["interval_seconds"]
        orders.append(dict(at_seconds=at, items=items))
    return orders


def build_plan(catalog):
    result = []
    for repetition, seed in enumerate(catalog["seeds"]):
        for case in catalog["cases"]:
            result.append(dict(id=f"{case['id']}-seed-{seed}", description=case["description"],
                case=case["id"], seed=seed,
                policies=POLICIES if repetition % 2 == 0 else list(reversed(POLICIES)),
                orders=make_orders(case, seed, catalog["orders_per_case"])))
    return result


def render(report):
    lines = ["# 연속 주문 부하 비교", "", report["state"], "",
        "| 상황·주문표 | 방식 | 작업 완료 | 평균 주문 시간 | 홈 복귀까지 | 모델 호출 | 최대 대기 주문 |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: |"]
    for pair in report["pairs"]:
        for trial in pair.get("trials", []):
            s = trial["summary"]
            seconds = lambda value: "미완료" if value is None else f"{value:.2f}초"
            label = "코드" if trial["policy"] == POLICIES[0] else "새 KEV"
            lines.append(f"| {pair['id']} | {label} | {s['completed_orders']}/{s['expected_orders']} | {seconds(s['mean_order_seconds'])} | {seconds(s['drain_seconds'])} | {s['model_calls']} | {s['max_pending']} |")
    lines.extend(["", "기록한 주문표 각각 한 번의 실행이다. 약 0.2초 간격 관측이며 장시간 안전·지속 처리량·통계적 우월성의 증명이 아니다.",
        "로봇 WAITING은 관측 상태의 누적 시간이며 교통조정 때문에 기다린 시간만 따로 분리한 값이 아니다.",
        "모든 주문의 접수 종료 뒤 남은 주문과 홈 복귀까지 기다린다. 화면용 서버·실기체에 명령하지 않는다."])
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8033)
    args = parser.parse_args()
    if not 8020 <= args.port <= 8099:
        parser.error("별도 시험 포트는 8020~8099를 사용하세요.")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    catalog = json.loads(CATALOG.read_text())
    plan = build_plan(catalog)
    for pair in plan:
        path = output / "schedules" / (pair["id"] + ".json")
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(dict(orders=pair["orders"]), ensure_ascii=False, indent=2) + "\n")
        load_schedule(path)
        pair["schedule_file"] = str(path)
    sources = ("tests/order_traffic_load_comparison.py", "tests/order_traffic_load_cases.json",
               "tests/order_traffic_policy_comparison.py", "tests/order_traffic_strategy_smoke.py")
    hashes = {}
    for name in sources:
        data = (ROOT / name).read_bytes()
        hashes[name] = hashlib.sha256(data).hexdigest()
        destination = output / "source" / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
    report = dict(state="PREPARED", plan=plan, source_sha256=hashes, pairs=[], errors=[],
                  trial_timeout_seconds=catalog["trial_timeout_seconds"],
                  visible_server_commands_sent=False, real_robot_commands_sent=False)

    def save():
        (output / "load-comparison.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        (output / "summary.md").write_text(render(report))

    save()
    if not args.run:
        print("주문표 준비 완료", output, flush=True)
        return 0
    started = monotonic()
    report["state"] = "RUNNING"; save()
    try:
        for pair in plan:
            if any(hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != sha for name, sha in hashes.items()):
                raise RuntimeError("도구·주문표가 변경돼 남은 시험을 중단합니다.")
            print("상황 시험 시작", pair["id"], flush=True)
            folder = output / pair["id"]
            env = dict(os.environ, PYTHONNOUSERSITE="1")
            with (output / (pair["id"] + ".log")).open("wb") as log:
                code = subprocess.run([sys.executable, str(ROOT / "tests/order_traffic_policy_comparison.py"),
                    "--run", "--output", str(folder), "--port", str(args.port),
                    "--policies", *pair["policies"], "--schedule-file", pair["schedule_file"],
                    "--trial-timeout", str(catalog["trial_timeout_seconds"])],
                    cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT).returncode
            result = json.loads((folder / "comparison.json").read_text())
            report["pairs"].append(dict(id=pair["id"], case=pair["case"], seed=pair["seed"],
                state=result["state"], checks=result.get("checks"), trials=result["trials"],
                errors=result["errors"], folder=str(folder)))
            save()
            print("상황 시험 종료", pair["id"], result["state"], flush=True)
            if code != 0:
                raise RuntimeError(f"{pair['id']}: 실패·조건 불일치 기록을 먼저 검토합니다.")
        report["state"] = "COMPLETED_LIMITED_REPEATS"
    except (Exception, KeyboardInterrupt) as exc:
        report["state"] = "STOPPED_REVIEW_REQUIRED"
        report["errors"].append(f"{type(exc).__name__}: {exc}")
    finally:
        report["elapsed_seconds"] = monotonic() - started
        save()
    print("전체 기록", output, report["state"], flush=True)
    return 0 if report["state"] == "COMPLETED_LIMITED_REPEATS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
