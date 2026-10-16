#!/usr/bin/env python3
"""재시도 주문에서 직전 로봇을 KEV 후보에서 빼는지 시뮬레이션으로 확인한다.

FMS 백엔드(8000)는 띄우지 않고 KEV 서버(8011)만 켠 상태에서 실행한다.
백엔드의 10Hz 루프(order tick + mock 시뮬레이션 진행)를 이 스크립트가 직접 돌린다.

    source /opt/ros/jazzy/setup.bash
    ~/venv/robot/bin/python tools/retry_scenario.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path
from time import monotonic

PROJECT_DIR = Path(__file__).resolve().parents[1]
MARKER = "KEV_DISPATCH_DEBUG "
TICK_INTERVAL_S = 0.1  # backend/app/main.py의 run_mock_simulation과 같은 주기


# start_fms.sh처럼 .env를 읽되, 이미 export된 값은 덮어쓰지 않는다.
def load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


# 해당 포트에 이미 서버가 떠 있는지 확인한다.
def port_open(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", port)) == 0


# KEV 서버가 응답하는지 확인한다.
def kev_reachable(host: str) -> bool:
    try:
        with urllib.request.urlopen(f"{host.rstrip('/')}/v1/models", timeout=3) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError):
        return False


# order_manager 로그 중 KEV 배차 기록만 모은다.
class DispatchLogCollector(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.INFO)
        self.debug_records = []
        self.messages = []

    def emit(self, record):
        message = record.getMessage()
        if message.startswith(MARKER):
            self.debug_records.append(json.loads(message.removeprefix(MARKER)))
        elif "KEV dispatch" in message:
            self.messages.append(message)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--battery", default="robot1=30,robot2=78,robot3=92", help="SIM_BATTERY_OVERRIDE 값")
    parser.add_argument("--timeout", type=float, default=120.0, help="단계별 최대 대기 시간(초)")
    parser.add_argument("--allow-backend", action="store_true", help="8000 포트에 백엔드가 떠 있어도 실행")
    return parser.parse_args()


async def run(args: argparse.Namespace, collector: DispatchLogCollector) -> bool:
    from backend.app.services.map_service import load_map_metadata
    from backend.app.services.mock_data import mock_fms
    from backend.app.services.mode_service import mode_manager
    from backend.app.services.order_manager import order_manager
    from backend.app.services.route_graph import load_route_graph
    from backend.app.services.simulation_order_runner import simulation_order_runner

    load_map_metadata()
    load_route_graph()
    mock_fms.reset()
    mode_manager.set_mode("simulation", "ai")
    previous = monotonic()

    # 조건이 참이 될 때까지 백엔드와 같은 순서로 tick과 시뮬레이션 진행을 반복한다.
    async def run_until(condition, label):
        nonlocal previous
        started = monotonic()
        while True:
            await asyncio.sleep(TICK_INTERVAL_S)
            now = monotonic()
            await order_manager.tick()
            mock_fms.advance_simulation(now - previous, now)
            previous = now
            if condition():
                return
            if now - started > args.timeout:
                raise TimeoutError(f"{label}: {args.timeout:.0f}초 안에 끝나지 않았습니다.")

    def order():
        return order_manager.get_order(order_id)

    def robots_line():
        return ", ".join(
            f"{row['robot_id']}={row['status']}@{row['current_node']}(battery {row['battery']})"
            for row in mock_fms.robot_snapshots("simulation")
        )

    try:
        print(f"[0] 초기 상태: {robots_line()}")

        # 1. 첫 배정
        order_id = order_manager.create_order(
            items={"A": 1, "B": 0, "C": 0, "D": 0}, total_quantity=1, operation_mode="ai"
        )["order_id"]
        await run_until(lambda: order()["robot_id"] is not None, "첫 배정")
        first_robot = order()["robot_id"]
        first_debug = collector.debug_records[-1] if collector.debug_records else None
        print(f"[1] 첫 배정: order={order_id} robot={first_robot}")
        if first_debug:
            print(f"    후보={list(first_debug['choices'])} 확률={first_debug['response']['probabilities']}")

        # 2. 픽업대 작업 중에 실패시킨다. 주행 중(MOVING)이면 자동 복귀가 시작되지 않는다.
        await run_until(lambda: order()["phase"] == "WORKING_AT_PICKUP", "픽업 작업 도착")
        order_manager.fail(order_id, "retry_scenario: injected failure at pickup")
        failed = order()
        print(
            f"[2] fail() 후: status={failed['status']} robot={failed['robot_id']} "
            f"recovery={failed.get('recovery_status')} last_robot_id={failed.get('last_robot_id')}"
        )

        # 3. 실패 로봇이 station으로 복귀하고 세 대 모두 배차 가능해질 때까지 기다린다.
        def all_available():
            status = order_manager.dispatch_status("simulation")
            return bool(status) and all(row["dispatch_available"] for row in status.values())

        await run_until(
            lambda: order().get("recovery_status") == "RETURNED" and all_available(), "실패 로봇 복귀"
        )
        print(f"[3] 복귀 완료: {robots_line()}")

        # 4. 재시도
        debug_count = len(collector.debug_records)
        order_manager.retry_order(order_id)
        retried = order()
        print(f"[4] retry 후: status={retried['status']} last_robot_id={retried.get('last_robot_id')}")
        await run_until(lambda: order()["robot_id"] is not None, "재시도 배정")
        final_robot = order()["robot_id"]

        retry_debug = collector.debug_records[debug_count:]
        print("[5] 재시도 KEV_DISPATCH_DEBUG:")
        for record in retry_debug:
            print("    " + json.dumps(record, ensure_ascii=False))
        retry_record = retry_debug[-1] if retry_debug else {}
        print(
            f"    후보={list(retry_record.get('choices', {}))} "
            f"excluded_last_robot={retry_record.get('excluded_last_robot')} "
            f"확률={(retry_record.get('response') or {}).get('probabilities')} "
            f"선택={(retry_record.get('response') or {}).get('choice')}"
        )
        print(f"[6] 최종 배정 로봇: {final_robot}")
        for message in collector.messages:
            print(f"    log: {message}")
    finally:
        await simulation_order_runner.shutdown()

    checks = {
        "첫 배정 robot3": first_robot == "robot3",
        "재시도 후보 robot1·robot2": list(retry_record.get("choices", {})) == ["robot1", "robot2"],
        "excluded_last_robot=robot3": retry_record.get("excluded_last_robot") == "robot3",
        "선택 robot2": final_robot == "robot2",
        "fallback 없음": not any("falling back" in message for message in collector.messages),
    }
    print("\n판정:")
    for label, passed in checks.items():
        print(f"  {'PASS' if passed else 'FAIL'}  {label}")
    return all(checks.values())


def main() -> int:
    args = parse_args()
    load_env_file(PROJECT_DIR / ".env")
    # mock_fms가 import될 때 배터리를 읽으므로 import 전에 환경변수를 정한다.
    os.environ["SIM_BATTERY_OVERRIDE"] = args.battery
    os.environ["KEV_DISPATCH_DEBUG"] = "1"

    if port_open(8000) and not args.allow_backend:
        print("FMS 백엔드(8000)가 실행 중입니다. ./stop_fms.sh로 끈 뒤 KEV 서버만 켜고 실행하세요.")
        return 2
    kev_host = os.getenv("KEV_HOST", "http://127.0.0.1:8011")
    if not kev_reachable(kev_host):
        print(f"KEV 서버에 연결할 수 없습니다: {kev_host}/v1/models")
        return 2

    sys.path.insert(0, str(PROJECT_DIR))
    collector = DispatchLogCollector()
    app_logger = logging.getLogger("backend.app")
    app_logger.setLevel(logging.INFO)
    app_logger.addHandler(collector)

    passed = asyncio.run(run(args, collector))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
