"""일반 주문 교통조정과 기존 시험 코드가 함께 사용하는 값 식별 함수."""

import hashlib
import json
from typing import Any


def fingerprint(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
    ).hexdigest()
