"""같은 새 전략 흐름에서 실제 KEV·고정 순서·평균 응답 대기만 비교한다.

기본은 준비만 수행한다. --run이면 별도 시뮬 서버에서 조건마다 동일한 12건을
실제 시간으로 한 번씩 실행한다. 현재 관제에는 조회만 하고 실패 시 중단한다.
"""

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import socket
from statistics import mean
import subprocess
import sys
from time import monotonic, sleep
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.simulation.order_traffic_ranking_control import ARMS
from tests.order_traffic_policy_comparison import SOURCE_PATHS, api, digest, load_lines, positions, summarize
from tests.order_traffic_strategy_smoke import load_schedule

LABELS = {"LIVE_KEV":"새 KEV", "FIXED_ORDER":"고정 순서", "FIXED_ORDER_DELAYED":"고정 순서＋평균 대기"}
REFERENCE = ROOT / "logs/order-traffic-load-comparisons/20261023-two-seeds-once/mixed_pickups-seed-137"
EXTRA_SOURCE = ("backend/simulation/order_traffic_ranking_control.py", "backend/simulation/order_traffic_ranking_preview.py",
                "tests/order_traffic_ranking_ablation.py", "backend/app/services/order_dispatcher.py")


def trace_summary(trace):
    actual = [row for row in trace if row.get("model_request_sent") is True]
    fixed = [row for row in trace if row.get("ranking_source") == "FIXED_CONTROL"]
    counts = lambda rows:dict(Counter(row["event"] for row in rows))
    return dict(model=counts(actual), fixed=counts(fixed),
        model_response_seconds=sum(row["elapsed_seconds"] for row in actual if row["event"] == "RESPONSE"),
        ranking_response_seconds=sum(row["elapsed_seconds"] for row in trace if row["event"] == "RESPONSE"))


def visible():
    host="http://127.0.0.1:8000"
    return {path:api(host,path) for path in ("/api/mode","/api/orders","/api/robots","/api/scenarios/status")}


def visible_signature(value):
    return {**value,"/api/robots":positions(value["/api/robots"])}


