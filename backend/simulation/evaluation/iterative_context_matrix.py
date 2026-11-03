"""JSON 활성 모델의 8가지 context를 순차 평가하고 비교표·진행률을 저장한다.

--check는 계획만 검증한다. 실제 실행은 모델별 자식 프로세스를 사용하며,
KEV 서버는 전용 포트에서 실행기가 시작한 프로세스만 종료한다.
"""
from __future__ import annotations

import argparse
import contextlib
import csv
import hashlib
import io
import json
import os
import random
import signal
import socket
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from backend.app.services.iterative_route_policy import (
    CONTEXT_PROTOCOL, NEXT_NODE_INSTRUCTIONS, STATE_PROFILE_FACTORS, STATE_PROFILES,
)
from backend.simulation.evaluation import selector_iterative_route_benchmark as bench

ROOT = Path(__file__).resolve().parents[3]
CONFIG = ROOT / "backend/simulation/evaluation/selector_iterative_benchmark.json"
PROFILES = STATE_PROFILES
LABELS = ("필수", "기존(필수+이력)", "필수+직선거리", "필수+방위·접근량",
          "필수+직선거리+방위·접근량", "기존+직선거리", "기존+방위·접근량", "기존+직선거리+방위·접근량")
SOURCES = [ROOT / f"backend/app/services/{name}.py" for name in
           ("iterative_route_policy", "iterative_route_runner", "route_service")]
SOURCES += sorted((ROOT / "backend/app/services/route_selector").glob("*.py"))
SOURCES += sorted((ROOT / "backend/app/services/llm_providers").glob("*.py"))
SOURCES += [Path(bench.__file__), Path(__file__)]


def now():
    return datetime.now(ZoneInfo("Asia/Seoul")).isoformat()


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def http(url, payload=None, timeout=10):
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def model_key(spec):
    return spec["selector"] + "-" + spec["name"].replace("/", "--").replace(":", "-")


def source_hashes():
    return {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in SOURCES}


def build_plan(config_path=CONFIG, selectors=None, models=None, order_seed=20260928):
    """모델을 불러오지 않고 JSON·지도·전체 사례·8조건을 검증한다."""
    config_path = Path(config_path).resolve()
    config = json.loads(config_path.read_text(encoding="utf-8"))
    resolved = bench.load_iterative_config(config_path, state_profile="minimal", allow_empty_models=True)
    settings = resolved["settings"]
    if (settings["case_mode"], settings["candidate_scope"], settings["context_mode"]) != (
        "all-pairs", "neighbors", "neighbor_context"
    ):
        raise ValueError("8조건 전체 경로 비교에는 all-pairs / neighbors / neighbor_context를 지정하세요")
    if int(settings["repeats"]) < 1 or int(settings["warmups"]) < 0:
        raise ValueError("repeats는 1 이상, warmups는 0 이상이어야 합니다")
    if settings["max_steps"] is not None and int(settings["max_steps"]) < 1:
        raise ValueError("max_steps는 1 이상이어야 합니다")
    specs = []
    for item in config.get("decision_models", []):
        if item.get("enabled") is not True:
            continue
        if item.get("selector") not in {"laya", "kev"} or not item.get("name"):
            raise ValueError(f"invalid decision model: {item}")
        specs.append({"selector": item["selector"], "name": item["name"]})
    specs += [{"selector": "ollama", "name": item["name"], "ollama": item} for item in resolved["models"]]
    if len({model_key(spec) for spec in specs}) != len(specs):
        raise ValueError("duplicate enabled model")
    if selectors:
        specs = [spec for spec in specs if spec["selector"] in selectors]
    if models:
        missing = set(models) - {spec["name"] for spec in specs}
        if missing:
            raise ValueError("선택한 모델이 활성화되어 있지 않습니다: " + ", ".join(sorted(missing)))
        specs = [spec for spec in specs if spec["name"] in models]
    if not specs:
        raise ValueError("활성화된 실험 모델이 없습니다")
    # 사례 생성·기준 경로 계산은 측정 전에 수행하고 모든 모델에 같은 사례를 사용한다.
    with contextlib.redirect_stdout(io.StringIO()):
        prepared = bench._prepare(graph_path=resolved["graph_path"], case_mode="all-pairs",
                                  case_count=int(settings["case_count"]), route_seed=settings["route_seed"])
    cases = [{"start": case["start"], "target": case["target"]} for case in prepared["cases"]]
    order = list(PROFILES)
    random.Random(order_seed).shuffle(order)
    # 모델마다 같은 조건 순서를 사용하고 실제 순서와 시드를 기록한다.
    settings = {key: value for key, value in settings.items()
                if key not in {"state_profile", "context_factors"}}
    return {
        "context_protocol": CONTEXT_PROTOCOL, "config_path": str(config_path),
        "config_sha256": resolved["config_sha256"], "graph_path": str(resolved["graph_path"]),
        "graph_sha256": resolved["graph_sha256"], "settings": settings,
        "profiles": list(PROFILES), "profile_labels": dict(zip(PROFILES, LABELS)),
        "profile_factors": STATE_PROFILE_FACTORS, "execution_order": order, "order_seed": order_seed,
        "models": specs, "case_count": len(cases), "cases": cases,
        "routes_per_condition": len(cases) * int(settings["repeats"]),
        "routes_per_model": len(cases) * int(settings["repeats"]) * len(PROFILES),
        "planned_conditions": len(specs) * len(PROFILES),
        "planned_routes": len(specs) * len(cases) * int(settings["repeats"]) * len(PROFILES),
    }


