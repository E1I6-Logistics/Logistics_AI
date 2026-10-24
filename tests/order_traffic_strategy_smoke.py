"""시뮬 전용 관제에 실제 주문 API로 순차 공급하고 완료·점유를 관측한다.

일반 관제에는 실행하지 않는다. /api/mode.simulation_preview 확인 및 빈 주문표가
필수다. 시험 주문은 삭제하지 않고 결과 폴더도 덮어쓰지 않는다.
"""

import argparse
import hashlib
import json
import math
from pathlib import Path
from time import monotonic, sleep
from urllib.request import Request, urlopen

JHLEE_POLICY = "JHLEE_NATIVE_GATE_COMMON_ASTAR"


def order_schedule(name):
    schedule = [(0, dict(A=1, B=0, C=0, D=0)), (0, dict(A=0, B=0, C=1, D=0)),
                (2, dict(A=0, B=1, C=0, D=0)), (4, dict(A=0, B=0, C=0, D=1)),
                (6, dict(A=1, B=0, C=1, D=0)), (8, dict(A=0, B=1, C=0, D=1))]
    return schedule[:{"single": 1, "overlap": 2, "six": 6}[name]]


def load_schedule(path):
    """정해진 시각·수량을 비교 전에 검사해 두 방식에 동일하게 전달한다."""
    rows = json.loads(Path(path).read_text())["orders"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= 100:
        raise ValueError("시험 주문은 1~100건이어야 합니다.")
    schedule, previous = [], 0.0
    for row in rows:
        at, items = row["at_seconds"], row["items"]
        if type(at) not in (int, float) or not math.isfinite(at) or not previous <= at <= 1800:
            raise ValueError("주문 시각은 0~1800초의 오름차순이어야 합니다.")
        if not isinstance(items, dict) or set(items) != set("ABCD"):
            raise ValueError("주문 수량은 A·B·C·D를 모두 명시하세요.")
        if any(type(value) is not int or value < 0 for value in items.values()) or not 1 <= sum(items.values()) <= 5:
            raise ValueError("물품 수량 합계는 정수 1~5개여야 합니다.")
        schedule.append((at, items)); previous = at
    return schedule


def validate_mode(mode, operation_mode, policy, run_id=None):
    """기존 알고리즘 흐름과 새 AI 흐름을 서로 바꿔 실행하지 않게 확인한다."""
    assert mode.get("simulation_preview") and mode["mode"] == "simulation"
    assert mode["operation_mode"] == operation_mode
    traffic = mode["order_traffic"]
    if operation_mode == "algorithm":
        assert traffic["enabled"] is False and traffic["selection_policy"] is None
        assert traffic["route_source"] == "MODE"
    else:
        assert operation_mode == "ai"
        assert traffic["enabled"] and traffic["selection_policy"] == policy
        assert traffic["route_source"] == "EXISTING_SHORTEST_PATH"
    if run_id:
        assert mode.get("simulation_preview_run_id") == run_id


def order_payload(index, items, operation_mode):
    payload = dict(order_name=f"STRATEGY-{index+1:02d}", items=items,
                   total_quantity=sum(items.values()), operation_mode=operation_mode)
    if operation_mode == "ai":
        payload.update(selector="kev", model="kev-08b")
    elif operation_mode != "algorithm":
        raise ValueError("지원하지 않는 주문 모드입니다.")
    return payload


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="http://127.0.0.1:8000")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--policy", choices=("KEV", "MIN_EXPECTED_TIME", "KEV_STRATEGY_ORDER", JHLEE_POLICY), default="KEV_STRATEGY_ORDER")
    parser.add_argument("--schedule", choices=("single", "overlap", "six"), default="six")
    parser.add_argument("--schedule-file", type=Path, help="고정 주문표 JSON. 기존 짧은 시험 대신 사용")
    parser.add_argument("--run-id", help="비교 도구가 직접 시작한 서버 식별자")
    parser.add_argument("--operation-mode", choices=("ai", "algorithm"), default="ai")
    args = parser.parse_args()
    if not 10 <= args.timeout <= 3600:
        parser.error("시험 한도는 10~3600초여야 합니다.")
    schedule = load_schedule(args.schedule_file) if args.schedule_file else order_schedule(args.schedule)
    if schedule[-1][0] >= args.timeout:
        parser.error("마지막 주문 접수 시각보다 시험 한도가 길어야 합니다.")
    args.output.mkdir(parents=True, exist_ok=False)

    def request(path, payload=None):
        req = Request(args.host.rstrip("/") + path,
                      data=None if payload is None else json.dumps(payload).encode(),
                      headers={"Content-Type":"application/json"})
        with urlopen(req,timeout=10) as response:
            return json.load(response)

    mode=request("/api/mode");initial=request("/api/orders")
    validate_mode(mode, args.operation_mode, args.policy, args.run_id)
    assert not initial["orders"], "다른 주문이 있어 시험을 실행하지 않습니다."
    initial_robots=request("/api/robots")
    graph=request("/api/route/graph?raw=true")
    graph_sha256=hashlib.sha256(json.dumps(graph,sort_keys=True).encode()).hexdigest()
    edge_resources={str(f["properties"]["id"]):("edge",*sorted((str(f["properties"]["startid"]),str(f["properties"]["endid"]))))
                    for f in graph["features"] if "startid" in f.get("properties",{}) and "endid" in f["properties"]}
    report=dict(state="RUNNING",scope="SIMULATION_PREVIEW_HTTP_ORDERS",mode=mode,
                policy=args.policy if args.operation_mode == "ai" else None,
                initial_robots=initial_robots,graph_sha256=graph_sha256,
                schedule=[dict(at_seconds=t,items=items) for t,items in schedule],submitted=[],samples=0,
                capacity_violations=0,initial_orders=initial,errors=[])
    started=monotonic();index=0
    events=args.output/"observations.jsonl"
    try:
        with events.open("w") as output:
            while monotonic()-started<args.timeout:
                elapsed=monotonic()-started
                while index<len(schedule) and schedule[index][0]<=elapsed:
                    items=schedule[index][1]
                    request_seconds=monotonic()-started
                    response=request("/api/orders",order_payload(index, items, args.operation_mode))
                    report["submitted"].append(dict(at_seconds=monotonic()-started,request_seconds=request_seconds,
                                                    scheduled_seconds=schedule[index][0],order_id=response["order_id"]))
                    index+=1
                    print("주문 등록",index,items,flush=True)
                orders=request("/api/orders");robots=request("/api/robots")
                resources=[]
                for robot in robots:
                    if robot.get("occupied_node") is not None:resources.append(("node",str(robot["occupied_node"])))
                    elif robot.get("occupied_edge") is not None:
                        # 방향별 엣지 ID는 아래 지도 기반 물리 자원 키로 변환한다.
                        resources.append(edge_resources[str(robot["occupied_edge"])])
                if len(resources)!=len(set(resources)):
                    report["capacity_violations"]+=1;raise ValueError("노드·물리 엣지 중복 점유")
                report["samples"]+=1;report["orders"]=orders["orders"]
                output.write(json.dumps(dict(at_seconds=monotonic()-started,orders=orders,robots=robots),ensure_ascii=False)+"\n")
                output.flush()
                if any(o["status"]=="FAILED" or o.get("return_error") for o in orders["orders"]):
                    raise ValueError("주문 또는 복귀 실패가 발생했습니다.")
                if index==len(schedule) and all(o["status"]==o["phase"]=="COMPLETED" for o in orders["orders"]):
                    report["state"]="COMPLETED";break
                sleep(.2)
        if report["state"]!="COMPLETED":raise ValueError("연속 주문 시험 시간 한도 초과")
    except Exception as exc:
        report["state"]="FAILED";report["errors"].append(f"{type(exc).__name__}: {exc}")
    finally:
        report["elapsed_seconds"]=monotonic()-started
        for key, endpoint in (("final_orders", "/api/orders"), ("final_robots", "/api/robots")):
            try:
                report[key]=request(endpoint)
            except Exception as exc:
                report["errors"].append(f"최종 상태 조회 실패: {type(exc).__name__}: {exc}")
        (args.output/"report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n")
    print(json.dumps({k:v for k,v in report.items() if k not in ("orders","initial_orders","mode","schedule","submitted")},ensure_ascii=False),flush=True)
    return 0 if report["state"]=="COMPLETED" else 1


if __name__=="__main__":raise SystemExit(main())
