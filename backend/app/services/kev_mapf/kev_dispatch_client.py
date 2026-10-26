"""KEV Choice API를 호출하는 로봇 배차 공통 클라이언트."""

import json
import os

from ..route_selector.kev_selector import KevSelector


def request_choice(request, selector, *, timeout_seconds, logger, debug_prefix):
    """준비된 KEV 요청을 실행하고 `(응답, selector)`를 반환한다."""
    if selector is None:
        selector = KevSelector(timeout_seconds=timeout_seconds)

    debug_record = request["debug_record"]
    debug_enabled = os.getenv("KEV_DISPATCH_DEBUG") == "1"
    try:
        result = selector.select_choice(
            state=debug_record["state"],
            candidates=debug_record["choices"],
            instructions=debug_record["instructions"],
            question_id=debug_record["question_id"],
        )
    except Exception as exc:
        if debug_enabled:
            logger.info(
                "%s %s",
                debug_prefix,
                json.dumps(
                    {
                        **debug_record,
                        "response": None,
                        "error": {"type": type(exc).__name__, "message": str(exc)},
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
        raise

    if debug_enabled:
        logger.info(
            "%s %s",
            debug_prefix,
            json.dumps(
                {
                    **debug_record,
                    "response": {
                        "probabilities": result.get("probabilities"),
                        "confidence": result.get("confidence"),
                        "answer_confidence": result.get("answer_confidence"),
                        "choice": result.get("choice"),
                    },
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )
    return result, selector
