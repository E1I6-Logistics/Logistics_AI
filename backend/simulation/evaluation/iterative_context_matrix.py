"""네 context를 활성 모델별로 순차 평가하고 진행률·비교표를 저장한다."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
import traceback
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[3]
PROFILES = ("legacy", "minimal", "minimal_distance", "legacy_compass_distance")
LABELS = ("기존 기본", "필수만", "필수+후보 직선거리", "기존 기본+방위·거리·접근량")
CONFIG = ROOT / "backend/simulation/evaluation/selector_iterative_benchmark.json"
SOURCES = [
    ROOT / "backend/app/services/iterative_route_policy.py",
    ROOT / "backend/app/services/iterative_route_runner.py",
    ROOT / "backend/simulation/evaluation/selector_iterative_route_benchmark.py",
    Path(__file__),
]


def now():
    return datetime.now(ZoneInfo("Asia/Seoul")).isoformat()


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def http(url, payload=None, timeout=10):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)


def model_key(spec):
    return spec["selector"] + "-" + spec["name"].replace("/", "--").replace(":", "-")


def line_count(path):
    if not path.exists():
        return 0
    with path.open() as stream:
        return sum(bool(line.strip()) for line in stream)


def source_hashes():
    return {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in SOURCES}


class ServiceAbort(BaseException):
    """인프라 실패를 경로 정확도 실패로 집계하지 않는다."""


class CheckedSelector:
    def __init__(self, selector):
        self.inner = selector

    def __getattr__(self, key):
        return getattr(self.inner, key)

    def select_choice(self, *args, **kwargs):
        try:
            result = self.inner.select_choice(*args, **kwargs)
        except Exception as exc:
            message = str(exc).lower()
            service_words = ("out of memory", "connection refused", "timed out", "timeout",
                             "urlopen", "http 500", "http 503", "http 404",
                             "cuda error", "cpu에서 처리")
            if any(word in message for word in service_words):
                raise ServiceAbort(str(exc)) from exc
            raise
        if result.get("state_truncated") is True:
            raise ServiceAbort("모델 입력 잘림 감지: 전체 context 비교 결과로 집계하지 않음")
        return result


def worker(output, spec, repeats):
    from backend.simulation.evaluation import selector_iterative_route_benchmark as bench
    env = spec["selector"]
    config = json.loads(CONFIG.read_text())
    model_dir = output / model_key(spec)
    for profile in PROFILES:
        directory = model_dir / profile
        expected = json.loads((output / "protocol.json").read_text())["source_sha256"]
        if source_hashes() != expected:
            raise RuntimeError("실험 중 소스 변경 감지: 새로운 코드로 같은 실험을 이어가지 않음")
        save(model_dir / "worker_status.json", {"status": "running", "profile": profile, "updated_at": now()})
        if env == "kev":
            from backend.app.services.route_selector.kev_selector import KevSelector
            selector = KevSelector(model=spec["name"], host="http://127.0.0.1:8021")
        elif env == "laya":
            from backend.app.services.route_selector.laya_selector import LayaSelector
            selector = LayaSelector(model=spec["name"], max_length=8192, require_cuda=True)
        else:
            resolved = bench.load_iterative_config(CONFIG, [spec["name"]], state_profile=profile)
            selector = bench._build_configured_ollama_selector(resolved["models"][0])
        result = bench.run_benchmark(
            directory, graph_path=ROOT / "routes/test_int.geojson", case_mode="all-pairs",
            repeats=repeats, warmups=1, max_steps=26, deadline=.15,
            candidate_scope="neighbors", context_mode="neighbor_context",
            state_profile=profile, selector=CheckedSelector(selector), progress=True,
        )
        if result["overall"]["trial_count"] != 84 * repeats:
            raise RuntimeError("평가 경로 수 불일치")
        if env == "ollama":
            host = config["ollama_defaults"]["host"]
            save(directory / "ollama_placement.json", http(host + "/api/ps"))
        save(model_dir / "worker_status.json", {"status": "running", "completed_profile": profile, "updated_at": now()})
    save(model_dir / "worker_status.json", {"status": "complete", "updated_at": now()})


def snapshot_jobs(output, jobs, repeats):
    rows = []
    for job in jobs:
        job = dict(job)
        directory = output / job["model_key"] / job["profile"]
        path = directory / "selector_iterative_route_trials.jsonl"
        job["completed_routes"] = line_count(path)
        job["total_routes"] = 84 * repeats
        summary = directory / "selector_iterative_route_summary.json"
        manifest = directory / "manifest.json"
        if manifest.exists() and job["status"] not in {"failed", "blocked"}:
            job["status"] = "running"
        if summary.exists() and manifest.exists() and json.loads(manifest.read_text())["status"] == "complete":
            value = json.loads(summary.read_text())
            job.update(status="complete", summary=value["overall"], device=value["device"])
        rows.append(job)
    return rows


def report(output, jobs, status):
    state = {"status": status, "updated_at": now(), "completed_conditions": sum(j["status"] == "complete" for j in jobs),
             "planned_conditions": len(jobs), "jobs": jobs}
    save(output / "progress.json", state)
    columns = ["model", "context", "status", "completed_routes", "arrival_count", "arrival_percent",
               "shortest_count", "shortest_percent", "decision_mean_ms", "route_mean_s", "note"]
    csvrows = []
    doc = ["# 4가지 context · 활성 모델 전체 비교", "", "갱신: " + state["updated_at"], "",
           "상태: " + status + ". 동일 정수 지도, 동일 지시문·후보 규칙. 모델별 4조건 × 84쌍 × 1회.", "",
           "① 기존 기본 ② 필수만 ③ 필수+후보 직선거리 ④ 기존 기본+방위·거리·접근량.",
           "다른 로봇 context·후보 필터는 추가하지 않는다. 모델별 정밀도·양자화·실행 장치는 별도로 기록한다.", "",
           "| 모델 | context | 상태·진행 | 도착 | 최단 일치 | 결정 ms | 경로 s |",
           "| --- | --- | --- | --- | --- | --- | --- |"]
    for job in jobs:
        value = job.get("summary")
        row = {"model": job["name"], "context": job["profile"], "status": job["status"],
               "completed_routes": job["completed_routes"], "note": job.get("reason", "")}
        if value:
            count = value["trial_count"]
            arrived, shortest = value["reached_target_count"], value["distance_match_count"]
            latency = value["step_wall_latency"]["mean_seconds"]
            row.update(arrival_count=arrived, arrival_percent=arrived/count*100,
                       shortest_count=shortest, shortest_percent=shortest/count*100,
                       decision_mean_ms=latency*1000 if latency is not None else None,
                       route_mean_s=value["all_trial_wall_latency"]["mean_seconds"])
            a, b = f"{arrived}/{count} ({arrived/count:.2%})", f"{shortest}/{count} ({shortest/count:.2%})"
            ms = f"{latency*1000:.1f}" if latency is not None else "—"
            seconds = f'{row["route_mean_s"]:.3f}'
        else:
            a = b = ms = seconds = "—"
        csvrows.append(row)
        doc.append(f'| {job["name"]} | {job["profile"]} | {job["status"]} {job["completed_routes"]}/{job["total_routes"]} | {a} | {b} | {ms} | {seconds} |')
    doc += ["", "## 결과 해석", "",
            "도착·최단 일치의 분모는 실패를 포함한 전체 경로 수다. 시간은 경로 생성 시간이며 주행 시간이 아니다.",
            "조건별 1회 측정으로 반복 변동·실행 순서 효과를 분리하지 않았다. 인프라 실패는 blocked/failed로 구분한다.",
            "실제 모델 전달 입력은 조건별 trials의 steps[].model_input에 있다. 기록은 selector 인코딩 전이며 단일 후보 자동 이동에는 모델 입력이 없다.",
            "모델별 4조건은 같은 프로세스·설정으로 순차 실행한다. 전체 경로는 앞선 선택에 따라 이후 상태가 달라질 수 있다.",
            "Ollama의 CPU/GPU 혼합 배치는 ollama_placement.json에서 확인한다. Laya의 입력 한도는 8192로 고정하고 입력 잘림이 감지되면 중단한다.", "",
            "## 미실행·오류", ""]
    for job in jobs:
        if job.get("reason"):
            item = f'- {job["name"]}: {job["reason"]}'
            if item not in doc:
                doc.append(item)
    (output / "report.md").write_text("\n".join(doc) + "\n", encoding="utf-8")
    with (output / "comparison.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(csvrows)
    wide = {}
    for job in jobs:
        path = output / job["model_key"] / job["profile"] / "selector_iterative_route_trials.jsonl"
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            try:
                trial = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = (trial["start_node"], trial["target_node"], trial["repeat"])
            row = wide.setdefault(key, dict(start=key[0], target=key[1], repeat=key[2]))
            prefix = job["model_key"] + "__" + job["profile"] + "__"
            times = [step["wall_seconds"] for step in trial["steps"] if step.get("model_called")]
            row.update({prefix+"arrived": trial["reached_target"], prefix+"shortest": trial["distance_match"],
                        prefix+"decision_ms": sum(times)/len(times)*1000 if times else None,
                        prefix+"route_s": trial["wall_seconds"], prefix+"path": json.dumps(trial["path"])})
    if wide:
        fields = ["start", "target", "repeat"] + sorted({k for row in wide.values() for k in row} - {"start","target","repeat"})
        with (output / "all84_side_by_side.csv").open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(wide[key] for key in sorted(wide))


def stop_owned_server(pid):
    if not pid:
        return
    cmd = Path(f"/proc/{pid}/cmdline")
    if not cmd.exists():
        return
    args = cmd.read_bytes().split(b"\0")
    if b"kev.serve" not in args or b"8021" not in args:
        raise RuntimeError("소유한 KEV 8021 서버인지 확인하지 못해 종료하지 않음")
    os.kill(pid, signal.SIGTERM)
    for _ in range(50):
        if not cmd.exists():
            return
        time.sleep(.2)
    raise RuntimeError("KEV 서버 종료를 확인할 수 없어 다음 GPU 작업을 시작하지 않음")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--worker", help="부모 실행기가 사용하는 단일 모델 JSON")
    parser.add_argument("--owned-kev-server-pid", type=int)
    args = parser.parse_args()
    output = args.output.resolve()
    os.chdir(ROOT)
    if args.worker:
        try:
            worker(output, json.loads(args.worker), 1)
        except BaseException as exc:
            spec = json.loads(args.worker)
            save(output/model_key(spec)/"worker_status.json", {
                "status": "failed", "error": f"{type(exc).__name__}: {exc}", "updated_at": now()})
            traceback.print_exc()
            return 1
        return 0
    if (output/"protocol.json").exists():
        raise FileExistsError("새 실험은 새 output 폴더를 사용하세요")
    config = json.loads(CONFIG.read_text())
    decision = [m for m in config["decision_models"] if m.get("enabled",True)]
    ollama = [dict(selector="ollama",name=m["name"]) for m in config["models"] if m.get("enabled",True)]
    specs = sorted(decision, key=lambda m: (m["selector"] != "kev", m["name"] != "kev-08b")) + ollama
    # 6GB에서 bf16 4B를 양자화/CPU로 바꿔 같은 조건인 것처럼 실행하지 않는다.
    blocked = "로컬 KEV-4B 캐시 없음. 4B bf16 전체 GPU 실행의 가중치만 약 8GB로 6GB VRAM을 초과; 같은 정밀도로 실행할 외부 GPU 필요."
    jobs = [dict(selector=s["selector"],name=s["name"],model_key=model_key(s),profile=p,
                 status="blocked" if s["name"]=="kev-4b" else "queued",
                 **({"reason":blocked} if s["name"]=="kev-4b" else {})) for s in specs for p in PROFILES]
    hardware = subprocess.check_output(["nvidia-smi","--query-gpu=name,memory.total","--format=csv,noheader"],text=True).strip()
    source = subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip()
    protocol = dict(created_at=now(), models=specs, profiles=list(PROFILES), profile_labels=list(LABELS),
                    case_mode="all-pairs", pairs=84, repeats=1, warmups=1,
                    graph="routes/test_int.geojson",
                    graph_sha256=hashlib.sha256((ROOT/"routes/test_int.geojson").read_bytes()).hexdigest(),
                    instruction_sha256=None, source_sha256=source_hashes(), source_commit=source,
                    hardware=hardware, jobs=jobs,
                    note="활성 모델만 포함. context 외 지도·지시문·후보 규칙 고정. 모델 내부 설정은 모델별 고정.")
    from backend.app.services.iterative_route_policy import NEXT_NODE_INSTRUCTIONS
    protocol["instruction_sha256"] = hashlib.sha256(NEXT_NODE_INSTRUCTIONS.encode()).hexdigest()
    save(output/"protocol.json", protocol)
    save(output/"ollama_inventory.json", http(config["ollama_defaults"]["host"]+"/api/tags"))
    if args.owned_kev_server_pid:
        save(output/"kev_runtime.json", http("http://127.0.0.1:8021/v1/models"))
    save(output/"parent.json", {"pid":os.getpid(),"owned_kev_server_pid":args.owned_kev_server_pid,"started_at":now()})
    jobs = snapshot_jobs(output,jobs,1)
    report(output,jobs,"running")
    try:
        for spec in specs:
            if spec["name"]=="kev-4b":
                continue
            if spec["selector"]!="kev" and args.owned_kev_server_pid:
                stop_owned_server(args.owned_kev_server_pid)
                args.owned_kev_server_pid = None
            for job in jobs:
                if job["model_key"]==model_key(spec):
                    job["status"]="running" if job["profile"] == PROFILES[0] else "queued"
            model_dir=output/model_key(spec)
            model_dir.mkdir(parents=True,exist_ok=True)
            env=os.environ.copy()
            env.update(USE_TF="0",HF_HUB_OFFLINE="1",TRANSFORMERS_OFFLINE="1",
                       LAYA_MAX_LENGTH="8192",LAYA_REQUIRE_CUDA="1",KEV_REQUIRE_CUDA="1",
                       KEV_HOST="http://127.0.0.1:8021",PYTHONUNBUFFERED="1")
            with (model_dir/"worker.log").open("w") as log:
                process=subprocess.Popen(
                    [sys.executable,"-u","-m","backend.simulation.evaluation.iterative_context_matrix",
                     "--output",str(output),"--worker",json.dumps(spec)],cwd=ROOT,env=env,
                    stdout=log,stderr=subprocess.STDOUT)
                while process.poll() is None:
                    jobs=snapshot_jobs(output,jobs,1)
                    report(output,jobs,"running")
                    time.sleep(5)
            status_path=model_dir/"worker_status.json"
            failure=json.loads(status_path.read_text()).get("error") if status_path.exists() else f"worker exit={process.returncode}"
            jobs=snapshot_jobs(output,jobs,1)
            for job in jobs:
                if job["model_key"]==model_key(spec) and job["status"]!="complete":
                    job.update(status="failed",reason=failure or f"worker exit={process.returncode}")
            report(output,jobs,"running")
            if spec["selector"]=="ollama":
                http(config["ollama_defaults"]["host"]+"/api/generate",{"model":spec["name"],"keep_alive":0},timeout=60)
        jobs=snapshot_jobs(output,jobs,1)
        final="complete" if all(j["status"]=="complete" for j in jobs) else "finished_with_unavailable_models"
        report(output,jobs,final)
    finally:
        if args.owned_kev_server_pid:
            stop_owned_server(args.owned_kev_server_pid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
