"""기존 주문 API로 가상 주문 시나리오를 실행한다. 기본 동작은 등록 계획 출력."""

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import time
from urllib.request import Request, urlopen
from uuid import uuid4


def load_scenario(name):
    scenarios = json.loads(Path(__file__).with_suffix(".json").read_text(encoding="utf-8"))["scenarios"]
    scenario = scenarios[name]
    if not scenario["orders"]:
        raise ValueError("등록할 주문이 없습니다.")
    previous = 0
    for spec in scenario["orders"]:
        at, items = spec["at_seconds"], spec["items"]
        if isinstance(at, bool) or not isinstance(at, (int, float)) or not math.isfinite(at) or at < previous:
            raise ValueError("등록 시각은 0 이상이며 순서대로 증가해야 합니다.")
        if set(items) != set("ABCD") or any(type(q) is not int or q < 0 for q in items.values()):
            raise ValueError("A/B/C/D 수량은 0 이상의 정수여야 합니다.")
        if not 1 <= sum(items.values()) <= 5:
            raise ValueError("한 주문의 수량 합계는 1~5여야 합니다.")
        previous = at
    return scenario


def ensure_mode(mode, operation):
    if mode["mode"] != "simulation" or mode["operation_mode"] != operation:
        raise ValueError(f"관제에서 시뮬 · {operation} 모드를 먼저 선택하세요.")


def ensure_available(mode, scenario, orders, operation):
    ensure_mode(mode, operation)
    if scenario["state"] in ("running", "stopping"):
        raise ValueError("지도 시나리오 종료 후 실행하세요.")
    for row in orders["orders"]:
        if (
            row["status"] in ("PENDING", "IN_PROGRESS")
            or row.get("phase") == "RETURNING"
            or row.get("recovery_status") in ("PENDING", "RETURNING", "BLOCKED", "STOPPED")
        ):
            raise ValueError("기존 주문·복귀·복구를 정리한 뒤 실행하세요. 기존 주문은 변경하지 않습니다.")


def order_payload(spec, operation, prefix, index, model="kev-08b"):
    payload = dict(
        order_name=f"{prefix}-{index:02d}", items=dict(spec["items"]),
        total_quantity=sum(spec["items"].values()), operation_mode=operation,
    )
    if operation == "ai":
        payload.update(selector="kev", model=model)
    return payload


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", choices=("bulk_six", "continuous_six"))
    parser.add_argument("--run", action="store_true", help="실제 가상 주문을 등록하고 상태 조회")
    parser.add_argument("--operation", choices=("algorithm", "ai"), default="algorithm")
    parser.add_argument("--model", default="kev-08b", help="AI 경로 모델. 일반 주문 교통조정은 서버 설정에 따름")
    parser.add_argument("--host", default="http://127.0.0.1:8000")
    parser.add_argument("--timeout", type=float, default=300)
    args = parser.parse_args()
    scenario = load_scenario(args.scenario)
    if not math.isfinite(args.timeout) or args.timeout <= scenario["orders"][-1]["at_seconds"]:
        parser.error("timeout은 마지막 주문 등록 시각보다 큰 유한한 초여야 합니다.")
    prefix = f"TEST-{args.scenario}-{uuid4().hex[:6]}"
    plan = [dict(at_seconds=spec["at_seconds"], payload=order_payload(
        spec, args.operation, prefix, index, args.model
    )) for index, spec in enumerate(scenario["orders"], 1)]
    print(scenario["description"], flush=True)
    print("검증 대상: 일반 주문 흐름. 서버에서 일반 주문 교통조정을 켜면 새 경로 조정도 적용합니다.", flush=True)
    if not args.run:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        print("등록하지 않았습니다. 실행하려면 --run을 추가하세요.")
        return 0

    def request(path, payload=None):
        data = None if payload is None else json.dumps(payload).encode()
        req = Request(args.host.rstrip("/") + path, data=data, headers={"Content-Type": "application/json"})
        with urlopen(req, timeout=10) as response:
            return json.load(response)

    output = Path(__file__).resolve().parents[1] / "logs" / "order-scenarios" / f"{prefix}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    report = dict(scenario=args.scenario, operation=args.operation, plan=plan, created=[], state="starting")
    try:
        ensure_available(request("/api/mode"), request("/api/scenarios/status"), request("/api/orders"), args.operation)
        report.update(state="running", started_at=datetime.now(timezone.utc).isoformat())
        started, next_order, previous = time.monotonic(), 0, None
        while True:
            ensure_mode(request("/api/mode"), args.operation)
            elapsed = time.monotonic() - started
            if elapsed >= args.timeout:
                raise ValueError("제한 시간 내 미완료입니다. 교착으로 단정하지 않고 화면과 로그에서 대기 원인을 확인하세요.")
            while next_order < len(plan) and elapsed >= plan[next_order]["at_seconds"]:
                row = request("/api/orders", plan[next_order]["payload"])
                report["created"].append(dict(order_id=row["order_id"], order_name=row["order_name"], submitted_seconds=round(time.monotonic()-started, 2)))
                next_order += 1
                output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
                print(f"등록 {next_order}/{len(plan)}: {row['order_name']}", flush=True)
            ids = {row["order_id"] for row in report["created"]}
            rows = [row for row in request("/api/orders")["orders"] if row["order_id"] in ids]
            if len(rows) != len(ids):
                raise ValueError("등록한 주문이 조회되지 않습니다. 서버 재시작 여부를 확인하세요.")
            report.update(orders=rows, elapsed=round(elapsed, 2))
            display = [(row["order_name"], row["robot_id"], row["status"], row["phase"]) for row in rows]
            if display != previous:
                print(json.dumps(display, ensure_ascii=False), flush=True)
                previous = display
            if any(row["status"] in ("FAILED", "CANCELLED") or row.get("return_error") for row in rows):
                raise ValueError("주문 실패·취소·복귀 오류가 있습니다. 결과 로그에서 원인을 확인하세요.")
            if next_order == len(plan) and all(row["status"] == row["phase"] == "COMPLETED" for row in rows):
                report["state"] = "completed"
                print("완료: 모든 주문 작업 및 후속 복귀 처리 종료", flush=True)
                return 0
            time.sleep(.5)
    except (Exception, KeyboardInterrupt) as exc:
        report.update(state="interrupted" if isinstance(exc, KeyboardInterrupt) else "failed", error=str(exc))
        print(f"중단: {exc or '상태 조회 중단'}", flush=True)
        print("이미 접수된 주문은 서버에서 계속 처리됩니다. 관제에서 확인하세요.", flush=True)
        return 1
    finally:
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
        print(f"결과: {output}", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