class ServiceAbort(BaseException):
    """인프라 장애·입력 잘림을 경로 선택 오류로 집계하지 않는다."""


class CheckedSelector:
    def __init__(self, selector):
        self.inner = selector

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def select_choice(self, *args, **kwargs):
        try:
            result = self.inner.select_choice(*args, **kwargs)
        except Exception as exc:
            message = str(exc).lower()
            words = ("out of memory", "connection refused", "timed out", "timeout", "urlopen",
                     "http 500", "http 503", "http 404", "cuda error", "cpu에서 처리")
            if any(word in message for word in words):
                raise ServiceAbort(str(exc)) from exc
            raise
        if result.get("state_truncated") is True:
            raise ServiceAbort("입력 잘림 감지: 전체 context 비교 결과로 집계하지 않음")
        return result


def worker(output, spec):
    protocol = json.loads((output / "protocol.json").read_text())
    settings = protocol["settings"]
    model_dir = output / model_key(spec)
    for profile in protocol["execution_order"]:
        if source_hashes() != protocol["source_sha256"]:
            raise RuntimeError("실험 중 소스 변경 감지: 같은 결과 폴더에서 코드를 혼합하지 않음")
        if hashlib.sha256(Path(protocol["graph_path"]).read_bytes()).hexdigest() != protocol["graph_sha256"]:
            raise RuntimeError("실험 중 지도 변경 감지")
        save(model_dir / "worker_status.json", {"status": "running", "profile": profile, "updated_at": now()})
        if spec["selector"] == "kev":
            from backend.app.services.route_selector.kev_selector import KevSelector
            selector = KevSelector(model=spec["name"], host=protocol["kev_host"], require_cuda=True)
        elif spec["selector"] == "laya":
            from backend.app.services.route_selector.laya_selector import LayaSelector
            selector = LayaSelector(model=spec["name"], max_length=8192, require_cuda=True)
        else:
            selector = bench._build_configured_ollama_selector(spec["ollama"])
        try:
            result = bench.run_benchmark(
                model_dir / profile, graph_path=Path(protocol["graph_path"]),
                case_mode=settings["case_mode"], case_count=int(settings["case_count"]),
                route_seed=settings["route_seed"], repeats=int(settings["repeats"]),
                warmups=int(settings["warmups"]), max_steps=settings["max_steps"],
                deadline=float(settings["deadline_seconds"]), candidate_scope=settings["candidate_scope"],
                context_mode=settings["context_mode"], state_profile=profile,
                selector=CheckedSelector(selector), progress=True,
            )
            if result["overall"]["trial_count"] != protocol["routes_per_condition"]:
                raise RuntimeError("측정 경로 수 불일치")
            if result["settings"]["generated_cases"] != protocol["cases"]:
                raise RuntimeError("출발·목적지 사례 불일치")
            if spec["selector"] == "ollama":
                save(model_dir / profile / "ollama_placement.json", http(spec["ollama"]["host"] + "/api/ps"))
        finally:
            release = getattr(selector, "release", None)
            if callable(release):
                release()
    save(model_dir / "worker_status.json", {"status": "complete", "updated_at": now()})


