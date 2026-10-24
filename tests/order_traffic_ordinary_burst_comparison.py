"""고정 코드 사본에서 보통 접수·중간 주문 폭증을 기존 알고리즘과 우리 AI로 비교한다.

사용자 관제에 명령하지 않는다. 주문·지도·코드를 별도로 보존하며 조건을
순차 실행한다. --run일 때만 전용 KEV와 시험 서버를 실행하고 종료한다.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
from random import Random
import signal
import socket
import subprocess
import sys
from time import monotonic, sleep

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests.order_traffic_policy_comparison import api, digest
from tests.order_traffic_native_mode_comparison import ARMS, LABELS, observe_visible
from tests.order_traffic_strategy_smoke import load_schedule


def ordinary_items(seed):
    """실제 통계 대신 단일 픽업대 8건·두 픽업대 4건으로 시험 구성을 고정한다."""
    rng = Random(seed)
    groups = ["A", "B", "AB", "C", "D", "CD", "A", "C", "AC", "AD", "BC", "BD"]
    rng.shuffle(groups)
    result = []
    for group in groups:
        items = dict.fromkeys("ABCD", 0)
        for key in group:
            items[key] += 1
        quantity = rng.randint(len(group), 5)
        for _ in range(quantity-len(group)):
            items[rng.choice(group)] += 1
        result.append(items)
    return result


def build_plan(seeds):
    rows = []
    for repetition, seed in enumerate(seeds):
        items = ordinary_items(seed)
        for index, case in enumerate(("ordinary", "burst")):
            times = [i*12 if case == "ordinary" or i < 3 else 36 for i in range(12)]
            rows.append(dict(id=f"{case}-seed-{seed}", case=case, seed=seed,
                arms=list(ARMS if (repetition+index) % 2 == 0 else reversed(ARMS)),
                item_sequence_sha256=digest(items),
                orders=[dict(at_seconds=t, items=dict(order)) for t, order in zip(times, items)]))
    return rows


def freeze_workspace(source, destination):
    """코드·지도 사본만 만들고 원본 파일은 수정하지 않는다."""
    destination.mkdir(parents=True, exist_ok=False)
    hashes = {}
    for name in ("backend", "tests", "routes", "maps"):
        for path in sorted((source/name).rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            relative = path.relative_to(source)
            data = path.read_bytes()
            target = destination/relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            hashes[str(relative)] = hashlib.sha256(data).hexdigest()
    return hashes


def frozen_unchanged(folder, hashes):
    return all((folder/name).is_file() and hashlib.sha256((folder/name).read_bytes()).hexdigest() == sha
               for name, sha in hashes.items())


def require_free_port(port):
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("127.0.0.1", port))


def render(report):
    lines = ["# 보통 주문과 주문 폭증: 기존 알고리즘·우리 AI 비교", "", report["state"], "",
             "같은 품목·수량·순서를 접수 시각만 바꿔 공급한다. 사용자 관제와 분리한 코드·지도 사본을 사용한다.", "",
             "| 상황·주문표 | 모드 | 완료 | 평균 주문 시간 | 전체 작업 끝 | 홈 복귀까지 | 모델 호출 | 최대 대기 주문 |",
             "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    show = lambda x: "미완료" if x is None else f"{x:.2f}초"
    for pair in report["pairs"]:
        for trial in pair["trials"]:
            s = trial["summary"]
            lines.append(f"| {pair['id']} | {LABELS[trial['arm']]} | {s['completed_orders']}/12 | "
                f"{show(s['mean_order_seconds'])} | {show(s['work_completion_seconds'])} | {show(s['drain_seconds'])} | "
                f"{s['model_calls']} | {s['max_pending']} |")
    lines.extend(["", "실제 주문 통계가 아닌 가정한 시험표이며, 주문표마다 각 모드 한 번씩 실행한다.",
                  "사용자의 다른 기능 시험과 컴퓨터 자원을 공유한다. 시간은 이번 실행의 관측값이며 확정적인 성능 우위 근거가 아니다.",
                  "실패 시 자동 재시험하지 않고 중단한다. 점유는 약 0.2초 간격 표본으로 확인한다."])
    return "\n".join(lines)+"\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seeds", type=int, nargs="+", default=[42, 137])
    parser.add_argument("--port", type=int, default=8035)
    parser.add_argument("--model-port", type=int, default=8012)
    args = parser.parse_args()
    if not 8020 <= args.port <= 8099 or not 8012 <= args.model_port <= 8019:
        parser.error("시험 백엔드 8020~8099, 시험 KEV 8012~8019 포트를 사용하세요.")
    if len(args.seeds) != len(set(args.seeds)) or not 1 <= len(args.seeds) <= 5:
        parser.error("서로 다른 주문표 번호 1~5개를 지정하세요.")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    plan = build_plan(args.seeds)
    for pair in plan:
        path = output/"schedules"/(pair["id"]+".json")
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(dict(orders=pair["orders"]), ensure_ascii=False, indent=2)+"\n")
        load_schedule(path)
        pair["schedule_file"] = str(path)
    frozen = output/"frozen"
    hashes = freeze_workspace(ROOT, frozen)
    (output/"frozen-source-sha256.json").write_text(json.dumps(hashes, indent=2)+"\n")
    report = dict(state="PREPARED", plan=plan, pairs=[], errors=[], frozen_root=str(frozen),
                  dedicated_model_port=args.model_port, backend_port=args.port,
                  concurrent_user_testing=True, visible_server_commands_sent=False,
                  real_robot_commands_sent=False, model_started_by_tool=False)

    def save():
        (output/"batch.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)+"\n")
        (output/"summary.md").write_text(render(report))

    save()
    if not args.run:
        print("분리된 시험 사본과 주문표 준비 완료", output, flush=True)
        return 0
    model = None
    started = monotonic()
    report["visible_before"] = observe_visible()
    report["state"] = "RUNNING"
    try:
        require_free_port(args.port)
        require_free_port(args.model_port)
        env = dict(os.environ, PYTHONNOUSERSITE="1", HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                   KEV_BACKEND="torch", KEV_DTYPE="bf16", KEV_PREFIX_CACHE="0", KEV_CUDA_GRAPHS="0", KEV_FUSED="0")
        with (output/"kev.log").open("wb") as log:
            model = subprocess.Popen([sys.executable, "-m", "kev.serve", "--run", "jaredpalmer/kev-0.8b@v1.0",
                "--device", "cuda", "--port", str(args.model_port)], cwd=ROOT.parent/"kev", env=env,
                stdout=log, stderr=subprocess.STDOUT)
        report.update(model_started_by_tool=True, model_pid=model.pid)
        (output/"kev.pid").write_text(str(model.pid)+"\n")
        save()
        host = f"http://127.0.0.1:{args.model_port}"
        for _ in range(180):
            if model.poll() is not None:
                raise RuntimeError("전용 KEV 시작 실패. kev.log를 확인하세요.")
            try:
                report["model_runtime_before"] = api(host, "/v1/models")
                break
            except OSError:
                sleep(1)
        else:
            raise RuntimeError("전용 KEV 준비 시간 초과")
        for pair in plan:
            if not frozen_unchanged(frozen, hashes):
                raise RuntimeError("고정한 시험 코드·지도가 변경돼 중단합니다.")
            print("상황 시작", pair["id"], "→".join(pair["arms"]), flush=True)
            worker = None
            folder = output/pair["id"]
            try:
                with (output/(pair["id"]+".log")).open("wb") as log:
                    worker = subprocess.Popen([sys.executable, str(frozen/"tests/order_traffic_native_mode_comparison.py"),
                        "--run", "--output", str(folder), "--port", str(args.port), "--model-host", host,
                        "--schedule-file", pair["schedule_file"], "--seed", str(pair["seed"]),
                        "--allow-visible-changes", "--arms", *pair["arms"]], cwd=frozen, env=env,
                        stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                code = worker.wait(timeout=660)
            except BaseException:
                if worker is not None:
                    try:
                        os.killpg(worker.pid, signal.SIGTERM)
                        worker.wait(timeout=10)
                    except (ProcessLookupError, subprocess.TimeoutExpired):
                        try:
                            os.killpg(worker.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                raise
            result = json.loads((folder/"comparison.json").read_text())
            report["pairs"].append(dict(id=pair["id"], case=pair["case"], seed=pair["seed"],
                item_sequence_sha256=pair["item_sequence_sha256"], folder=str(folder),
                state=result["state"], checks=result.get("checks", {}), trials=result["trials"], errors=result["errors"]))
            save()
            print("상황 종료", pair["id"], result["state"], flush=True)
            if code or result["state"] != "COMPLETED_ONE_COMPARISON_PASS":
                raise RuntimeError("실패 또는 조건 불일치가 있어 남은 시험을 중단합니다.")
        report["model_runtime_after"] = api(host, "/v1/models")
        report["state"] = "COMPLETED_INITIAL_ORDINARY_BURST_COMPARISON"
    except Exception as exc:
        report["state"] = "STOPPED_REVIEW_REQUIRED"
        report["errors"].append(f"{type(exc).__name__}: {exc}")
    finally:
        if model is not None and model.poll() is None:
            model.terminate()
            try:
                model.wait(timeout=15)
            except subprocess.TimeoutExpired:
                model.kill()
                model.wait(timeout=5)
        report["own_model_stopped"] = model is None or model.poll() is not None
        report["frozen_sources_unchanged"] = frozen_unchanged(frozen, hashes)
        report["visible_after"] = observe_visible()
        report["elapsed_seconds"] = monotonic()-started
        if not report["frozen_sources_unchanged"]:
            report["state"] = "STOPPED_REVIEW_REQUIRED"
            report["errors"].append("시험용 코드·지도 사본이 변경됐습니다.")
        save()
    print("시험 기록", output, report["state"], flush=True)
    return 0 if report["state"] == "COMPLETED_INITIAL_ORDINARY_BURST_COMPARISON" else 1


if __name__ == "__main__":
    raise SystemExit(main())
