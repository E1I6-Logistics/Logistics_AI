"""주문 전체가 아닌 이번 픽업대의 차례를 판단한다. 실제 통행 허가는 예약기가 담당한다."""

PICKUP_PHASES = {"WAITING_PICKUP", "MOVING_TO_PICKUP", "WORKING_AT_PICKUP"}


def requested_pickup_node(order):
    if order.get("status") != "IN_PROGRESS" or order.get("phase") not in PICKUP_PHASES:
        return None
    index = order.get("pickup_index", 0)
    nodes = order.get("pickup_nodes", [])
    return str(nodes[index]) if 0 <= index < len(nodes) else None


def earlier_pickup_request(orders, order, node):
    """접수 순서가 앞선, 같은 픽업을 현재 요청 중인 주문이 있는가."""
    for previous in orders:
        if previous["order_id"] == order["order_id"]:
            break
        if (previous["mode"] == order["mode"]
                and requested_pickup_node(previous) == str(node)):
            return True
    return False