def jobs_for(plan):
    return [dict(selector=spec["selector"], name=spec["name"], model_key=model_key(spec),
                 profile=profile, status="queued", completed_routes=0,
                 total_routes=plan["routes_per_condition"])
            for spec in plan["models"] for profile in plan["profiles"]]


def snapshot_jobs(output, jobs):
    for job in jobs:
        if job["status"] == "complete":
            continue
        directory = output / job["model_key"] / job["profile"]
        manifest_path, summary_path = directory / "manifest.json", directory / bench.SUMMARY_FILENAME
        if summary_path.exists() and manifest_path.exists():
            manifest = json.loads(manifest_path.read_text())
            if manifest.get("status") == "complete":
                value = json.loads(summary_path.read_text())
                if value["settings"].get("context_protocol") != CONTEXT_PROTOCOL:
                    raise RuntimeError("이전 context 프로토콜의 결과 혼합 감지")
                job.update(status="complete", completed_routes=value["overall"]["trial_count"],
                           summary=value["overall"], device=value["device"])
                continue
        if manifest_path.exists() and job["status"] not in {"failed", "interrupted"}:
            job["status"] = "running"
            trials = directory / bench.TRIALS_FILENAME
            if trials.exists():
                with trials.open() as stream:
                    job["completed_routes"] = sum(bool(line.strip()) for line in stream)
    return jobs


def comparison_rows(jobs):
    rows = []
    for job in jobs:
        row = {key: job[key] for key in ("name", "selector", "profile", "status", "completed_routes", "total_routes")}
        row.update(STATE_PROFILE_FACTORS[job["profile"]])
        row["note"] = job.get("reason", "")
        summary = job.get("summary")
        if summary:
            count = summary["trial_count"]
            row.update(arrival_count=summary["reached_target_count"], shortest_count=summary["distance_match_count"],
                       arrival_percent=summary["reached_target_count"] / count * 100,
                       shortest_percent=summary["distance_match_count"] / count * 100,
                       mean_api_calls=summary["mean_api_call_count"])
            for key, source, scale in (("decision_mean_ms", "step_wall_latency", 1000),
                                       ("decision_p95_ms", "step_wall_latency", 1000),
                                       ("route_mean_s", "all_trial_wall_latency", 1),
                                       ("route_p95_s", "all_trial_wall_latency", 1),
                                       ("successful_route_mean_s", "wall_latency", 1)):
                metric = "p95_seconds" if "p95" in key else "mean_seconds"
                value = summary[source].get(metric)
                row[key] = value * scale if value is not None else None
            row["device"] = json.dumps(job.get("device", {}), ensure_ascii=False)
        rows.append(row)
    return rows


