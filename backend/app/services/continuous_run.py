"""Transient navigation pauses retry automatically after real terminal/pose checks.

Never replay OMX/ArUco/docking operations or bypass emergency, occupancy or cancellation.
Only this branch's launcher enables the policy by default.
"""
import logging
import os
from copy import deepcopy
from ..models.robot import RobotState, NavigationType
from .mode_service import mode_manager

LOG = logging.getLogger(__name__)
RECOVERABLE = {
    'pose_stale_route_protected', 'robot_offline_route_protected',
    'reconnected_with_active_work', 'navigation_update_failed',
    'navigation_execution_error', 'goal_send_failed', 'navigation_result_failed',
    'navigation_aborted', 'arrival_failed', 'arrival_interrupted_offline',
    'current_segment_occupied', 'route_mismatch_protected',
}


class ContinuousRun:
    def __init__(self, navigation):
        self.nav = navigation
        self.enabled = os.getenv('FMS_CONTINUOUS_RUN', '0').lower() in {'1', 'true', 'yes', 'on'}
        self.pending = {}

    def observe(self, robot, reason):
        rid = robot.robot_id
        if not self.enabled or reason not in RECOVERABLE:
            self.pending.pop(rid, None)
            return
        if robot.goal_node is None or robot.navigation_type not in (NavigationType.GOAL, NavigationType.RETURN):
            return
        old = self.pending.get(rid)
        identity = (robot.order_id, str(robot.goal_node), robot.navigation_type)
        if old and old['identity'] == identity:
            return
        row = self.nav._robots.get(rid, {})
        route = row.get('route') or robot.route
        required = self.nav._traffic._required_paths.get(rid)
        path = deepcopy(list(required)) if required else (
            deepcopy(route.get('node_ids')) if isinstance(route, dict) and route.get('route_source') == 'llm' else None
        )
        self.pending[rid] = dict(identity=identity, path=path, attempts=0,
                                 retry_at=self.nav._clock() + 2., healthy_at=None)
        LOG.warning('[자동 복구] %s: %s · 기존 목적지·주문 보존, 위치·기존 Action 종료 확인 후 재개', rid, reason)

    def waiting(self, rid):
        entry = self.pending.get(rid)
        robot = self.nav._fleet.get_robot(rid)
        return bool(entry and robot and robot.state != RobotState.EMERGENCY_STOP
                    and not robot.reconnect_reset_pending
                    and (robot.order_id, str(robot.goal_node), robot.navigation_type) == entry['identity'])

    def tick(self):
        if not self.enabled or mode_manager.mode != self.nav.mode:
            return
        nav = self.nav
        now = nav._clock()
        for robot in nav._fleet.get_all_robots():
            rid = robot.robot_id
            if robot.state == RobotState.PAUSED and rid not in self.pending:
                self.observe(robot, robot.pause_reason_code)
            entry = self.pending.get(rid)
            if entry is None:
                continue
            identity = (robot.order_id, str(robot.goal_node), robot.navigation_type)
            if (robot.state == RobotState.EMERGENCY_STOP or robot.reconnect_reset_pending
                    or identity != entry['identity'] or robot.state not in (RobotState.PAUSED, RobotState.OFFLINE)):
                self.pending.pop(rid, None)
                continue
            if not robot.connected:
                entry['healthy_at'] = None
                continue
            if robot.pause_reason_code not in RECOVERABLE:
                self.pending.pop(rid, None)
                continue
            if (not robot.connected or robot.docked_station is not None
                    or rid in nav._executing or rid in nav._stopping or rid in nav._preparing
                    or rid in nav._arrivals or rid in nav._station_pending):
                entry['healthy_at'] = None
                continue
            try:
                nav.refresh()  # Original graph, measured pose, occupancy and ROS registration gates.
                state = nav._robots.get(rid)
                if (not state or state.get('connection_state') != 'ONLINE'
                        or rid in nav._sync_errors):
                    raise ValueError('위치·통신 확인 대기')
                if entry['healthy_at'] is None:
                    entry['healthy_at'] = now
                if now < entry['retry_at'] or now - entry['healthy_at'] < .5:
                    continue
                entry['attempts'] += 1
                entry['retry_at'] = now + min(30., 2. ** min(entry['attempts'], 5))
                previous_reason = robot.pause_reason_code
                robot.set_state(RobotState.WAITING)
                try:
                    if rid in nav._requests and state.get('route') is not None:
                        nav._replan_after_stop(rid)
                        nav._resume_pending.add(rid)
                    else:
                        path = entry['path']
                        if path:
                            current = state.get('occupied_node')
                            if current is None or str(current) not in list(map(str, path)):
                                raise ValueError('기존 경로상의 노드 위치 확인 대기')
                            path = list(map(str, path))
                            path = path[path.index(str(current)):]
                        nav.request(rid, entry['identity'][1], entry['identity'][2], required_path=path)
                except Exception:
                    # No fake success; original pause is restored and retry is backed off.
                    robot.pause(previous_reason)
                    raise
                self.pending.pop(rid, None)
                LOG.warning('[자동 복구] %s: 기존 목적지 %s 재개 · 물리 작업 재전송 없음', rid, entry['identity'][1])
            except (OSError, ValueError, KeyError, RuntimeError) as exc:
                entry['healthy_at'] = None
                # Keep the intent; a later fresh observation may make the same request valid.
                if now >= entry['retry_at']:
                    entry['retry_at'] = now + 3.
                    LOG.info('[자동 복구] %s: 재점검 대기 · %s', rid, exc)
