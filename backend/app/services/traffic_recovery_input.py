"""같은 미검사 후보에 가상 노드 점유 관계를 설명한다. 후보를 제외·선택하지 않는다."""

from copy import deepcopy
import json


INPUT_STYLES = ("original", "occupancy_facts", "compact")


def path_occupancy_facts(state, criteria):
    occupants = {r["current_node"]: r["robot_id"] for r in state["robots"]}
    facts = {}
    for key, text in criteria.items():
        move = json.loads(text)
        facts[key] = [dict(node=node, robot_id=occupants[node]) for node in move["path"][1:]
                      if node in occupants and occupants[node] != move["robot_id"]]
    return facts


def format_guidance_state(state, criteria, style="original"):
    if style not in INPUT_STYLES:
        raise ValueError("알 수 없는 KEV 탐색 입력 표현입니다.")
    if style == "original":
        return deepcopy(state)
    facts = path_occupancy_facts(state, criteria)
    if style == "occupancy_facts":
        return {**deepcopy(state), "offered_path_occupants": facts,
                "fact_scope": "CURRENT_HYPOTHETICAL_NODE_OCCUPANCY_ONLY_NOT_A_RESERVATION_VERDICT"}
    lines = ["Planning only. Positions below are the CURRENT hypothetical planning state.",
             "Every offered move is retained. Node occupancy facts are not a reservation verdict.",
             "Robots:"]
    for r in state["robots"]:
        lines.append(f"{r['robot_id']}: now N{r['current_node']}, original start N{r['original_start']}, "
                     f"goal N{r['original_goal']}, moving task={r['has_movement_request']}, stage={r['stage']}.")
    lines.append("Current other-robot occupants along each offered path:")
    for key, occupants in facts.items():
        detail = "; ".join(f"N{x['node']} occupied by {x['robot_id']}" for x in occupants)
        lines.append(f"{key}: {detail or 'no other robot currently on path nodes'}.")
    lines.append("Current chosen prefix: " + json.dumps(state["chosen_prefix"], ensure_ascii=False))
    lines.append("Previous checks refer to their own prefixes, which may differ from CURRENT positions:")
    for check in state["recent_checks"]:
        steps = check["steps"]
        prefix = "; ".join(f"{s['robot_id']} {s['action']} {'>'.join(s['path'])}" for s in steps)
        lines.append(f"Check {check['check']}: [{prefix}] => {check['result']}.")
    for key in ("wait_allowed_nodes", "blocked_nodes", "blocked_edges", "remaining_checks", "remaining_seconds"):
        lines.append(f"{key}: " + json.dumps(state[key], ensure_ascii=False))
    return "\n".join(lines)
