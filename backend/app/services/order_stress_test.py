"""주문 스트레스 시험의 난수·집계·실패 위치 기록. 배차 판단은 기존 FMS가 한다."""

from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import random
from time import monotonic
from zoneinfo import ZoneInfo

STRESS_NAME = "random_order_stress"
MAX_OPEN_ORDERS = 6  # 3대 작업 + 3개 대기로 지속 부하를 유지한다.


def unfinished_order(order):
    return order["status"] in {"PENDING", "IN_PROGRESS"} or (
        order["status"] == "COMPLETED" and order.get("phase") != "COMPLETED"
    )


def record_filename(started_at):
    started = datetime.fromisoformat(started_at).astimezone(ZoneInfo("Asia/Seoul"))
    return f"자동배차테스트[{started:%Y-%m-%d %H-%M-%S}].json"


class OrderStressTest:
    def __init__(self, run_id, directory: Path, *, seed):
        self.run_id, self.directory = run_id, directory
        started_at = datetime.now(timezone.utc).isoformat()
        filename = record_filename(started_at)
        if (directory / filename).exists():
            fraction = datetime.fromisoformat(started_at).strftime("%f")
            filename = filename.replace("].json", f".{fraction}].json")
            if (directory / filename).exists():
                raise FileExistsError(filename)
        self.random = random.Random(seed)
        self.pickup_patterns = []
        self.started = monotonic()
        self.orders, self.failures = {}, {}
        self.summary = dict(run_id=run_id, state="running", seed=seed,
            started_at=started_at, file_name=filename, elapsed=0.0,
            created=0, completed=0, failed=0, open_orders=0, failures=[],
            record_url=f"/api/scenarios/records/{run_id}")
        directory.mkdir(parents=True, exist_ok=True)
        self._save_summary()

    @property
    def record_path(self):
        return self.directory / self.summary["file_name"]

    def next_items(self):
        if not self.pickup_patterns:
            self.pickup_patterns = ["AB", "CD", "ABCD"]
            self.random.shuffle(self.pickup_patterns)
        pattern = self.pickup_patterns.pop()
        quantity = self.random.randint(2 if pattern == "ABCD" else 1, 5)
        selected = ([self.random.choice("AB"), self.random.choice("CD")]
                    if pattern == "ABCD" else [])
        selected.extend(self.random.choices(pattern, k=quantity-len(selected)))
        counts = Counter(selected)
        return {item: counts[item] for item in "ABCD"}

    def created(self, order):
        self.orders[order["order_id"]] = {key: order.get(key) for key in ("status", "phase")}

    def sample(self, orders, robots):
        if self.summary["state"] != "running":
            return dict(self.summary), []
        for order in orders:
            oid = order["order_id"]
            if oid not in self.orders:
                continue
            self.orders[oid] = {key: order.get(key) for key in ("status", "phase")}
            if order["status"] == "FAILED" and oid not in self.failures:
                robot = order.get("failure_robot_state") or robots.get(order.get("robot_id"), {})
                self.failures[oid] = dict(order_id=oid, robot_id=order.get("robot_id"),
                    phase=order.get("failed_phase"), reason=order.get("error"),
                    node=robot.get("occupied_node"), edge=robot.get("occupied_edge"),
                    destination=robot.get("goal_node"), time=order.get("failed_at"))
        self.summary.update(elapsed=round(monotonic()-self.started, 2), created=len(self.orders),
            completed=sum(o["status"] == "COMPLETED" and not unfinished_order(o) for o in self.orders.values()),
            failed=len(self.failures), open_orders=sum(unfinished_order(o) for o in self.orders.values()),
            failures=list(self.failures.values()))
        self._save_summary()
        rows = [dict(robot_id=rid, status=r["status"], current_node=r.get("current_node"), goal=r.get("goal_node"))
                for rid, r in robots.items()]
        return dict(self.summary), rows

    def _save_summary(self):
        temporary = self.record_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.summary, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
        temporary.replace(self.record_path)

    def finish(self, state, message):
        self.summary.update(state=state, message=message,
            ended_at=datetime.now(timezone.utc).isoformat(), elapsed=round(monotonic()-self.started, 2))
        self._save_summary()
