"""관제 시나리오용 자동 주문 공급. 일반 주문 처리기를 그대로 사용한다."""

import asyncio
from collections import Counter
from copy import deepcopy
import json
import math
from pathlib import Path
import random
from time import monotonic


def validate_options(options):
    interval = options["interval_seconds"]
    if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not math.isfinite(interval) or interval < 1:
        raise ValueError("자동 주문 간격은 1초 이상의 유한한 숫자여야 합니다.")
    for key in ("max_pending", "initial_orders"):
        if type(options[key]) is not int or not 1 <= options[key] <= 20:
            raise ValueError(f"{key}는 1~20 정수여야 합니다.")
    if options["initial_orders"] > options["max_pending"]:
        raise ValueError("초기 주문 수는 대기열 상한을 넘을 수 없습니다.")
    if type(options["seed"]) is not int or options["model"] != "kev-08b":
        raise ValueError("정수 seed와 현재 시험 모델 kev-08b가 필요합니다.")


def random_items(generator):
    items = dict.fromkeys("ABCD", 0)
    for _ in range(generator.randint(1, 5)):
        items[generator.choice("ABCD")] += 1
    return items


def refresh_status_counts(status, orders):
    """전달된 상태 묶음의 소유 주문만 집계한다. 주문 자체는 바꾸지 않는다."""
    stream = status["order_stream"]
    owned = set(stream["order_ids"])
    rows = [row for row in orders if row["order_id"] in owned]
    counts = Counter(row["status"] for row in rows)
    stream.update(
        generated=len(owned), pending=counts["PENDING"], in_progress=counts["IN_PROGRESS"],
        completed=counts["COMPLETED"], failed=counts["FAILED"], cancelled=counts["CANCELLED"],
        returning=sum(row.get("phase") == "RETURNING" for row in rows),
    )
    return rows


def refresh_counts(manager, orders):
    return refresh_status_counts(manager._status, orders)


async def run_order_stream(manager, options):
    from backend.app.config import BASE_DIR
    from backend.app.services.mode_service import mode_manager
    from backend.app.services.order_manager import order_manager

    validate_options(options)
    generator = random.Random(options["seed"])
    started, next_supply = monotonic(), monotonic()
    first = True
    output = Path(BASE_DIR) / "logs" / "order-scenarios" / f"stream-{manager._status['run_id']}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    manager._status["order_stream"]["log_path"] = str(output)
    try:
        while manager._status["state"] == "running":
            if mode_manager.mode != "simulation" or mode_manager.operation_mode != "ai":
                raise ValueError("시뮬·AI 모드가 변경되어 주문 공급을 중지했습니다.")
            orders = order_manager.list_orders()
            rows = refresh_counts(manager, orders)
            # 접수한 주문이 사라지거나 실패했는데 새 주문을 계속 쌓지 않는다.
            if len(rows) != len(manager._status["order_stream"]["order_ids"]):
                raise ValueError("접수한 주문 상태를 확인할 수 없습니다.")
            if any(row["status"] == "FAILED" or row.get("return_error") for row in rows):
                raise ValueError("주문 또는 복귀 오류로 공급을 중지했습니다. 주문 목록에서 원인을 확인하세요.")
            now = monotonic()
            pending = sum(row["mode"] == "simulation" and row["status"] == "PENDING" for row in orders)
            if now >= next_supply and pending < options["max_pending"]:
                count = min(options["initial_orders"] if first else 1, options["max_pending"] - pending)
                for _ in range(count):
                    # 모드 확인부터 등록까지 await가 없어 실기체로 전환되어 접수될 틈이 없다.
                    if manager._status["state"] != "running" or mode_manager.mode != "simulation" or mode_manager.operation_mode != "ai":
                        raise ValueError("공급 상태 또는 운용 모드가 변경되었습니다.")
                    items = random_items(generator)
                    index = len(manager._status["order_stream"]["order_ids"]) + 1
                    row = order_manager.create_order(
                        items, sum(items.values()),
                        order_name=f"STREAM-{manager._status['run_id'][:6]}-{index:03d}",
                        selector="kev", model=options["model"],
                    )
                    manager._status["order_stream"]["order_ids"].append(row["order_id"])
                first = False
                next_supply = now + options["interval_seconds"]
                refresh_counts(manager, order_manager.list_orders())
            stream = manager._status["order_stream"]
            manager._status.update(
                elapsed=round(now-started, 2),
                message=(f"자동 공급 중 · 등록 {stream['generated']} · 대기 {stream['pending']} · "
                         f"처리 {stream['in_progress']} · 완료 {stream['completed']} · "
                         f"{options['interval_seconds']:g}초 간격 / 대기 상한 {options['max_pending']}건"),
            )
            output.write_text(json.dumps(manager.status(), ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
            await asyncio.sleep(.5)
    except asyncio.CancelledError:
        if manager._status["state"] == "running":
            manager._status.update(state="completed", message="서버 종료로 주문 공급 중지")
        raise
    except Exception as exc:
        manager._status.update(state="failed", message=str(exc))
    finally:
        refresh_counts(manager, order_manager.list_orders())
        output.write_text(json.dumps(deepcopy(manager._status), ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
