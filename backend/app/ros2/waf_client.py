"""WAF 전용 ROS 노드. 기존 FmsRosNode와 로봇 주행 클라이언트를 변경하지 않는다."""
import logging

LOG = logging.getLogger(__name__)


def create_waf_node(service):
    try:
        from rclpy.node import Node
        from rclpy.action import ActionClient
        from waffle_interfaces.action import PalletReplace
    except (ImportError, OSError, RuntimeError, AttributeError) as exc:
        service.unavailable("WAF 인터페이스 미준비 · 워크스페이스 빌드/source 후 FMS 재시작 필요")
        LOG.warning("WAF 인터페이스 준비 필요: %s", exc)
        return None
    node = None
    try:
        node = Node("fms_waf_replenishment")
        client = ActionClient(node, PalletReplace, "/pallet_replace")
        service.attach(client, PalletReplace.Goal)
        node.create_timer(1.0, service.tick)
        return node
    except Exception:
        if node is not None:
            node.destroy_node()
        service.unavailable("WAF 액션 클라이언트 초기화 실패")
        LOG.exception("WAF 초기화 실패 · 기존 FMS 기능은 계속 실행")
        return None
