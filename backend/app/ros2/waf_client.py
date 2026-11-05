"""WAF 전용 관측과 보충 액션. Nav2·cmd_vel·일반 배송 클라이언트는 생성하지 않는다."""
import logging
import os
from ..services.waf_telemetry import waf_telemetry

LOG = logging.getLogger(__name__)


class WafActionTransport:
    def __init__(self, clients, node=None):
        self.clients = clients
        self.node = node
        self.cancel_clients = {}
        self.action_name = clients[0][0]
        self.selected = clients[0][1]

    def server_is_ready(self):
        for name, client in self.clients:
            if client.server_is_ready():
                self.action_name, self.selected = name, client
                return True
        return False

    def send_goal_async(self, goal, goal_id=None, **kwargs):
        if goal_id is not None:
            from unique_identifier_msgs.msg import UUID
            kwargs['goal_uuid'] = UUID(uuid=list(bytes.fromhex(goal_id)))
        return self.selected.send_goal_async(goal, **kwargs)

    def cancel_goal_async(self, goal_id, action_name):
        """FMS 재시작 후에도 기록된 UUID 하나만 취소한다. 전체 취소는 보내지 않는다."""
        from action_msgs.srv import CancelGoal
        if self.node is None or action_name not in dict(self.clients):
            raise RuntimeError('기록된 WAF 액션의 취소 연결을 찾지 못했습니다')
        client = self.cancel_clients.get(action_name)
        if client is None:
            client = self.node.create_client(CancelGoal, action_name + '/_action/cancel_goal')
            self.cancel_clients[action_name] = client
        if not client.service_is_ready():
            raise RuntimeError('WAF 취소 서비스 연결 대기')
        request = CancelGoal.Request()
        request.goal_info.goal_id.uuid = list(bytes.fromhex(goal_id))
        return client.call_async(request)


def create_waf_node(service, telemetry=waf_telemetry):
    from rclpy.node import Node
    from geometry_msgs.msg import PoseStamped, PoseWithCovarianceStamped
    from sensor_msgs.msg import BatteryState
    from action_msgs.msg import GoalStatusArray
    from rclpy.qos import qos_profile_sensor_data, QoSProfile, DurabilityPolicy, ReliabilityPolicy

    node = None
    try:
        node = Node("fms_waf_replenishment")
        node.create_subscription(BatteryState, "/robot4/battery_state", telemetry.observe_battery, qos_profile_sensor_data)
        for topic, kind in (("/robot4/logitle_pose", PoseStamped), ("/robot4/amcl_pose", PoseWithCovarianceStamped)):
            node.create_subscription(kind, topic, lambda msg, source=topic: telemetry.observe_pose(msg, source), qos_profile_sensor_data)
        node.create_timer(1.0, service.tick)
        try:
            from rclpy.action import ActionClient
            from waffle_interfaces.action import PalletReplace
            override = os.environ.get("FMS_WAF_ACTION_NAME", "").strip()
            names = [override] if override else ["/robot4/pallet_replace", "/pallet_replace"]
            clients = [(name, ActionClient(node, PalletReplace, name)) for name in names]
            service.attach(WafActionTransport(clients, node), PalletReplace.Goal)
            status_qos = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL,
                                    reliability=ReliabilityPolicy.RELIABLE)
            for name in names:
                node.create_subscription(GoalStatusArray, name + "/_action/status",
                    lambda msg, source=name: telemetry.observe_action_status(msg, source), status_qos)
                node.create_subscription(PalletReplace.Impl.FeedbackMessage, name + "/_action/feedback",
                    lambda msg, source=name: telemetry.observe_feedback(msg, source), 10)
        except (ImportError, OSError, RuntimeError, AttributeError) as exc:
            service.unavailable("WAF 액션 인터페이스 미준비 · 워크스페이스 빌드/source 필요")
            LOG.warning("WAF 관측은 유지하며 액션 인터페이스 준비 대기: %s", exc)
        return node
    except Exception:
        if node is not None:
            node.destroy_node()
        service.unavailable("WAF 클라이언트 초기화 실패")
        LOG.exception("WAF 초기화 실패 · 기존 FMS 기능은 계속 실행")
        return None
