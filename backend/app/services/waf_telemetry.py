"""robot4 관측 전용 저장소. 배송 Fleet·예약·경로 상태와 분리한다."""
from copy import deepcopy
import math
from threading import RLock
from time import time

ROBOT_ID = "robot4"


class WafTelemetry:
    def __init__(self, clock=time):
        self.clock = clock
        self.lock = RLock()
        self.seen = False
        self.ip = ""
        self.connected = False
        self.checked_at = None
        self.last_seen = None
        self.battery = None
        self.voltage = None
        self.battery_at = None
        self.pose = None
        self.pose_at = None
        self.actions = {}
        self.terminal_goals = {}
        self.feedback = None

    def sync_connections(self, devices):
        with self.lock:
            row = next((d for d in devices if d.get("name") == ROBOT_ID and d.get("connected")), None)
            now = self.clock()
            self.checked_at = now
            if row is not None:
                self.seen = True
                self.ip = str(row.get("ip", ""))
                self.last_seen = now
            self.connected = row is not None

    def observe_battery(self, msg):
        with self.lock:
            try:
                percent, voltage = float(msg.percentage), float(msg.voltage)
            except (ValueError, TypeError, AttributeError):
                return
            self.battery = percent * 100 if math.isfinite(percent) and 0 <= percent <= 1 else None
            self.voltage = voltage if math.isfinite(voltage) and voltage > 0 else None
            self.battery_at = self.clock()

    def observe_pose(self, msg, source):
        try:
            pose = msg.pose.pose if hasattr(msg.pose, "pose") else msg.pose
            x, y = float(pose.position.x), float(pose.position.y)
            q = pose.orientation
            qx, qy, qz, qw = map(float, (q.x, q.y, q.z, q.w))
            if not all(math.isfinite(v) for v in (x, y, qx, qy, qz, qw)):
                return
            norm = math.sqrt(qx*qx + qy*qy + qz*qz + qw*qw)
            if norm < 1e-9:
                return
            qx, qy, qz, qw = (v/norm for v in (qx, qy, qz, qw))
            yaw = math.atan2(2*(qw*qz+qx*qy), 1-2*(qy*qy+qz*qz))
            frame = str(msg.header.frame_id)
        except (ValueError, TypeError, AttributeError):
            return
        with self.lock:
            self.pose = dict(x=x, y=y, yaw=yaw, frame_id=frame, source=source)
            self.pose_at = self.clock()

    def observe_action_status(self, msg, source):
        with self.lock:
            # StatusArray는 변경 때 발행된다. 가동 중 상태를 짧은 시간만으로 지우지 않는다.
            goals, unidentified_active = {}, False
            for row in msg.status_list:
                status = int(row.status)
                try:
                    goal_id = bytes(row.goal_info.goal_id.uuid).hex()
                except (AttributeError, TypeError, ValueError):
                    goal_id = None
                if goal_id:
                    goals[goal_id] = status
                    if status in (4, 5, 6):
                        self.complete_goal(goal_id, status)
                elif status in (1, 2, 3):
                    unidentified_active = True
            self.actions[source] = dict(goals=goals, unidentified_active=unidentified_active,
                                        received_at=self.clock())

    def complete_goal(self, goal_id, status):
        """Result/Status로 종료가 확인된 동일 UUID의 늦은 EXECUTING 표시는 무시한다."""
        with self.lock:
            self.terminal_goals[goal_id] = int(status)
            if len(self.terminal_goals) > 256:
                del self.terminal_goals[next(iter(self.terminal_goals))]

    def goal_status(self, goal_id):
        with self.lock:
            return self.terminal_goals.get(goal_id)

    def observe_feedback(self, msg, source):
        with self.lock:
            self.feedback = dict(stage=str(msg.feedback.stage)[:100], source=source, received_at=self.clock())

    def external_active(self):
        with self.lock:
            return any(row['unidentified_active'] or any(
                status in (1, 2, 3) and goal_id not in self.terminal_goals
                for goal_id, status in row['goals'].items()) for row in self.actions.values())

    def snapshot(self):
        with self.lock:
            now = self.clock()
            connection_fresh = self.checked_at is not None and 0 <= now-self.checked_at <= 5
            state = ("CONNECTED" if self.connected else "DISCONNECTED") if connection_fresh else "UNKNOWN"
            battery_fresh = self.battery_at is not None and 0 <= now-self.battery_at <= 30
            pose_fresh = self.pose_at is not None and 0 <= now-self.pose_at <= 5
            return dict(robot_id=ROBOT_ID, role="replenishment", seen=self.seen, ip=self.ip,
                        connected=state == "CONNECTED", connection_state=state, last_seen=self.last_seen,
                        battery=self.battery if battery_fresh else None,
                        battery_voltage=self.voltage if battery_fresh else None,
                        battery_fresh=battery_fresh, battery_received_at=self.battery_at,
                        pose=deepcopy(self.pose) if pose_fresh else None, pose_fresh=pose_fresh,
                        pose_received_at=self.pose_at, external_active=self.external_active(),
                        feedback=deepcopy(self.feedback))


waf_telemetry = WafTelemetry()