def write_csv(path, rows, base_fields):
    fields = list(base_fields) + sorted({key for row in rows for key in row} - set(base_fields))
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def report(output, jobs, status, protocol):
    state = {"status": status, "updated_at": now(), "context_protocol": CONTEXT_PROTOCOL,
             "completed_conditions": sum(job["status"] == "complete" for job in jobs),
             "planned_conditions": len(jobs), "jobs": jobs}
    save(output / "progress.json", state)
    rows = comparison_rows(jobs)
    write_csv(output / "comparison.csv", rows, ("name", "selector", "profile", "status"))
    doc = ["# 8가지 context · 활성 모델 비교", "", "갱신: " + state["updated_at"],
           "상태: " + status, "", f"프로토콜: {CONTEXT_PROTOCOL}. 모델당 8조건 × {protocol['case_count']}쌍 × {protocol['settings']['repeats']}회.",
           "직선거리는 후보 dist_to_target만 제공하며 target_distance는 모든 조건에서 제외한다.",
           "방위·접근량은 하나의 묶음이며 접근량은 직선거리에서 파생된 입력 표현이다.", "",
           "| 모델 | context | 상태 | 도착률 % | 최단 일치율 % | 호출당 ms | 전체 경로 s |",
           "| --- | --- | --- | ---: | ---: | ---: | ---: |"]
    def display(row, key):
        value = row.get(key)
        return f"{value:.3f}" if isinstance(value, (int, float)) else "—"
    for row in rows:
        doc.append(f"| {row['name']} | {row['profile']} | {row['status']} {row['completed_routes']}/{row['total_routes']} | "
                   f"{display(row, 'arrival_percent')} | {display(row, 'shortest_percent')} | "
                   f"{display(row, 'decision_mean_ms')} | {display(row, 'route_mean_s')} |")
    doc += ["", "도착·최단 일치율의 분모는 실패를 포함한 전체 측정 경로 수다.",
            "경로 시간은 후보/context 계산·반복 모델 호출·경로 검증을 포함하며 지도 로딩·결과 파일 저장은 제외한다.",
            "전체 경로 평균은 실패도 포함한다. 성공 경로 평균과 p95, 호출 횟수는 comparison.csv에 별도 기록한다.",
            "호출당 시간은 selector 호출의 경과 시간이며 순수 GPU 연산 시간과 다르다.",
            "실제 모델 입력은 trials의 steps[].model_input에 기록한다. 후보가 하나인 단계는 모델을 호출하지 않는다.",
            "동일 사례를 반복한 결과이며 반복 횟수만큼 독립적인 지도 사례가 늘어나는 것은 아니다.",
            "조건 순서는 시드로 섞어 기록하지만 시간·열·캐시의 순서 효과가 완전히 제거되지는 않는다.",
            "전체 경로 비교이므로 앞선 선택이 달라지면 이후 상태·후보도 달라질 수 있다.", ""]
    doc += [f"- {job['name']} / {job['profile']}: {job['reason']}" for job in jobs if job.get("reason")]
    (output / "report.md").write_text("\n".join(doc) + "\n", encoding="utf-8")


def write_side_by_side(output, jobs):
    wide = {}
    for job in jobs:
        if job["status"] != "complete":
            continue
        path = output / job["model_key"] / job["profile"] / bench.TRIALS_FILENAME
        with path.open() as stream:
            for line in stream:
                trial = json.loads(line)
                key = (trial["start_node"], trial["target_node"], trial["repeat"])
                row = wide.setdefault(key, dict(start=key[0], target=key[1], repeat=key[2]))
                prefix = job["model_key"] + "__" + job["profile"] + "__"
                times = [step["wall_seconds"] for step in trial["steps"] if step.get("model_called")]
                row.update({prefix + "arrived": trial["reached_target"], prefix + "shortest": trial["distance_match"],
                            prefix + "decision_ms": sum(times) / len(times) * 1000 if times else None,
                            prefix + "route_s": trial["wall_seconds"], prefix + "calls": trial["api_call_count"],
                            prefix + "path": json.dumps(trial["path"])})
    write_csv(output / "all_routes_side_by_side.csv", [wide[key] for key in sorted(wide)], ("start", "target", "repeat"))


def stop_process(process):
    """실행기가 시작한 자식 프로세스만 종료하고 다음 GPU 작업 전에 종료를 기다린다."""
    if process is None or process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass  # poll 직후 자식이 종료된 경우에도 wait로 회수한다.
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=10)


