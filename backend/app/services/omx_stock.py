"""OMX 관측 전용 팔레트 상태. 주문 재고·배차에는 영향을 주지 않는다."""
import math
import threading
import time

PALLETS = {"omx1": ("A", "B"), "omx2": ("C", "D"), "omx3": (), "omx4": ()}


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


class PalletObservation:
    def __init__(self, robot_id):
        self.robot_id = robot_id
        self._lock = threading.Lock()
        self.clear()

    def clear(self):
        with self._lock:
            self._sample = None

    def update(self, data):
        keys = PALLETS[self.robot_id]
        if not keys or type(data.get("schema_version")) is not int or data["schema_version"] != 1:
            return False
        stock = data.get("stock")
        if (data.get("robot_id") != self.robot_id or not isinstance(stock, dict)
                or set(stock) != set(keys)
                or any(not isinstance(v, str) or v not in ("PRESENT", "EMPTY", "UNKNOWN") for v in stock.values())):
            return False
        published, observed, ttl = data.get("at"), data.get("observed_at"), data.get("valid_for_s")
        now = time.time()
        if not finite(published) or not finite(ttl) or not 0 < ttl <= 750 or published > now + 5:
            return False
        known = any(v != "UNKNOWN" for v in stock.values())
        if known and (not finite(observed) or observed > published + .1):
            return False
        source_at = observed if known else published
        if now - source_at > ttl:
            return False
        remaining = min(ttl, ttl - max(0, now - source_at))
        details = data.get("items", {})
        details = details if isinstance(details, dict) else {}
        rows = {}
        for item in keys:
            detail = details.get(item, {})
            reason = detail.get("reason") if isinstance(detail, dict) else None
            rows[item] = {"state": stock[item], "reason": str(reason or data.get("reason") or "판정 근거 없음")[:300]}
        sample = {"palettes": rows, "observed_at": observed if finite(observed) else None,
                  "received_at": now, "expires": time.monotonic() + remaining, "source_at": source_at}
        with self._lock:
            # 지연 도착한 옛 결과로 최신 판정을 덮어쓰지 않는다.
            if self._sample is not None and source_at < self._sample["source_at"]:
                return False
            self._sample = sample
        return True

    def snapshot(self, *, connected, idle):
        with self._lock:
            sample = self._sample
        fresh = bool(connected and idle and sample and time.monotonic() < sample["expires"])
        reason = ("장치 연결 끊김" if not connected else "작업 중·검사 대기" if not idle
                  else "수신된 관측 없음" if sample is None else "관측 유효시간 초과")
        return {
            "supported": bool(PALLETS[self.robot_id]), "fresh": fresh,
            "observed_at": sample["observed_at"] if sample else None,
            "received_at": sample["received_at"] if sample else None,
            "exact_item_count": None,
            "palettes": {k: dict(sample["palettes"][k]) if fresh else {"state": "UNKNOWN", "reason": reason}
                         for k in PALLETS[self.robot_id]},
        }
