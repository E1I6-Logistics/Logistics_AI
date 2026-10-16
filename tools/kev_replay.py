#!/usr/bin/env python3
"""Replay a saved KEV dispatch request directly against Kev, without starting FMS."""

from __future__ import annotations

import argparse
import copy
from itertools import permutations
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

MARKER = "KEV_DISPATCH_DEBUG "
API_MODEL = "kev-latest"
DEFAULT_LOG = Path(__file__).resolve().parents[1] / "logs" / "testD1_all.log"


def load_first_debug_record(path: Path) -> dict[str, Any]:
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        marker_index = line.find(MARKER)
        if marker_index < 0:
            continue
        try:
            record = json.loads(line[marker_index + len(MARKER) :])
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_number}: malformed KEV debug JSON: {exc}") from exc
        required = {"order_id", "question_id", "state", "choices", "instructions"}
        missing = required - record.keys()
        if missing:
            raise ValueError(f"{path}:{line_number}: missing fields: {', '.join(sorted(missing))}")
        if not isinstance(record["choices"], dict) or len(record["choices"]) < 2:
            raise ValueError("The saved request must contain at least two choice candidates.")
        return record
    raise ValueError(f"No {MARKER.strip()} record found in {path}")


def request_choice(
    host: str,
    state: dict[str, Any],
    choices: dict[str, str],
    instructions: str,
    question_id: str,
    timeout: float,
    api_key: str | None,
) -> dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {
        "model": API_MODEL,
        "state": state,
        "questions": {
            question_id: {
                "type": "choice",
                "instructions": instructions,
                "criteria": choices,
            }
        },
    }
    request = urllib.request.Request(
        f"{host.rstrip('/')}/v1/systemone",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Kev returned HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"Kev request failed: {exc}") from exc

    try:
        answer = body["answers"][question_id]
        if answer.get("type") not in (None, "choice"):
            raise ValueError(f"unexpected answer type: {answer.get('type')}")
        return answer
    except (KeyError, TypeError) as exc:
        raise RuntimeError(f"Unexpected Kev response shape: {body!r}") from exc


def battery_ranks(state: dict[str, Any]) -> dict[str, int]:
    profiles = state.get("robots")
    if not isinstance(profiles, list):
        raise ValueError("Saved state has no robots list.")
    batteries = {profile["robot_id"]: float(profile["battery"]) for profile in profiles}
    ranked = sorted(batteries, key=lambda robot_id: (-batteries[robot_id], robot_id))
    return {robot_id: rank for rank, robot_id in enumerate(ranked, 1)}


def profile_description(profile: dict[str, Any]) -> str:
    return (
        f"state={profile['state']}, "
        f"battery={profile['battery']}, "
        f"current_node={profile['current_node']}, "
        f"occupied_node={profile['occupied_node']}, "
        f"goal_node={profile['goal_node']}, "
        f"returning={profile['returning']}"
    )