@contextlib.contextmanager
def kev_server(spec, protocol, directory):
    host, port = "127.0.0.1", protocol["kev_port"]
    with socket.socket() as probe:
        probe.bind((host, port))  # 다른 프로세스의 서버를 사용하거나 종료하지 않는다.
    repo = Path(protocol["kev_repo"]).expanduser()
    if not (repo / "kev/serve.py").is_file():
        raise FileNotFoundError(f"KEV 소스 저장소를 찾을 수 없습니다: {repo}; --kev-repo로 지정하세요")
    from backend.app.services.route_selector.kev_selector import KevSelector
    selector = KevSelector(model=spec["name"], host=protocol["kev_host"], require_cuda=True)
    checkpoint = selector.expected_run + "@v1.0"
    command = [sys.executable, "-u", "-m", "kev.serve", "--run", checkpoint,
               "--host", host, "--port", str(port)]
    env = os.environ.copy()
    env.update(KEV_BACKEND="torch", KEV_DTYPE="bf16", KEV_PREFIX_CACHE="0",
               KEV_CUDA_GRAPHS="0", KEV_FUSED="0", KEV_TRUNCATE_STATES="0", PYTHONUNBUFFERED="1")
    save(directory / "server_command.json", {"command": command, "checkpoint": checkpoint,
                                            "cwd": str(repo), "dtype": "bf16"})
    with (directory / "server.log").open("w") as log:
        process = subprocess.Popen(command, cwd=repo, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            deadline = time.monotonic() + protocol["server_start_timeout"]
            while True:
                if process.poll() is not None:
                    raise RuntimeError(f"KEV 서버 시작 실패(exit={process.returncode}); {directory / 'server.log'} 확인")
                try:
                    runtime = selector._ensure_runtime()
                except (OSError, TimeoutError, RuntimeError) as exc:
                    # 연결 실패를 감싼 RuntimeError만 재시도하고 모델·CUDA 검증 오류는 즉시 알린다.
                    if isinstance(exc, RuntimeError) and (
                        not isinstance(exc.__cause__, urllib.error.URLError)
                        or isinstance(exc.__cause__, urllib.error.HTTPError)
                    ):
                        raise
                    if time.monotonic() >= deadline:
                        raise RuntimeError("KEV 서버 준비 시간 초과; server.log 확인") from exc
                    time.sleep(1)
                    continue
                save(directory / "server_runtime.json", runtime)
                break
            yield
        finally:
            stop_process(process)


def run_model(output, spec, protocol, jobs):
    directory = output / model_key(spec)
    directory.mkdir(parents=True, exist_ok=True)
    server = kev_server(spec, protocol, directory) if spec["selector"] == "kev" else contextlib.nullcontext()
    try:
        with server, (directory / "worker.log").open("w") as log:
            env = os.environ.copy()
            env.update(USE_TF="0", PYTHONUNBUFFERED="1", LAYA_MAX_LENGTH="8192", LAYA_REQUIRE_CUDA="1", KEV_REQUIRE_CUDA="1")
            command = [sys.executable, "-u", "-m", __name__ if __name__ != "__main__" else
                       "backend.simulation.evaluation.iterative_context_matrix",
                       "--output", str(output), "--worker", json.dumps(spec)]
            process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                last_progress = None
                while process.poll() is None:
                    snapshot_jobs(output, jobs)
                    report(output, jobs, "running", protocol)
                    signature = [(job["profile"], job["completed_routes"], job["status"])
                                 for job in jobs if job["model_key"] == model_key(spec)]
                    if signature != last_progress:
                        done = sum(job["completed_routes"] for job in jobs if job["model_key"] == model_key(spec))
                        print(f"{spec['name']}: {done}/{protocol['routes_per_model']} routes", flush=True)
                        last_progress = signature
                    time.sleep(5)
                if process.returncode != 0:
                    status_path = directory / "worker_status.json"
                    failure = json.loads(status_path.read_text()).get("error") if status_path.exists() else "worker log 확인"
                    raise RuntimeError(f"worker exit={process.returncode}: {failure}")
            finally:
                stop_process(process)
    finally:
        if spec["selector"] == "ollama":
            # 활성 모델을 일괄 시험할 때에는 GPU를 공유하므로, 시험한 모델을 다음 작업 전에 해제한다.
            http(spec["ollama"]["host"] + "/api/generate", {"model": spec["name"], "keep_alive": 0}, timeout=60)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=CONFIG)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--check", action="store_true", help="모델 호출 없이 JSON·지도·8조건·실행 계획 확인")
    parser.add_argument("--selector", action="append", choices=("kev", "laya", "ollama"))
    parser.add_argument("--model", action="append", help="활성 모델 중 일부만 실행; 여러 번 지정 가능")
    parser.add_argument("--order-seed", type=int, default=20260928)
    parser.add_argument("--kev-repo", type=Path, default=Path.home() / "kev")
    parser.add_argument("--kev-port", type=int, default=8021)
    parser.add_argument("--server-start-timeout", type=float, default=1800)
    parser.add_argument("--worker", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    os.chdir(ROOT)
    if args.worker:
        if args.output is None:
            parser.error("worker에는 --output이 필요합니다")
        spec, output = json.loads(args.worker), args.output.resolve()
        try:
            worker(output, spec)
        except BaseException as exc:
            save(output / model_key(spec) / "worker_status.json",
                 {"status": "failed", "error": f"{type(exc).__name__}: {exc}", "updated_at": now()})
            traceback.print_exc()
            return 1
        return 0
    plan = build_plan(args.config, args.selector, args.model, args.order_seed)
    if args.check:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0
    if not 1 <= args.kev_port <= 65535 or args.server_start_timeout <= 0:
        parser.error("KEV 포트·서버 준비 시간 설정을 확인하세요")
    output = (args.output or ROOT / "backend/simulation/benchmark_results" /
              ("context8-" + datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d-%H%M%S-%f"))).resolve()
    if output.exists() and any(output.iterdir()):
        raise FileExistsError(f"새 데이터 수집은 비어 있는 결과 폴더를 사용하세요: {output}")
    output.mkdir(parents=True, exist_ok=True)
    protocol = dict(plan, created_at=now(), source_sha256=source_hashes(),
                    instructions=NEXT_NODE_INSTRUCTIONS,
                    instructions_sha256=hashlib.sha256(NEXT_NODE_INSTRUCTIONS.encode()).hexdigest(),
                    kev_repo=str(args.kev_repo.expanduser().resolve()), kev_port=args.kev_port,
                    kev_host=f"http://127.0.0.1:{args.kev_port}", server_start_timeout=args.server_start_timeout)
    for label, command in (("source_commit", ["git", "rev-parse", "HEAD"]),
                           ("source_status", ["git", "status", "--short"]),
                           ("hardware", ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"])):
        try:
            protocol[label] = subprocess.check_output(command, cwd=ROOT, text=True, stderr=subprocess.STDOUT).strip()
        except (OSError, subprocess.CalledProcessError) as exc:
            protocol[label] = str(exc)
    save(output / "protocol.json", protocol)
    snapshot = json.loads(args.config.read_text(encoding="utf-8"))
    snapshot["graph"]["path"] = plan["graph_path"]
    save(output / "config_snapshot.json", snapshot)
    jobs = jobs_for(plan)
    report(output, jobs, "running", protocol)
    print(f"output={output}\nmodels={len(plan['models'])}, conditions={plan['planned_conditions']}, routes={plan['planned_routes']}", flush=True)
    status = "running"
    try:
        for spec in plan["models"]:
            print(f"=== {spec['selector']}: {spec['name']} ===", flush=True)
            try:
                run_model(output, spec, protocol, jobs)
            except Exception as exc:
                snapshot_jobs(output, jobs)
                failure = f"{type(exc).__name__}: {exc}"
                for job in jobs:
                    if job["model_key"] == model_key(spec) and job["status"] != "complete":
                        job.update(status="failed", reason=failure)
                print(f"FAILED {spec['name']}: {failure}", flush=True)
            snapshot_jobs(output, jobs)
            report(output, jobs, "running", protocol)
        status = "complete" if all(job["status"] == "complete" for job in jobs) else "finished_with_errors"
    except KeyboardInterrupt:
        status = "interrupted"
        for job in jobs:
            if job["status"] in {"queued", "running"}:
                job["status"] = "interrupted"
    finally:
        report(output, jobs, status, protocol)
        write_side_by_side(output, jobs)
    print(f"status={status}; report={output / 'report.md'}", flush=True)
    return 0 if status == "complete" else 130 if status == "interrupted" else 1


if __name__ == "__main__":
    raise SystemExit(main())