def render(report):
    lines=["# 전략 순서와 응답 대기 대조", "", report["state"], "",
           "같은 두 픽업대 방문 주문 12건, 후보 생성·검사·최종 선택은 새 전략 흐름으로 동일.", "",
           "| 조건 | 작업 완료 | 평균 주문 시간 | 홈 복귀까지 | 실제 모델 요청 | 고정 순서 제안 |",
           "| --- | ---: | ---: | ---: | ---: | ---: |"]
    show=lambda value:"미완료" if value is None else f"{value:.2f}초"
    for row in report["trials"]:
        s=row["summary"]
        lines.append(f"| {LABELS[row['arm']]} | {s['completed_orders']}/12 | {show(s['mean_order_seconds'])} | {show(s['drain_seconds'])} | {s['model_calls']} | {s['fixed_ranking_requests']} |")
    lines.extend(["",f"인공 대기: {report['fixed_delay_seconds']:.6f}초. 지난 동일 장면의 모델 응답 평균을 실행 전에 고정했다.",
                  "입력별 지연을 똑같이 재현한 것은 아니다. 조건당 한 번의 진단이며 통계적 우열·인과관계를 확정하지 않는다.",
                  "기존 이동 기록의 kev_calls는 여기서는 전략 제안기 호출 수다. 실제 모델 요청은 별도 메타데이터와 서버 카운터로 구분한다."])
    return "\n".join(lines)+"\n"


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run",action="store_true")
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--port",type=int,default=8034)
    args=parser.parse_args()
    if not 8020<=args.port<=8099:parser.error("별도 시험 포트는 8020~8099를 사용하세요.")
    output=args.output.resolve();output.mkdir(parents=True,exist_ok=False)
    load_schedule(REFERENCE/"schedule.json")
    reference_trace=load_lines(REFERENCE/"KEV_STRATEGY_ORDER/order-traffic/selection-inferences.jsonl")
    responses=[row for row in reference_trace if row["event"]=="RESPONSE"]
    delay=mean(row["elapsed_seconds"] for row in responses)
    if len(responses)!=15:raise ValueError("기준 장면의 모델 기록을 확인하세요.")
    (output/"schedule.json").write_bytes((REFERENCE/"schedule.json").read_bytes())
    hashes={name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in (*SOURCE_PATHS,*EXTRA_SOURCE)}
    for name in hashes:
        path=output/"source"/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes((ROOT/name).read_bytes())
    report=dict(state="PREPARED",arms=list(ARMS),trials=[],errors=[],fixed_delay_seconds=delay,
                reference=str(REFERENCE),reference_trace_sha256=hashlib.sha256((REFERENCE/"KEV_STRATEGY_ORDER/order-traffic/selection-inferences.jsonl").read_bytes()).hexdigest(),
                source_sha256=hashes,visible_server_commands_sent=False,real_robot_commands_sent=False,
                timeout_seconds=300,repeat_count_per_arm=1)

    def save():
        (output/"ablation.json").write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False)+"\n")
        (output/"summary.md").write_text(render(report))

    save()
    if not args.run:
        print("대조 시험 준비 완료",output,"대기",delay,flush=True);return 0
    from backend.app.services.route_selector.kev_selector import KevSelector
    selector=KevSelector(model="kev-08b",host="http://127.0.0.1:8012",timeout_seconds=5)
    report["model_runtime"]=selector._ensure_runtime()
    report["model_before"]=api("http://127.0.0.1:8012","/v1/models")["models"][0]["batches"]
    report["visible_before"]=visible()
    report["warmup"]=selector.select_choice({"purpose":"RANKING_ABLATION_COMMON_WARMUP"},
        {"READY":"Ready.","NOT_READY":"Not ready."},"Choose READY.")
    report["state"]="RUNNING";started=monotonic();save()
    for arm in ARMS:
        child=None;folder=output/arm;folder.mkdir();run_id=uuid4().hex
        try:
            if any(hashlib.sha256((ROOT/name).read_bytes()).hexdigest()!=sha for name,sha in hashes.items()):
                raise RuntimeError("시험 소스가 변경돼 중단합니다.")
            with socket.socket() as probe:
                probe.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);probe.bind(("127.0.0.1",args.port))
            env=dict(os.environ,PYTHONNOUSERSITE="1",FMS_ROBOT_MODE="simulation",FMS_ORDER_TRAFFIC_ENABLED="1",
                FMS_ORDER_ROUTE_SOURCE="EXISTING_SHORTEST_PATH",FMS_TRAFFIC_SELECTION_POLICY="KEV_STRATEGY_ORDER",
                FMS_PREVIEW_LOG_DIR=str(folder),FMS_PREVIEW_RUN_ID=run_id,KEV_HOST="http://127.0.0.1:8012",
                FMS_RANKING_ABLATION_ARM=arm,FMS_RANKING_ABLATION_DELAY_SECONDS=str(delay),
                FMS_TRAFFIC_REROUTE_LIMIT="2",FMS_TRAFFIC_SELECTION_SECONDS="10",FMS_TRAFFIC_WAIT_SECONDS="60",
                FMS_TRAFFIC_STRATEGY_CALL_LIMIT="8",FMS_SIMULATION_WORK_SECONDS="1.5")
            with (folder/"backend.log").open("wb") as log:
                child=subprocess.Popen([sys.executable,"-m","uvicorn","backend.simulation.order_traffic_ranking_preview:app",
                    "--host","127.0.0.1","--port",str(args.port)],cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT)
            (folder/"backend.pid").write_text(str(child.pid)+"\n")
            host=f"http://127.0.0.1:{args.port}"
            for _ in range(100):
                if child.poll() is not None:raise RuntimeError("대조 서버 시작 실패")
                try:
                    mode=api(host,"/api/mode")
                    if mode.get("simulation_preview_run_id")==run_id:break
                except OSError:pass
                sleep(.1)
            else:raise RuntimeError("대조 서버 시작 시간 초과")
            if mode["ranking_ablation"]["arm"]!=arm:raise RuntimeError("실행 조건 불일치")
            print("조건 시작",LABELS[arm],flush=True)
            with (folder/"watcher.log").open("wb") as log:
                watcher=subprocess.run([sys.executable,str(ROOT/"tests/order_traffic_strategy_smoke.py"),"--host",host,
                    "--policy","KEV_STRATEGY_ORDER","--run-id",run_id,"--schedule-file",str(output/"schedule.json"),
                    "--output",str(folder/"trial"),"--timeout","300"],cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT,timeout=320)
            if watcher.returncode:raise RuntimeError("주문·복귀 시험 실패. 원문을 보존하고 중단합니다.")
        except Exception as exc:
            report["errors"].append(f"{arm}: {type(exc).__name__}: {exc}")
        finally:
            if child is not None and child.poll() is None:
                child.terminate()
                try:child.wait(timeout=10)
                except subprocess.TimeoutExpired:child.kill();child.wait(timeout=5)
            if (folder/"trial/report.json").exists():
                raw=json.loads((folder/"trial/report.json").read_text());trace=load_lines(folder/"order-traffic/selection-inferences.jsonl")
                summary=summarize(raw,load_lines(folder/"trial/observations.jsonl"),load_lines(folder/"order-traffic/decisions.jsonl"),trace)
                counts=trace_summary(trace)
                identity_verified=all(row.get("ranking_ablation_arm")==arm
                    and row.get("model_request_sent")==(arm=="LIVE_KEV")
                    and row.get("ranking_source")==("MODEL" if arm=="LIVE_KEV" else "FIXED_CONTROL") for row in trace)
                transport_verified=all(row.get("exact_transport_request_saved")== (arm=="LIVE_KEV")
                    for row in trace if row["event"]=="RESPONSE")
                summary.update(ranking_requests=summary["model_calls"],ranking_responses=summary["model_responses"],
                    model_calls=counts["model"].get("REQUEST",0),model_responses=counts["model"].get("RESPONSE",0),
                    model_errors=counts["model"].get("ERROR",0),fixed_ranking_requests=counts["fixed"].get("REQUEST",0),
                    fixed_ranking_responses=counts["fixed"].get("RESPONSE",0),summed_model_response_seconds=counts["model_response_seconds"],
                    summed_ranking_response_seconds=counts["ranking_response_seconds"])
                report["trials"].append(dict(arm=arm,run_id=run_id,state=raw["state"],summary=summary,
                    ranking_identity_verified=identity_verified,transport_verified=transport_verified,
                    server_stopped=child is None or child.poll() is not None))
                if not identity_verified or not transport_verified:report["errors"].append(arm+": 제안기·실제 전송 원문 구분 실패")
                print("조건 종료",LABELS[arm],raw["state"],flush=True)
            save()
        if report["errors"]:break
    report["visible_after"]=visible();report["model_after"]=api("http://127.0.0.1:8012","/v1/models")["models"][0]["batches"]
    summaries=[row["summary"] for row in report["trials"]]
    report["checks"]=dict(all_three_completed=len(summaries)==3 and all(s["all_returns_completed"] and s["all_robots_home"] for s in summaries),
        same_schedule=len({s["schedule_sha256"] for s in summaries})==1,same_start=len({digest(s["initial_positions"]) for s in summaries})==1,
        same_map=len({s["map_sha256"] for s in summaries})==1,same_settings=len({digest(s["simulation_settings"]) for s in summaries})==1,
        registration_lateness_within_half_second=all(s["max_registration_lateness_seconds"]<=.5 for s in summaries),
        no_observed_capacity_duplicates=all(s["capacity_violations"]==0 for s in summaries),
        ranking_responses_complete=all(s["ranking_requests"]==s["ranking_responses"] for s in summaries),
        model_responses_complete=all(s["model_calls"]==s["model_responses"] and s["model_errors"]==0 for s in summaries),
        fixed_responses_complete=all(s["fixed_ranking_requests"]==s["fixed_ranking_responses"] for s in summaries),
        ranking_identity_and_transport=all(row["ranking_identity_verified"] and row["transport_verified"] for row in report["trials"]),
        actual_calls_only_in_live_arm=all((row["summary"]["model_calls"]>0 if row["arm"]=="LIVE_KEV" else row["summary"]["model_calls"]==0) for row in report["trials"]),
        server_counter_matches=report["model_after"]["requests"]-report["model_before"]["requests"]==1+sum(s["model_calls"] for s in summaries),
        visible_unchanged=visible_signature(report["visible_before"])==visible_signature(report["visible_after"]),
        all_own_servers_stopped=all(row["server_stopped"] for row in report["trials"]),
        sources_unchanged=all(hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==sha for name,sha in hashes.items()))
    report["state"]="COMPLETED_ONE_DIAGNOSTIC_PASS" if not report["errors"] and all(report["checks"].values()) else "STOPPED_REVIEW_REQUIRED"
    report["elapsed_seconds"]=monotonic()-started;save();print("대조 기록",output,report["state"],flush=True)
    return 0 if report["state"]=="COMPLETED_ONE_DIAGNOSTIC_PASS" else 1


if __name__=="__main__":raise SystemExit(main())