def run_d3(
    source: dict[str, Any], host: str, timeout: float, api_key: str | None
) -> int:
    robot_ids = list(source["choices"])
    if len(robot_ids) != 3:
        print("D-3 expects exactly three robot candidates.", file=sys.stderr)
        return 2

    original_instructions = str(source["instructions"])
    strict_instructions = (
        "Among idle and non-returning robots, choose the one with the highest battery. "
        "Use other fields only to break exact ties. Return only one candidate ID."
    )
    simple_instructions = "Choose the robot with the highest battery."
    variants = {
        "V1": original_instructions,
        "V2": simple_instructions,
        "V3": strict_instructions,
    }
    assignments = list(permutations((30.0, 78.0, 92.0)))
    rows: dict[str, list[dict[str, Any]]] = {variant: [] for variant in variants}
    order_rows: dict[str, list[dict[str, Any]]] = {"R1a choices only": [], "R1b state only": []}
    failures = 0

    def call_case(label, state, choices, instructions, expected, bucket):
        nonlocal failures
        try:
            answer = request_choice(
                host,
                state,
                choices,
                instructions,
                str(source["question_id"]),
                timeout,
                api_key,
            )
            choice = answer.get("choice")
            probs = answer.get("probabilities", {})
            result = {
                "label": label,
                "battery": {robot_id: state_profile["battery"] for robot_id, state_profile in
                            ((p["robot_id"], p) for p in state["robots"])},
                "expected": expected,
                "probabilities": probs,
                "confidence": answer.get("confidence"),
                "choice": choice,
                "correct": choice == expected,
            }
        except Exception as exc:
            failures += 1
            result = {"label": label, "error": str(exc)}
        bucket.append(result)

    for assignment_index, values in enumerate(assignments, 1):
        battery_by_robot = dict(zip(robot_ids, values))
        expected = max(battery_by_robot, key=battery_by_robot.get)
        state = copy.deepcopy(source["state"])
        state.get("order", {}).pop("order_id", None)
        for profile in state["robots"]:
            profile["battery"] = battery_by_robot[profile["robot_id"]]
        profile_by_id = {profile["robot_id"]: profile for profile in state["robots"]}
        choices = {
            robot_id: profile_description(profile_by_id[robot_id])
            for robot_id in robot_ids
        }

        for variant, instructions in variants.items():
            call_case(
                f"{variant}-{assignment_index}",
                copy.deepcopy(state),
                dict(choices),
                instructions,
                expected,
                rows[variant],
            )

        # R1 controls use V3 and change only one of the two ordering dimensions.
        reversed_choices = dict(reversed(list(choices.items())))
        call_case(
            f"R1a-{assignment_index}",
            copy.deepcopy(state),
            reversed_choices,
            strict_instructions,
            expected,
            order_rows["R1a choices only"],
        )
        reversed_profiles_state = copy.deepcopy(state)
        reversed_profiles_state["robots"] = list(reversed(reversed_profiles_state["robots"]))
        call_case(
            f"R1b-{assignment_index}",
            reversed_profiles_state,
            dict(choices),
            strict_instructions,
            expected,
            order_rows["R1b state only"],
        )

    def print_rows(title: str, results: list[dict[str, Any]]) -> None:
        print(f"\n### {title}")
        print("Case | battery (robot1/2/3) | expected | probabilities (robot1/2/3) | confidence | choice | correct")
        print("---|---|---|---|---:|---|---|")
        for result in results:
            if "error" in result:
                print(f"{result['label']} | - | - | ERROR: {result['error']} | - | - | -")
                continue
            batteries = " / ".join(str(result["battery"].get(robot_id, "?")) for robot_id in robot_ids)
            probs = " / ".join(str(result["probabilities"].get(robot_id, "?")) for robot_id in robot_ids)
            print(
                f"{result['label']} | {batteries} | {result['expected']} | {probs} | "
                f"{result['confidence']} | {result['choice']} | {'yes' if result['correct'] else 'no'}"
            )

    print(f"Source order_id: {source['order_id']} (removed from every D-3 state)")
    print(f"Kev endpoint: {host.rstrip('/')}/v1/systemone")
    for variant, results in rows.items():
        print_rows(variant, results)
        successful = [result for result in results if "error" not in result]
        correct = sum(result["correct"] for result in successful)
        print(f"{variant} battery-argmax accuracy: {correct}/{len(successful)} ({len(successful)} permutations)")
    for label, results in order_rows.items():
        print_rows(label, results)
        successful = [result for result in results if "error" not in result]
        correct = sum(result["correct"] for result in successful)
        print(f"{label} battery-argmax accuracy: {correct}/{len(successful)} ({len(successful)} permutations)")
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG, help="backend log containing KEV_DISPATCH_DEBUG JSON")
    parser.add_argument("--host", default=os.getenv("KEV_HOST", "http://127.0.0.1:8011"))
    parser.add_argument("--timeout", type=float, default=float(os.getenv("KEV_TIMEOUT_SECONDS", "60")))
    parser.add_argument("--repeats", type=int, default=3, help="number of identical R0 requests (default: 3)")
    parser.add_argument("--d3", action="store_true", help="run six battery permutations and V1/V2/V3 plus isolated R1 order controls")
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be at least 1")

    try:
        source = load_first_debug_record(args.log)
        base_state = copy.deepcopy(source["state"])
        base_choices = dict(source["choices"])
        instructions = str(source["instructions"])
        question_id = str(source["question_id"])
        ranks = battery_ranks(base_state)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if args.d3:
        return run_d3(source, args.host, args.timeout, os.getenv("KEV_API_KEY"))

    jobs: list[tuple[str, dict[str, Any], dict[str, str], str]] = []
    for repeat in range(1, args.repeats + 1):
        jobs.append((f"R0-{repeat}", copy.deepcopy(base_state), dict(base_choices), instructions))

    reversed_state = copy.deepcopy(base_state)
    reversed_state["robots"] = list(reversed(reversed_state["robots"]))
    jobs.append(
        (
            "R1",
            reversed_state,
            dict(reversed(list(base_choices.items()))),
            instructions,
        )
    )

    no_order_id_state = copy.deepcopy(base_state)
    no_order_id_state.get("order", {}).pop("order_id", None)
    jobs.append(("R2", no_order_id_state, dict(base_choices), instructions))

    ranked_choices = {
        robot_id: f"{description}, battery_rank={ranks[robot_id]}"
        for robot_id, description in base_choices.items()
    }
    jobs.append(("R3", copy.deepcopy(base_state), ranked_choices, instructions))
    jobs.append(
        (
            "R4",
            copy.deepcopy(base_state),
            dict(base_choices),
            "Choose the robot with the highest battery.",
        )
    )

    print(f"Source order_id: {source['order_id']}")
    print(f"Kev endpoint: {args.host.rstrip('/')}/v1/systemone")
    print("Test | probabilities (robot1 / robot2 / robot3) | confidence | choice")
    print("---|---|---:|---|")
    failures = 0
    for label, state, choices, prompt in jobs:
        try:
            answer = request_choice(
                args.host,
                state,
                choices,
                prompt,
                question_id,
                args.timeout,
                os.getenv("KEV_API_KEY"),
            )
            probs = answer.get("probabilities", {})
            formatted_probs = " / ".join(str(probs.get(robot_id, "?")) for robot_id in base_choices)
            confidence = answer.get("confidence", "n/a")
            print(f"{label} | {formatted_probs} | {confidence} | {answer.get('choice', 'n/a')}")
        except Exception as exc:  # keep later variants running if one request fails
            failures += 1
            print(f"{label} | ERROR: {exc} | - | -")

    print("\nR1 reverses candidate and robot-profile order; R2 removes only state.order.order_id;")
    print("R3 adds descending battery_rank to candidate descriptions; R4 simplifies instructions.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
