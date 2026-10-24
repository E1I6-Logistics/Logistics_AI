"""기존 시뮬·알고리즘과 시뮬·AI의 우리 KEV 교통조정을 비교한다.

두 조건에 같은 주문·출발 위치·주행 설정을 적용한다. 별도 시뮬 서버만 사용하며
현재 화면 서버에는 조회만 한다. 기본은 준비이며 --run일 때 한 번씩 실행한다.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from time import monotonic, sleep
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests.order_traffic_load_comparison import CATALOG, make_orders
from tests.order_traffic_policy_comparison import SOURCE_PATHS, api, digest, load_lines, positions, summarize
from tests.order_traffic_strategy_smoke import load_schedule, validate_mode

ARMS = ("NATIVE_ALGORITHM", "OUR_KEV")
LABELS = {"NATIVE_ALGORITHM": "시뮬·알고리즘: 기존 관제", "OUR_KEV": "시뮬·AI: 우리 KEV"}
EXTRA_SOURCE = ("tests/order_traffic_native_mode_comparison.py", "tests/order_traffic_load_comparison.py",
                "tests/order_traffic_load_cases.json", "backend/app/services/order_dispatcher.py",
                "backend/app/services/mode_service.py", "backend/app/routers/orders.py")


def operation_for(arm):
    if arm not in ARMS:
        raise ValueError("지원하지 않는 비교 조건입니다.")
    return "algorithm" if arm == "NATIVE_ALGORITHM" else "ai"


def order_identity(orders, operation):
    """실제 저장된 주문이 요청한 분기에서 실행됐는지 확인한다."""
    return bool(orders) and all(
        row.get("mode") == "simulation" and row.get("operation_mode") == operation
        and row.get("driving_mode") == ("standard" if operation == "algorithm" else "llm")
        and row.get("selector") == (None if operation == "algorithm" else "kev")
        and row.get("model") == (None if operation == "algorithm" else "kev-08b")
        for row in orders)


def visible():
    return {path: api("http://127.0.0.1:8000", path)
            for path in ("/api/mode", "/api/orders", "/api/robots", "/api/scenarios/status")}


def observe_visible():
    """사용자의 별도 기능 시험 중에는 관제의 시작·종료도 관측 정보로만 남긴다."""
    try:
        return visible()
    except OSError as exc:
        return {"unavailable": f"{type(exc).__name__}: {exc}"}


def visible_signature(value):
    if "/api/robots" not in value:
        return value
    return {**value, "/api/robots": positions(value["/api/robots"])}


def traffic_branch_verified(operation, decisions, traces):
    if operation == "algorithm":
        return not decisions and not traces
    # 유효 전략이 하나뿐이면 AI 모드에서도 실제 모델 호출 없이 적용할 수 있다.
    return bool(decisions) and all(row.get("selection_policy") == "KEV_STRATEGY_ORDER"
                                  and row.get("route_source") == "EXISTING_SHORTEST_PATH" for row in decisions)


def render(report):
    lines = ["# 기존 시뮬·알고리즘과 우리 시뮬·AI 비교", "", report["state"], "",
             "기존 알고리즘은 기존 예약·교통 제어를 그대로 사용한다. 우리 AI는 A* 경로＋KEV 전략 순서다.",
             "시뮬·AI 안에서 코드 선택과 KEV 선택만 바꾸던 이전 시험과 구분한다.", "",
             "| 모드 | 작업 완료 | 전체 작업 끝 | 평균 주문 시간 | 홈 복귀까지 | 모델 호출 |",
             "| --- | ---: | ---: | ---: | ---: | ---: |"]
    seconds = lambda value: "미완료" if value is None else f"{value:.2f}초"
    for row in report["trials"]:
        s = row["summary"]
        lines.append(f"| {LABELS[row['arm']]} | {s['completed_orders']}/{s['expected_orders']} | "
                     f"{seconds(s['work_completion_seconds'])} | {seconds(s['mean_order_seconds'])} | "
                     f"{seconds(s['drain_seconds'])} | {s['model_calls']} |")
    lines.extend(["", "각 모드 한 번씩의 전체 시스템 비교다. AI 판단 하나만의 효과나 장시간 처리량 우위를 확정하지 않는다.",
                  "평균 주문 시간: 접수 응답부터 작업대 작업 완료까지. 홈 복귀는 별도 집계한다.",
                  "약 0.2초 간격으로 노드·물리 엣지 점유를 관측한다. 표본 사이의 모든 순간을 증명하는 검사는 아니다.",
                  "실기체는 이번 시험에 포함하지 않는다. 실패하면 기록을 남기고 다음 조건을 중단한다."])
    if report.get("allow_visible_changes"):
        lines.append("사용자는 별도 관제에서 다른 기능을 시험할 수 있다. 화면 상태 변화는 관측만 하며 본 비교의 실패로 처리하지 않는다.")
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8035)
    parser.add_argument("--case", choices=("pickup_ab", "mixed_pickups", "fast_queue"), default="mixed_pickups")
    parser.add_argument("--seed", type=int, default=137)
    parser.add_argument("--schedule-file", type=Path)
    parser.add_argument("--model-host", default="http://127.0.0.1:8012")
    parser.add_argument("--arms", nargs=2, choices=ARMS, default=list(ARMS))
    parser.add_argument("--allow-visible-changes", action="store_true")
    args = parser.parse_args()
    if not 8020 <= args.port <= 8099:
        parser.error("별도 시뮬 시험 포트는 8020~8099를 사용하세요.")
    if set(args.arms) != set(ARMS):
        parser.error("알고리즘과 AI를 각각 한 번 지정하세요.")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    schedule = output / "schedule.json"
    if args.schedule_file:
        rows = [dict(at_seconds=t, items=items) for t, items in load_schedule(args.schedule_file)]
    else:
        catalog = json.loads(CATALOG.read_text())
        case = next(row for row in catalog["cases"] if row["id"] == args.case)
        rows = make_orders(case, args.seed, catalog["orders_per_case"])
    if rows[-1]["at_seconds"] >= 300:
        parser.error("마지막 접수 시각이 300초 시험 한도보다 짧아야 합니다.")
    schedule.write_text(json.dumps(dict(orders=rows), ensure_ascii=False, indent=2) + "\n")
    hashes = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
              for name in (*SOURCE_PATHS, *EXTRA_SOURCE)}
    for name in hashes:
        destination = output / "source" / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((ROOT / name).read_bytes())
    report = dict(state="PREPARED", arms=args.arms, case="CUSTOM" if args.schedule_file else args.case, seed=args.seed,
                  trials=[], errors=[], source_sha256=hashes, trial_timeout_seconds=300,
                  allow_visible_changes=args.allow_visible_changes,
                  repeat_count_per_arm=1, visible_server_commands_sent=False, real_robot_commands_sent=False)

    def save():
        (output / "comparison.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
        (output / "summary.md").write_text(render(report))

    save()
    if not args.run:
        print("두 모드 비교 준비 완료", output, flush=True)
        return 0
    from backend.app.services.route_selector.kev_selector import KevSelector
    selector = KevSelector(model="kev-08b", host=args.model_host, timeout_seconds=5)
    report["model_runtime"] = selector._ensure_runtime()
    report["model_before"] = api(args.model_host, "/v1/models")["models"][0]["batches"]
    report["visible_before"] = observe_visible() if args.allow_visible_changes else visible()
    report["warmup"] = selector.select_choice({"purpose": "NATIVE_MODE_COMPARISON_WARMUP"},
        {"READY": "Ready.", "NOT_READY": "Not ready."}, "Choose READY.")
    started = monotonic()
    report["state"] = "RUNNING"
    save()
    for arm in args.arms:
        operation = operation_for(arm)
        folder = output / arm
        folder.mkdir()
        run_id, child = uuid4().hex, None
        try:
            if any(hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != sha for name, sha in hashes.items()):
                raise RuntimeError("시험 중 소스가 변경돼 중단합니다.")
            with socket.socket() as probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                probe.bind(("127.0.0.1", args.port))
            before = api(args.model_host, "/v1/models")["models"][0]["batches"]
            env = dict(os.environ, PYTHONNOUSERSITE="1", FMS_ROBOT_MODE="simulation",
                FMS_PREVIEW_OPERATION_MODE=operation, FMS_ORDER_TRAFFIC_ENABLED="1",
                FMS_ORDER_ROUTE_SOURCE="EXISTING_SHORTEST_PATH", FMS_TRAFFIC_SELECTION_POLICY="KEV_STRATEGY_ORDER",
                FMS_PREVIEW_LOG_DIR=str(folder), FMS_PREVIEW_RUN_ID=run_id, KEV_HOST=args.model_host,
                FMS_ROUTE_GRAPH="test.geojson", FMS_MAP_YAML="logitle_map.yaml",
                FMS_TRAFFIC_REROUTE_LIMIT="2", FMS_TRAFFIC_SELECTION_SECONDS="10", FMS_TRAFFIC_WAIT_SECONDS="60",
                FMS_TRAFFIC_STRATEGY_CALL_LIMIT="8", FMS_SIMULATION_WORK_SECONDS="1.5")
            with (folder / "backend.log").open("wb") as log:
                child = subprocess.Popen([sys.executable, "-m", "uvicorn", "backend.simulation.order_traffic_preview:app",
                    "--host", "127.0.0.1", "--port", str(args.port)], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
            (folder / "backend.pid").write_text(str(child.pid) + "\n")
            host = f"http://127.0.0.1:{args.port}"
            for _ in range(100):
                if child.poll() is not None:
                    raise RuntimeError("시뮬 비교 서버 시작 실패")
                try:
                    mode = api(host, "/api/mode")
                    if mode.get("simulation_preview_run_id") == run_id:
                        break
                except OSError:
                    pass
                sleep(.1)
            else:
                raise RuntimeError("시뮬 비교 서버 시작 시간 초과")
            validate_mode(mode, operation, "KEV_STRATEGY_ORDER", run_id)
            print("비교 시작", LABELS[arm], flush=True)
            with (folder / "watcher.log").open("wb") as log:
                watcher = subprocess.run([sys.executable, str(ROOT / "tests/order_traffic_strategy_smoke.py"),
                    "--host", host, "--operation-mode", operation, "--policy", "KEV_STRATEGY_ORDER", "--run-id", run_id,
                    "--schedule-file", str(schedule), "--output", str(folder / "trial"), "--timeout", "300"],
                    cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=320)
            if watcher.returncode:
                raise RuntimeError("주문·복귀 시험 실패. 원문을 보존하고 중단합니다.")
        except Exception as exc:
            report["errors"].append(f"{arm}: {type(exc).__name__}: {exc}")
        finally:
            if child is not None and child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
            if (folder / "trial/report.json").exists():
                raw = json.loads((folder / "trial/report.json").read_text())
                traces = load_lines(folder / "order-traffic/selection-inferences.jsonl")
                decisions = load_lines(folder / "order-traffic/decisions.jsonl")
                summary = summarize(raw, load_lines(folder / "trial/observations.jsonl"), decisions, traces)
                # 기존 알고리즘은 새 교통조정의 기록을 만들지 않는다. 실제 이동 수 0이라는 뜻이 아니다.
                summary["added_traffic_decisions"] = summary.pop("applied_routes")
                after = api(args.model_host, "/v1/models")["models"][0]["batches"]
                identity = order_identity(raw.get("orders", []), operation)
                branch = traffic_branch_verified(operation, decisions, traces)
                report["trials"].append(dict(arm=arm, operation_mode=operation, mode=raw["mode"], state=raw["state"],
                    run_id=run_id, summary=summary, order_identity_verified=identity, branch_verified=branch,
                    server_requests_delta=after["requests"]-before["requests"], server_stopped=child is not None and child.poll() is not None))
                if not identity or not branch:
                    report["errors"].append(arm + ": 주문 모드 또는 교통조정 분기 검증 실패")
                print("비교 종료", LABELS[arm], raw["state"], flush=True)
            save()
        if report["errors"]:
            break
    report["visible_after"] = observe_visible() if args.allow_visible_changes else visible()
    report["visible_state_changed"] = visible_signature(report["visible_before"]) != visible_signature(report["visible_after"])
    report["model_after"] = api(args.model_host, "/v1/models")["models"][0]["batches"]
    summaries = [row["summary"] for row in report["trials"]]
    report["checks"] = dict(
        both_completed=len(summaries) == 2 and all(s["all_work_completed"] and s["all_returns_completed"] and s["all_robots_home"] for s in summaries),
        same_schedule=len({s["schedule_sha256"] for s in summaries}) == 1,
        same_start=len({digest(s["initial_positions"]) for s in summaries}) == 1,
        same_map=len({s["map_sha256"] for s in summaries}) == 1,
        same_settings=len({digest(s["simulation_settings"]) for s in summaries}) == 1,
        timely_registration=all(s["max_registration_lateness_seconds"] <= .5 for s in summaries),
        no_failures=all(s["failed_orders"] == 0 for s in summaries),
        no_observed_capacity_duplicates=all(s["capacity_violations"] == 0 for s in summaries),
        branches_and_order_modes=all(row["order_identity_verified"] and row["branch_verified"] for row in report["trials"]),
        model_counters_match=all(row["server_requests_delta"] == row["summary"]["model_calls"] for row in report["trials"]),
        model_responses_complete=all(s["model_calls"] == s["model_responses"] and s["model_errors"] == 0 for s in summaries),
        calls_only_in_ai=all(row["arm"] == "OUR_KEV" or row["summary"]["model_calls"] == 0 for row in report["trials"]),
        total_server_counter_matches=report["model_after"]["requests"]-report["model_before"]["requests"] == 1+sum(s["model_calls"] for s in summaries),
        own_servers_stopped=all(row["server_stopped"] for row in report["trials"]),
        sources_unchanged=all(hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == sha for name, sha in hashes.items()))
    if not args.allow_visible_changes:
        report["checks"]["visible_unchanged"] = not report["visible_state_changed"]
    report["state"] = "COMPLETED_ONE_COMPARISON_PASS" if not report["errors"] and all(report["checks"].values()) else "STOPPED_REVIEW_REQUIRED"
    report["elapsed_seconds"] = monotonic()-started
    save()
    print("비교 기록", output, report["state"], flush=True)
    return 0 if report["state"] == "COMPLETED_ONE_COMPARISON_PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
