"""같은 주문표를 별도 시뮬 서버 세 개에 차례로 공급하는 전체 흐름 비교.

화면의 8000 서버·일반 관제·실기체에는 명령하지 않는다. 실행은 --run 명시가
필요하며 각 서버는 이 도구가 시작한 자식 프로세스만 종료한다.
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
from urllib.request import urlopen
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
POLICIES = ("MIN_EXPECTED_TIME", "KEV", "KEV_STRATEGY_ORDER")
JHLEE_POLICY = "JHLEE_NATIVE_GATE_COMMON_ASTAR"
LABELS = {"MIN_EXPECTED_TIME":"코드 선택", "KEV":"기존 KEV 선택", "KEV_STRATEGY_ORDER":"새 KEV 전략 순서", JHLEE_POLICY:"jhlee 원본 주문 게이트·공통 A*"}
SOURCE_PATHS = (
    "backend/app/services/mapf/__init__.py",
    "backend/app/services/mapf/common.py",
    "backend/app/services/mapf/order_traffic_service.py",
    "backend/app/services/mapf/order_traffic_strategy.py",
    "backend/app/services/mapf/traffic_route_provider.py",
    "tests/order_traffic_policy_comparison.py", "tests/order_traffic_strategy_smoke.py",
    "backend/simulation/order_traffic_preview.py", "backend/app/services/order_traffic_service.py",
    "backend/app/services/order_traffic_strategy.py", "backend/app/services/order_route_dispatcher.py",
    "backend/app/services/order_manager.py", "backend/app/services/simulation_order_runner.py",
    "backend/app/services/traffic_manager.py", "backend/app/services/reservation.py",
    "backend/app/services/route_service.py", "backend/app/services/traffic_route_provider.py",
    "backend/app/services/route_selector/kev_selector.py", "routes/test.geojson",
    "backend/simulation/order_traffic_jhlee.py", "backend/simulation/jhlee_order_preview.py",
    "backend/app/services/scenario_service.py", "backend/simulation/order_stream_scenario.py",
    "backend/app/services/mock_data.py", "backend/app/services/pickup_queue.py",
    "backend/app/services/workstation_manager.py",
)


def read_json(path):
    return json.loads(path.read_text())


def api(host, path):
    with urlopen(host.rstrip("/")+path,timeout=5) as response:
        return json.load(response)


def positions(rows):
    return [{k:r.get(k) for k in ("robot_id","occupied_node","occupied_edge","x","y","yaw","battery","status")}
            for r in sorted(rows,key=lambda r:r["robot_id"])]


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def summarize(report, observations, decisions, traces):
    submissions={x["order_id"]:x for x in report["submitted"]}
    dispatched,completed={},{}
    previous_time,previous_robots=0.0,[]
    waiting=Counter(); longest_wait=Counter(); wait_streak=Counter()
    max_pending=max_active=0
    for sample in observations:
        now=sample["at_seconds"]
        for robot in previous_robots:
            rid=robot["robot_id"]
            if robot["status"]=="WAITING":
                duration=max(0,now-previous_time)
                waiting[rid]+=duration;wait_streak[rid]+=duration
                longest_wait[rid]=max(longest_wait[rid],wait_streak[rid])
            else:wait_streak[rid]=0
        counts=Counter(order["status"] for order in sample["orders"]["orders"])
        max_pending=max(max_pending,counts["PENDING"]);max_active=max(max_active,counts["IN_PROGRESS"])
        for order in sample["orders"]["orders"]:
            oid=order["order_id"]
            if order.get("robot_id") is not None:dispatched.setdefault(oid,now)
            if order["status"]=="COMPLETED":completed.setdefault(oid,now)
        previous_time,previous_robots=now,sample["robots"]
    orders=report.get("orders",[])
    all_work=(len(orders)==len(report["schedule"])==len(submissions)
              and all(o["status"]=="COMPLETED" and o["order_id"] in completed for o in orders))
    all_dispatched=len(dispatched)==len(report["schedule"])==len(submissions)
    latencies=[completed[oid]-submissions[oid]["at_seconds"] for oid in completed if oid in submissions]
    horizon=max(completed.values()) if all_work else None
    requests=[x for x in traces if x["event"]=="REQUEST"]
    responses=[x for x in traces if x["event"]=="RESPONSE"]
    final_robots=report.get("final_robots")
    starts={r["robot_id"]:r.get("occupied_node") for r in report["initial_robots"]}
    all_home=(all(r.get("status")=="IDLE" and r.get("occupied_node")==starts.get(r["robot_id"])
                  and r.get("occupied_edge") is None and r.get("goal_node") is None for r in final_robots)
              and len(final_robots)==len(starts)) if final_robots is not None else None
    return dict(all_work_completed=all_work,all_returns_completed=report["state"]=="COMPLETED",
        expected_orders=len(report["schedule"]), expected_items=sum(sum(x.get("items",{}).values()) for x in report["schedule"]),
        completed_orders=sum(o["status"]=="COMPLETED" for o in orders),
        failed_orders=sum(o["status"]=="FAILED" or bool(o.get("return_error")) for o in orders),
        work_completion_seconds=horizon,drain_seconds=report["elapsed_seconds"] if report["state"]=="COMPLETED" else None,
        mean_order_seconds=mean(latencies) if all_work else None,max_order_seconds=max(latencies) if all_work else None,
        mean_dispatch_wait_seconds=mean(dispatched[oid]-submissions[oid]["at_seconds"] for oid in dispatched) if all_dispatched else None,
        max_dispatch_wait_seconds=max((dispatched[oid]-submissions[oid]["at_seconds"] for oid in dispatched),default=0) if all_dispatched else None,
        batch_items_per_minute=sum(o["total_quantity"] for o in orders)/horizon*60 if horizon else None,
        sampled_robot_waiting_seconds=sum(waiting.values()),robot_waiting_seconds=dict(waiting),
        sampled_longest_robot_waiting_seconds=dict(longest_wait),max_pending=max_pending,max_in_progress=max_active,
        model_calls=len(requests),model_responses=len(responses),model_errors=sum(x["event"]=="ERROR" for x in traces),
        summed_model_response_seconds=sum(x.get("elapsed_seconds",0) for x in responses),
        applied_routes=len(decisions),selected_by=dict(Counter(x["selected_by"] for x in decisions)),
        capacity_samples=report["samples"],capacity_violations=report["capacity_violations"],
        all_robots_home=all_home,
        max_registration_lateness_seconds=max((max(0,x["request_seconds"]-x["scheduled_seconds"]) for x in report["submitted"]),default=0),
        schedule_sha256=digest(report["schedule"]),initial_positions=positions(report["initial_robots"]),
        map_sha256=report["graph_sha256"],simulation_settings=report["mode"].get("simulation_settings"))


def load_lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def remaining_policies(report):
    if any(row.get("state")!="COMPLETED" or not row["summary"].get("all_returns_completed") for row in report["trials"]):
        raise ValueError("실패한 정책 시험은 자동 재실행하지 않습니다. 기록을 먼저 검토하세요.")
    done={row["policy"] for row in report["trials"]}
    return tuple(policy for policy in report.get("policy_order", POLICIES) if policy not in done)


def render(report):
    lines=["# 일반 주문 방식 비교", "",report["state"],"",
           "같은 주문표를 각 방식의 새 시뮬 상태에 한 번씩 공급. 실제 판단·주행을 포함하며 실기체 명령 없음.","",
           "| 방식 | 작업 완료 | 전체 작업 끝 | 평균 주문 시간 | 홈 복귀까지 | 모델 호출 | 로봇 대기 합계 |",
           "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    show=lambda value:"미완료" if value is None else f"{value:.2f}초"
    for row in report["trials"]:
        s=row["summary"]
        lines.append(f"| {LABELS[row['policy']]} | {s['completed_orders']}/{s.get('expected_orders',6)} | {show(s['work_completion_seconds'])} | {show(s['mean_order_seconds'])} | {show(s['drain_seconds'])} | {s['model_calls']} | {show(s['sampled_robot_waiting_seconds'])} |")
    lines.extend(["","시간은 약 0.2초 주기의 관측값. 한 번씩의 전체 흐름 비교이며 통계적 우열·지속 처리량을 입증하지 않는다.",
                  "코드/기존 KEV/새 전략의 판단 책임과 호출 조건이 달라 AI 단독 효과 비교가 아니다."])
    return "\n".join(lines)+"\n"


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run",action="store_true")
    parser.add_argument("--resume",action="store_true",help="완료 시험은 유지하고 도구 오류로 시작 못 한 정책만 실행")
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--port",type=int,default=8030)
    parser.add_argument("--model-host",default="http://127.0.0.1:8012")
    parser.add_argument("--policies", nargs="+", choices=tuple(LABELS))
    parser.add_argument("--schedule", choices=("single", "overlap", "six"), default="six")
    parser.add_argument("--schedule-file", type=Path)
    parser.add_argument("--trial-timeout", type=float, default=180)
    args=parser.parse_args()
    if not 10 <= args.trial_timeout <= 3600:parser.error("시험 한도는 10~3600초여야 합니다.")
    schedule_rows=None
    if args.schedule_file:
        from tests.order_traffic_strategy_smoke import load_schedule
        schedule_rows=[dict(at_seconds=t,items=items) for t,items in load_schedule(args.schedule_file)]
        if schedule_rows[-1]["at_seconds"] >= args.trial_timeout:parser.error("시험 한도가 마지막 주문보다 짧습니다.")
    if args.policies and len(args.policies)!=len(set(args.policies)):parser.error("같은 방식을 반복 지정하지 마세요.")
    if not 8020<=args.port<=8099:parser.error("비교 서버 포트는 8020~8099를 사용하세요.")
    args.output=args.output.resolve()
    hashes={p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in SOURCE_PATHS}
    revision=0
    if args.resume:
        if not args.run:parser.error("이어서 실행은 --run도 지정하세요.")
        report=read_json(args.output/"comparison.json")
        if args.policies and args.policies!=report["policy_order"]:parser.error("이어서 실행할 판단기가 다릅니다.")
        if args.schedule!=report.get("schedule_name","six"):parser.error("이어서 실행할 주문표가 다릅니다.")
        if schedule_rows!=report.get("custom_schedule"):parser.error("고정 주문표 내용이 다릅니다.")
        if args.trial_timeout!=report.get("trial_timeout_seconds",180):parser.error("시험 한도가 다릅니다.")
        policies=remaining_policies(report)
        changed={p for p,h in hashes.items() if report["source_sha256"].get(p)!=h}
        if changed-{"tests/order_traffic_policy_comparison.py"}:
            parser.error("주문·정책·지도 소스가 달라 이어서 실행하지 않습니다: "+str(sorted(changed)))
        revision=len(report.get("tool_resumes",[]))+1
        history=args.output/"history";history.mkdir(exist_ok=True)
        (history/f"before-resume-{revision}.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n")
        report.setdefault("tool_resumes",[]).append(dict(revision=revision,previous_errors=report["errors"],
            remaining_policies=list(policies),changed_paths=sorted(changed),source_sha256=hashes))
        for relative in changed:
            p=args.output/"source_revisions"/str(revision)/relative;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes((ROOT/relative).read_bytes())
        report["errors"]=[]
    else:
        args.output.mkdir(parents=True,exist_ok=False)
        policies=tuple(args.policies or POLICIES)
        for relative in hashes:
            p=args.output/"source"/relative;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes((ROOT/relative).read_bytes())
        report=dict(state="PREPARED",policy_order=list(policies),schedule_name=args.schedule,source_sha256=hashes,trials=[],
                    custom_schedule=schedule_rows,trial_timeout_seconds=args.trial_timeout,
                    visible_server_commands_sent=False,real_robot_commands_sent=False,errors=[],model_runtime=None)
    if schedule_rows is not None:
        (args.output/"schedule.json").write_text(json.dumps(dict(orders=schedule_rows),ensure_ascii=False,indent=2)+"\n")
    def save():
        (args.output/"comparison.json").write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False)+"\n")
        (args.output/"summary.md").write_text(render(report))
    save()
    if not args.run:
        print("실행 준비만 완료:",args.output,flush=True);return 0
    from backend.app.services.route_selector.kev_selector import KevSelector
    selector=KevSelector(model="kev-08b",host=args.model_host,timeout_seconds=5)
    runtime=selector._ensure_runtime()
    if args.resume:
        if any(runtime[k]!=report["model_runtime"][k] for k in ("run","base","device","backend","dtype","temperature")):
            parser.error("모델 설정이 달라 이어서 비교하지 않습니다.")
    else:
        report["model_runtime"]=runtime
        report["model_counters_before"]=api(args.model_host,"/v1/models")["models"][0]["batches"]
        report["warmup"]=selector.select_choice({"purpose":"COMMON_WARMUP_NOT_ORDER_TEST"},
                                {"READY":"Ready.","NOT_READY":"Not ready."},"Choose READY.")
        try:report["visible_server_before"]={p:api("http://127.0.0.1:8000",p) for p in ("/api/mode","/api/orders","/api/scenarios/status")}
        except OSError:report["visible_server_before"]=None
    report["state"]="RUNNING";save()
    host=f"http://127.0.0.1:{args.port}"
    for policy in policies:
        if any(hashlib.sha256((ROOT/p).read_bytes()).hexdigest()!=h for p,h in hashes.items()):
            report["errors"].append("시험 중 소스 변경으로 남은 비교를 중단합니다.");break
        folder=args.output/policy
        if folder.exists():folder=args.output/f"{policy}-resume-{revision}"
        folder.mkdir()
        run_id=uuid4().hex;child=None
        try:
            with socket.socket() as probe:
                probe.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
                probe.bind(("127.0.0.1",args.port))
            env=os.environ.copy();env.update(PYTHONNOUSERSITE="1",FMS_ROBOT_MODE="simulation",
                FMS_ORDER_TRAFFIC_ENABLED="1",FMS_ORDER_ROUTE_SOURCE="EXISTING_SHORTEST_PATH",
                FMS_TRAFFIC_SELECTION_POLICY="KEV" if policy==JHLEE_POLICY else policy,FMS_PREVIEW_LOG_DIR=str(folder),FMS_PREVIEW_RUN_ID=run_id,
                FMS_TRAFFIC_REROUTE_LIMIT="2",FMS_TRAFFIC_SELECTION_SECONDS="10",FMS_TRAFFIC_WAIT_SECONDS="60",
                FMS_TRAFFIC_STRATEGY_CALL_LIMIT="8",FMS_SIMULATION_WORK_SECONDS="1.5",KEV_HOST=args.model_host,
                FMS_SIM_MAPF_MAX_WAIT="60",FMS_MAPF_MAX_REROUTES="2",FMS_MAPF_PRIORITY_MODE="score")
            with (folder/"backend.log").open("wb") as log:
                entrypoint="backend.simulation.jhlee_order_preview:app" if policy==JHLEE_POLICY else "backend.simulation.order_traffic_preview:app"
                child=subprocess.Popen([sys.executable,"-m","uvicorn",entrypoint,
                                        "--host","127.0.0.1","--port",str(args.port)],cwd=ROOT,env=env,
                                        stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
            (folder/"backend.pid").write_text(str(child.pid)+"\n")
            ready=False
            for _ in range(80):
                if child.poll() is not None:raise RuntimeError("시뮬 비교 서버 시작 실패")
                try:
                    mode=api(host,"/api/mode")
                    if mode.get("simulation_preview_run_id")==run_id:ready=True;break
                except OSError:pass
                sleep(.1)
            if not ready:raise RuntimeError("시뮬 비교 서버 준비 시간 초과")
            print("비교 시작",LABELS[policy],flush=True)
            with (folder/"watcher.log").open("wb") as watcher_log:
                command=[sys.executable,str(ROOT/"tests/order_traffic_strategy_smoke.py"),
                    "--host",host,"--policy",policy,"--run-id",run_id,"--schedule",args.schedule,"--output",str(folder/"trial"),"--timeout",str(args.trial_timeout)]
                if schedule_rows is not None:command.extend(["--schedule-file",str(args.output/"schedule.json")])
                watcher=subprocess.run(command,cwd=ROOT,env=env,stdout=watcher_log,stderr=subprocess.STDOUT,timeout=args.trial_timeout+20)
            if not (folder/"trial/report.json").exists():
                raise RuntimeError(f"관측 결과 없음: watcher.log 확인 (종료 {watcher.returncode})")
        except Exception as exc:
            report["errors"].append(f"{policy}: {type(exc).__name__}: {exc}")
        finally:
            if child is not None and child.poll() is None:
                child.terminate()
                try:child.wait(timeout=10)
                except subprocess.TimeoutExpired:child.kill();child.wait(timeout=5)
            if (folder/"trial/report.json").exists():
                trial=read_json(folder/"trial/report.json")
                row=next((x for x in report["trials"] if x["policy"]==policy),None)
                if row is None:
                    row=dict(policy=policy,run_id=run_id,folder=folder.name,source_revision=revision);report["trials"].append(row)
                row.update(state=trial["state"],server_stopped=True,
                    summary=summarize(trial,load_lines(folder/"trial/observations.jsonl"),
                                      load_lines(folder/"order-traffic/decisions.jsonl"),load_lines(folder/"order-traffic/selection-inferences.jsonl")))
                print("비교 종료",LABELS[policy],row["state"],row["summary"]["model_calls"],"회 호출",flush=True)
            save()
        if report["errors"] or any(not x["summary"]["all_returns_completed"] for x in report["trials"]):break
    report["model_counters_after"]=api(args.model_host,"/v1/models")["models"][0]["batches"]
    try:report["visible_server_after"]={p:api("http://127.0.0.1:8000",p) for p in ("/api/mode","/api/orders","/api/scenarios/status")}
    except OSError:report["visible_server_after"]=None
    summaries=[x["summary"] for x in report["trials"]]
    report["checks"]=dict(
        all_planned_trials_completed=len(summaries)==len(report["policy_order"]) and all(x["all_returns_completed"] for x in summaries),
        same_schedule=len({x["schedule_sha256"] for x in summaries})==1,
        same_start=len({digest(x["initial_positions"]) for x in summaries})==1,
        same_map=len({x["map_sha256"] for x in summaries})==1,
        same_settings=len({digest(x["simulation_settings"]) for x in summaries})==1,
        registration_delay_within_half_second=all(x["max_registration_lateness_seconds"]<=.5 for x in summaries),
        no_capacity_violations=all(x["capacity_violations"]==0 for x in summaries),
        final_robots_home=all(x.get("all_robots_home") is True for x in summaries),
        model_responses_complete=all(x["model_calls"]==x["model_responses"] and x["model_errors"]==0 for x in summaries),
        model_counter_matches=report["model_counters_after"]["requests"]-report["model_counters_before"]["requests"]==1+sum(x["model_calls"] for x in summaries),
        visible_server_state_unchanged=report["visible_server_before"]==report["visible_server_after"],
        source_unchanged=all(hashlib.sha256((ROOT/p).read_bytes()).hexdigest()==h for p,h in hashes.items()),
        all_own_servers_stopped=all(x.get("server_stopped") for x in report["trials"]))
    report["state"]="COMPLETED_ONE_PASS_NOT_STATISTICAL_EVIDENCE" if all(report["checks"].values()) and not report["errors"] else "COMPLETED_WITH_ISSUES_REVIEW_REQUIRED"
    save();print("비교 기록:",args.output,report["state"],flush=True)
    return 0 if report["state"]=="COMPLETED_ONE_PASS_NOT_STATISTICAL_EVIDENCE" else 1


if __name__=="__main__":raise SystemExit(main())
