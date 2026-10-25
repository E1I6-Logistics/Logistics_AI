#!/usr/bin/env python3
"""ROS 2 Jazzy 3-way benchmark: FastDDS / CycloneDDS / Zenoh ROS2 bridge.

A single file on both hosts. No robot bringup or actual sensors are required.
Main orchestrator starts remote agent via SSH and switches RMW in child processes.
Only private /bench3/... topics are used; no /cmd_vel or real sensor topics.
"""
from __future__ import annotations

import argparse
from array import array
import csv
from datetime import datetime
import json
import math
import os
from pathlib import Path
import select
import shlex
import signal
import socket
import statistics
import subprocess
import sys
import threading
import time
import uuid

try:
    import psutil
except ImportError:
    psutil = None

# (key, ROS type, description, data spec, rate Hz, data QoS)
# OpenCR-powered TurtleBot3 safe-rate presets. Payload sizes remain representative.
# Rates are intentionally lower than real sensor rates to prioritize robot stability.
PROFILES = [
    ('small', 'String', '100B 상태 메시지', 100, 20, 'reliable'),
    ('twist', 'Twist', '속도 명령 ROS2 Twist 메시지', 0, 10, 'reliable'),
    ('odom', 'Odometry', '로봇 Odometry', 0, 10, 'reliable'),
    ('scan720', 'LaserScan', '2D LaserScan 720 rays', 720, 5, 'best_effort'),
    ('scan1080', 'LaserScan', '2D LaserScan 1080 rays', 1080, 5, 'best_effort'),
    ('cloud30k', 'PointCloud2', '3D PointCloud2 30k points (480KB)', 30000, 2, 'best_effort'),
    ('cloud100k', 'PointCloud2', '3D PointCloud2 100k points (1.6MB)', 100000, 1, 'best_effort'),
    ('rgb_vga', 'Image', 'RGB8 640x480', (640, 480, 'rgb8'), 2, 'best_effort'),
    ('rgb_hd', 'Image', 'RGB8 1280x720', (1280, 720, 'rgb8'), 1, 'best_effort'),
    ('depth_vga', 'Image', '16UC1 640x480', (640, 480, '16UC1'), 2, 'best_effort'),
]
PROFILE_MAP = {p[0]: p for p in PROFILES}
GROUPS = {
    'core': ['small', 'twist', 'odom'],
    'sensor': ['scan720', 'scan1080', 'cloud30k', 'cloud100k', 'rgb_vga', 'rgb_hd', 'depth_vga'],
    'all': [p[0] for p in PROFILES if p[0] not in ('cloud100k', 'rgb_hd')],
    'heavy': ['cloud100k','rgb_hd'],
}
MODES = {'fastdds': 'rmw_fastrtps_cpp', 'cyclone': 'rmw_cyclonedds_cpp', 'zenoh': 'rmw_cyclonedds_cpp'}
BASE = '/bench3'


def clock_ns():
    return time.monotonic_ns()


def time_ns():
    return time.time_ns()


def percentile(values, fraction):
    if not values:
        return None
    sorted_v = sorted(values)
    k = max(0, math.ceil(fraction * len(sorted_v)) - 1)
    return round(sorted_v[k], 3)


def metrics(values):
    if not values:
        return {'n': 0, 'avg': None, 'trim5_avg': None, 'median': None,
                'p95': None, 'p99': None, 'max': None, 'std': None, 'iqr_outliers': 0}
    arr = sorted(values)
    trim = int(len(arr) * 0.05)
    inner = arr[trim: len(arr)-trim] if len(arr) > 2*trim else arr
    q1, q3 = percentile(arr, .25), percentile(arr, .75)
    bound = q3 + 1.5 * (q3 - q1)
    return dict(n=len(arr), avg=round(statistics.mean(arr), 3),
                trim5_avg=round(statistics.mean(inner), 3),
                median=round(statistics.median(arr), 3),
                p95=percentile(arr, .95), p99=percentile(arr, .99),
                max=round(arr[-1], 3),
                std=round(statistics.pstdev(arr), 3),
                iqr_outliers=sum(v > bound for v in arr))


def gap_metrics(receive_times_ns, rate):
    times = sorted(receive_times_ns)
    gaps = [(times[i] - times[i-1])/1e6 for i in range(1, len(times))]
    ideal = 1000.0 / rate
    jitter = [abs(t - ideal) for t in gaps]
    return {'gap_ms': metrics(gaps), 'jitter_abs_ms': metrics(jitter),
            'gap_over_150pct': sum(t > ideal * 1.5 for t in gaps)}


def cpu_time(t):
    return t.user + t.system


class CpuMonitor:
    def __init__(self):
        self.start = time.monotonic()
        self.self_start = time.process_time()
        self.system_start = psutil.cpu_times() if psutil else None
        self.procs = {}
        if psutil:
            for p in psutil.process_iter(['pid','name','cmdline']):
                try:
                    name = (p.info['name'] or '').lower()
                    cmd = ' '.join(p.info['cmdline'] or []).lower()
                    if 'zenoh-bridge-ros2dds' in name or 'zenoh-bridge-ros2dds' in cmd:
                        kind = 'bridge'
                    elif 'zenohd' in name:
                        kind = 'router'
                    else:
                        continue
                    self.procs[p.pid] = (kind, cpu_time(p.cpu_times()))
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass

    def finish(self):
        interval = max(.001, time.monotonic() - self.start)
        result = {'app_cpu_pct_one_core': round(100 * (time.process_time() - self.self_start)/interval, 2),
                  'system_cpu_pct_all_cores': None, 'bridge_cpu_pct_one_core': None,
                  'router_cpu_pct_one_core': None}
        if not psutil:
            return result
        end = psutil.cpu_times()
        total = sum(end) - sum(self.system_start)
        idle = (end.idle + getattr(end, 'iowait', 0)) - (self.system_start.idle + getattr(self.system_start, 'iowait', 0))
        result['system_cpu_pct_all_cores'] = round(100*(total-idle)/total, 2) if total > 0 else None
        sums = {'bridge': 0.0, 'router': 0.0}
        found = set()
        for pid, (kind, start) in self.procs.items():
            try:
                sums[kind] += max(0, cpu_time(psutil.Process(pid).cpu_times()) - start)
                found.add(kind)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        for kind in found:
            result[f'{kind}_cpu_pct_one_core'] = round(100*sums[kind]/interval, 2)
        return result


def build_msg(p):
    key, msg_type, label, spec, rate, qos = p
    if msg_type == 'String':
        from std_msgs.msg import String
        return String(data='X' * spec)
    if msg_type == 'Twist':
        from geometry_msgs.msg import Twist
        msg = Twist()
        msg.linear.x = 0.1
        return msg
    if msg_type == 'Odometry':
        from nav_msgs.msg import Odometry
        msg = Odometry()
        msg.pose.pose.orientation.w = 1.0
        return msg
    if msg_type == 'LaserScan':
        from sensor_msgs.msg import LaserScan
        msg = LaserScan()
        msg.angle_min = -math.pi
        msg.angle_max = math.pi
        msg.angle_increment = 2*math.pi/spec
        msg.range_min = 0.1
        msg.range_max = 12.0
        msg.ranges = array('f', [4.0])*spec
        msg.intensities = array('f', [1.0])*spec
        return msg
    if msg_type == 'PointCloud2':
        from sensor_msgs.msg import PointCloud2, PointField
        msg = PointCloud2()
        msg.height = 1
        msg.width = spec
        msg.fields = [PointField(name=name, offset=offset, datatype=PointField.FLOAT32, count=1)
                      for name, offset in [('x',0), ('y',4), ('z',8), ('intensity',12)]]
        msg.is_bigendian = False
        msg.point_step = 16
        msg.row_step = spec * 16
        msg.is_dense = True
        msg.data = array('B', [0]) * (spec*16)
        return msg
    if msg_type == 'Image':
        from sensor_msgs.msg import Image
        width, height, fmt = spec
        bpp = 2 if fmt == '16UC1' else 3
        msg = Image()
        msg.height, msg.width, msg.encoding = height, width, fmt
        msg.step = width*bpp
        msg.is_bigendian = 0
        msg.data = array('B', [0]) * (width*height*bpp)
        return msg
    raise ValueError(key)


def message_class(p):
    kind = p[1]
    if kind == 'String':
        from std_msgs.msg import String
        return String
    if kind == 'Twist':
        from geometry_msgs.msg import Twist
        return Twist
    if kind == 'Odometry':
        from nav_msgs.msg import Odometry
        return Odometry
    from sensor_msgs.msg import Image, LaserScan, PointCloud2
    return {'Image': Image, 'LaserScan': LaserScan, 'PointCloud2': PointCloud2}[kind]


def encode_data(msg, p, run_id, seq):
    # Every message carries a sequence ID and the sending device's wallclock.
    frame = f'{run_id}:{seq}:{time_ns()}'
    if p[1] == 'String':
        msg.data = frame + '|' + 'X' * max(0, p[3] - len(frame) - 1)
    elif p[1] == 'Twist':
        # ROS2 Twist has no Header; metadata uses otherwise-unused fields.
        # Tests run only on /bench3/up|down/twist, never /cmd_vel.
        msg.linear.y = float(seq)
        msg.linear.z = float(time_ns())
        msg.angular.y = float(int(run_id, 16))
    else:
        msg.header.frame_id = frame
        wall = time.time_ns()
        msg.header.stamp.sec = wall//1_000_000_000
        msg.header.stamp.nanosec = wall%1_000_000_000
    return msg


def decode_data(msg, p):
    if p[1] == 'Twist':
        return f'{int(msg.angular.y):012x}', int(msg.linear.y), int(msg.linear.z)
    frame = msg.data.split('|', 1)[0] if p[1] == 'String' else msg.header.frame_id
    rid, seq, stamp = frame.split(':', 2)
    return rid, int(seq), int(stamp)


def make_qos(kind):
    from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy, HistoryPolicy
    return QoSProfile(history=HistoryPolicy.KEEP_LAST, depth=10,
                      reliability=ReliabilityPolicy.RELIABLE if kind == 'reliable' else ReliabilityPolicy.BEST_EFFORT,
                      durability=DurabilityPolicy.VOLATILE)


def send_json(pub, data):
    from std_msgs.msg import String
    pub.publish(String(data=json.dumps(data, ensure_ascii=False, separators=(',', ':'))))


def read_json(msg):
    try:
        return json.loads(msg.data)
    except (json.JSONDecodeError, ValueError):
        return {}


def close_process(proc, label, seconds=6):
    if proc is None or proc.poll() is not None:
        return
    try:
        if proc.stdin and not proc.stdin.closed:
            proc.stdin.close()
    except OSError:
        pass
    try:
        proc.wait(timeout=seconds)
        return
    except subprocess.TimeoutExpired:
        pass
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def spawn_bridge(router):
    from shutil import which
    exe = which('zenoh-bridge-ros2dds')
    if not exe:
        # Some ROS2 installations expose the executable only through 'ros2 run'.
        try:
            prefix = subprocess.check_output(['ros2','pkg','prefix','zenoh_bridge_ros2dds'],
                                             text=True, stderr=subprocess.DEVNULL,timeout=5).strip()
            candidate = Path(prefix)/'lib'/'zenoh_bridge_ros2dds'/'zenoh-bridge-ros2dds'
            if candidate.exists() and os.access(candidate, os.X_OK):
                exe = str(candidate)
        except (subprocess.SubprocessError, FileNotFoundError):
            pass
    if not exe:
        raise RuntimeError('zenoh-bridge-ros2dds 실행 파일 없음: PATH / ros2 pkg prefix zenoh_bridge_ros2dds 확인')
    # This subprocess is the benchmark's own bridge; existing FMS bridges are not changed.
    command = [exe, '-d', os.environ.get('ROS_DOMAIN_ID','67'),
               '--ros-automatic-discovery-range','LOCALHOST','-e',router,'client']
    log = open(Path('/tmp') / f'ros_bench_bridge_{os.getpid()}.log','w',encoding='utf-8')
    proc = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
    log.close()
    return proc

def remote_agent(args):
    import rclpy
    from rclpy.node import Node
    from std_msgs.msg import String

    bridge = None
    should_exit = threading.Event()
    def stdin_watch():
        # Closing the SSH pipe on the main host stops the remote process and bridge.
        try:
            for line in sys.stdin:
                if line.strip() == 'STOP':
                    break
        except (OSError, ValueError):
            pass
        should_exit.set()
    threading.Thread(target=stdin_watch, daemon=True).start()

    if args.mode == 'zenoh':
        bridge = spawn_bridge(f'tcp/{args.router_ip}:{args.router_port}')
    rclpy.init()

    class RobotAgent(Node):
        def __init__(self):
            super().__init__('bench3_robot_agent')
            self.status = self.create_publisher(String, BASE+'/status', make_qos('reliable'))
            self.ack_pub = self.create_publisher(String, BASE+'/ack', make_qos('reliable'))
            self.control_sub = self.create_subscription(String, BASE+'/control', self.handle_control, make_qos('reliable'))
            self.ack_sub = self.create_subscription(String, BASE+'/ack', self.handle_ack, make_qos('reliable'))
            self.up_subs = {}
            self.down_pubs = {}
            self.templates = {}
            for p in PROFILES:
                key = p[0]
                msg_cls = message_class(p)
                self.up_subs[key] = self.create_subscription(msg_cls, BASE+'/up/'+key,
                    lambda m, key=key: self.recv_up(m, key), make_qos(p[5]))
                self.down_pubs[key] = self.create_publisher(msg_cls, BASE+'/down/'+key, make_qos(p[5]))
                self.templates[key] = build_msg(p)
            self.run = None
            self.cpu_monitor = None
            self.seq_received = set()
            self.recv_times = []
            self.rx_wall = []
            self.send_monotonic = {}
            self.ack_rtt = {}
            self.remote_rx_clock_ms = []
            self.tx_count = 0
            self.next_slot = 0
            self.start_send = None
            self.send_end = None
            self.finished = False
            self.timer = self.create_timer(.002, self.send_tick)

        def handle_control(self, msg):
            packet = read_json(msg)
            rid, cmd = packet.get('rid'), packet.get('cmd')
            if cmd == 'quit':
                should_exit.set()
                send_json(self.status, {'cmd':'bye', 'rid':rid})
                return
            if cmd == 'begin':
                if self.run and self.run.get('rid') == rid:
                    send_json(self.status, {'cmd':'ready', 'rid':rid})
                    return
                self.run = packet
                self.cpu_monitor = CpuMonitor()
                self.seq_received = set()
                self.recv_times = []
                self.rx_wall = []
                self.send_monotonic = {}
                self.ack_rtt = {}
                self.remote_rx_clock_ms = []
                self.tx_count = 0
                self.next_slot = 0
                self.start_send = None
                self.send_end = None
                self.finished = False
                send_json(self.status, {'cmd':'ready', 'rid':rid})
            elif cmd == 'start' and self.run and self.run['rid'] == rid and self.run['direction'] == 'robot_to_pc':
                self.start_send = clock_ns()
                self.send_end = self.start_send + int(float(self.run['duration']) * 1e9)
            elif cmd == 'end' and self.run and self.run['rid'] == rid:
                self.finish()

        def recv_up(self, msg, key):
            if not self.run or self.run['direction'] != 'pc_to_robot' or self.run['profile'] != key:
                return
            rx_clock = clock_ns()
            rx_wall = time_ns()
            try:
                rid, seq, send_wall = decode_data(msg, PROFILE_MAP[key])
            except (ValueError, AttributeError, IndexError):
                return
            if rid != self.run['rid'] or seq in self.seq_received:
                return
            self.seq_received.add(seq)
            self.recv_times.append(rx_clock)
            self.rx_wall.append((rx_wall - send_wall)/1e6)
            send_json(self.ack_pub, {'rid':rid, 'seq':seq, 'from':'robot', 'rx_wall':rx_wall})

        def handle_ack(self, msg):
            ack = read_json(msg)
            if not self.run or self.run['direction'] != 'robot_to_pc':
                return
            if ack.get('rid') != self.run['rid'] or ack.get('from') != 'pc':
                return
            seq = ack.get('seq')
            if seq in self.send_monotonic and seq not in self.ack_rtt:
                self.ack_rtt[seq] = (clock_ns() - self.send_monotonic[seq])/1e6
                self.remote_rx_clock_ms.append((ack['rx_wall'] - ack['send_wall'])/1e6)

        def send_tick(self):
            if not self.run or self.start_send is None or self.finished:
                return
            now = clock_ns()
            key = self.run['profile']
            rate = float(self.run['rate'])
            maximum = max(1, int(float(self.run['duration'])*rate))
            # Never burst to "catch up" after CPU overload; that would hide scheduler saturation.
            if now < self.send_end:
                # Send at most one payload per schedule slot: never catch up in a burst.
                slot = min(maximum-1, int((now-self.start_send)*rate/1e9))
                if slot >= self.next_slot:
                    seq = slot
                    self.next_slot = slot + 1
                    self.send_monotonic[seq] = clock_ns()
                    msg = encode_data(self.templates[key], PROFILE_MAP[key], self.run['rid'], seq)
                    self.down_pubs[key].publish(msg)
                    self.tx_count += 1
            elif now >= self.send_end and not self.finished:
                self.finished = True
                send_json(self.status, {'cmd':'sent', 'rid':self.run['rid'], 'sent':self.tx_count})

        def finish(self):
            if self.run is None:
                return
            c = self.cpu_monitor.finish()
            if self.run['direction'] == 'pc_to_robot':
                body = {'received':len(self.seq_received), 'receive_gap':gap_metrics(self.recv_times, self.run['rate']),
                        'raw_one_way_ms': metrics(self.rx_wall)}
            else:
                body = {'sent':self.tx_count, 'acked':len(self.ack_rtt),
                        'rtt_ms':metrics(list(self.ack_rtt.values())),
                        'raw_one_way_ms': metrics(self.remote_rx_clock_ms)}
            send_json(self.status, {'cmd':'result','rid':self.run['rid'], 'cpu':c, 'metrics':body})

    node = RobotAgent()
    try:
        print('AGENT_READY', flush=True)
        while rclpy.ok() and not should_exit.is_set():
            rclpy.spin_once(node, timeout_sec=.02)
    finally:
        node.destroy_node()
        rclpy.shutdown()
        close_process(bridge, 'robot bridge', seconds=2)


def local_client(args):
    import rclpy
    from rclpy.node import Node
    from std_msgs.msg import String
    rclpy.init()

    class MainClient(Node):
        def __init__(self):
            super().__init__('bench3_main_client')
            self.control = self.create_publisher(String, BASE+'/control', make_qos('reliable'))
            self.ack_pub = self.create_publisher(String, BASE+'/ack', make_qos('reliable'))
            self.status_sub = self.create_subscription(String, BASE+'/status', self.status_callback, make_qos('reliable'))
            self.ack_sub = self.create_subscription(String, BASE+'/ack', self.ack_callback, make_qos('reliable'))
            self.up_pubs = {}
            self.down_subs = {}
            self.templates = {}
            for p in PROFILES:
                key = p[0]
                msg_cls = message_class(p)
                self.up_pubs[key] = self.create_publisher(msg_cls, BASE+'/up/'+key, make_qos(p[5]))
                self.down_subs[key] = self.create_subscription(msg_cls, BASE+'/down/'+key,
                    lambda m, key=key: self.down_callback(m, key), make_qos(p[5]))
                self.templates[key] = build_msg(p)
            self.run = None
            self.statuses = {}
            self.send_at = {}
            self.arrivals = {}
            self.rtt = {}
            self.raw_one_way = []
            self.remote_result = None
            self.cpu_monitor = None

        def status_callback(self, msg):
            packet = read_json(msg)
            if packet.get('rid'):
                self.statuses[(packet['rid'], packet.get('cmd'))] = packet

        def ack_callback(self, msg):
            ack = read_json(msg)
            if not self.run or self.run['direction'] != 'pc_to_robot':
                return
            if ack.get('rid') != self.run['rid'] or ack.get('from') != 'robot':
                return
            seq = ack.get('seq')
            if seq in self.send_at and seq not in self.rtt:
                self.rtt[seq] = (clock_ns() - self.send_at[seq])/1e6
                # Negative values are possible if wallclock is not synced.
                self.raw_one_way.append((ack['rx_wall'] - self.sent_wall[seq])/1e6)

        def down_callback(self, msg, key):
            if not self.run or self.run['direction'] != 'robot_to_pc' or self.run['profile'] != key:
                return
            now = clock_ns()
            wall = time_ns()
            try:
                rid, seq, sent_wall = decode_data(msg, PROFILE_MAP[key])
            except (ValueError, AttributeError, IndexError):
                return
            if rid != self.run['rid'] or seq in self.arrivals:
                return
            self.arrivals[seq] = now
            self.raw_one_way.append((wall - sent_wall)/1e6)
            send_json(self.ack_pub, {'rid':rid, 'seq':seq,'from':'pc','rx_wall':wall,'send_wall':sent_wall})

        def wait_status(self, rid, what, timeout):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if (rid,what) in self.statuses:
                    return self.statuses[(rid,what)]
                rclpy.spin_once(self, timeout_sec=.02)
            raise RuntimeError(f'{what} 응답 타임아웃 ({timeout}s). 로봇 agent, Bridge, Router, Domain 확인 필요')

        def send_control(self, body):
            send_json(self.control, body)

        def case(self, mode, profile, direction, rep):
            p = PROFILE_MAP[profile]
            rid = uuid.uuid4().hex[:12]
            self.run = {'rid':rid,'cmd':'begin','profile':profile,'direction':direction,
                        'rate':p[4],'duration':args.duration}
            self.send_at = {}
            self.sent_wall = {}
            self.arrivals = {}
            self.rtt = {}
            self.raw_one_way = []
            self.statuses = {}
            print(f'[{mode}] {profile:10s} {direction:12s} #{rep} {args.duration:g}s @ {p[4]}Hz', flush=True)
            # Reliable control messages repeated until the ROS/Zenoh routes are discovered.
            deadline = time.monotonic()+25
            while time.monotonic() < deadline:
                self.send_control(self.run)
                for _ in range(10):
                    rclpy.spin_once(self, timeout_sec=.05)
                if (rid,'ready') in self.statuses:
                    break
            else:
                raise RuntimeError('로봇 agent READY 없음 (Domain/Bridge/허용 필터 검사)')
            time.sleep(.4)
            self.cpu_monitor = CpuMonitor()
            if direction == 'pc_to_robot':
                self.send_pc_to_robot(p, rid)
            else:
                self.send_robot_to_pc(p, rid)
            self.send_control({'cmd':'end','rid':rid})
            try:
                robot_result = self.wait_status(rid,'result',12)
            except RuntimeError:
                self.send_control({'cmd':'end','rid':rid})
                robot_result = self.wait_status(rid,'result',8)
            cpu_main = self.cpu_monitor.finish()
            if direction == 'pc_to_robot':
                sent = len(self.send_at)
                received = robot_result['metrics']['received']
                acked = len(self.rtt)
                rtt_ms = metrics(list(self.rtt.values()))
                arrive = robot_result['metrics']['receive_gap']
            else:
                sent = robot_result['metrics']['sent']
                received = len(self.arrivals)
                acked = robot_result['metrics']['acked']
                rtt_ms = robot_result['metrics']['rtt_ms']
                arrive = gap_metrics(list(self.arrivals.values()), p[4])
            payload = payload_size(p)
            return {'timestamp':datetime.now().isoformat(timespec='seconds'), 'backend':mode,
                    'profile':profile, 'description':p[2], 'direction':direction, 'repeat':rep,
                    'duration_s':args.duration, 'target_hz':p[4], 'qos':p[5], 'payload_bytes_approx':payload,
                    'expected_count':int(args.duration*p[4]), 'send_schedule_misses':max(0,int(args.duration*p[4])-sent), 'sent_count':sent, 'received_count':received,
                    'ack_count':acked, 'loss_pct': round(max(0, sent-received)/sent*100,3) if sent else None,
                    'ack_missing_pct':round(max(0,sent-acked)/sent*100,3) if sent else None,
                    'actual_rx_hz':round(received/args.duration,3),
                    'goodput_mbps_approx': round(received*payload*8/(args.duration*1e6),3),
                    'rtt_ms':rtt_ms, 'receive_gap':arrive,
                    'raw_one_way_ms_unverified':metrics(self.raw_one_way),
                    'pc_cpu':cpu_main, 'robot_cpu':robot_result['cpu']}

        def send_pc_to_robot(self, p, rid):
            key, rate = p[0], p[4]
            count = int(args.duration*rate)
            start = clock_ns()
            end = start + int(args.duration*1e9)
            period_ns = int(1e9/rate)
            for seq in range(count):
                scheduled = start + int(seq*1e9/rate)
                while clock_ns() < scheduled:
                    rclpy.spin_once(self, timeout_sec=min(.003, max(0.0,(scheduled-clock_ns())/1e9)))
                now = clock_ns()
                if now >= end:
                    break
                if now - scheduled > period_ns:
                    # Overload: count an unsent slot instead of publishing a false burst.
                    continue
                self.send_at[seq] = now
                msg = encode_data(self.templates[key], p, rid, seq)
                _, _, sent_wall = decode_data(msg, p)
                self.sent_wall[seq] = sent_wall
                self.up_pubs[key].publish(msg)
                rclpy.spin_once(self, timeout_sec=0)
            # Collect delayed ACKs without affecting scheduled transmit rate.
            drain = time.monotonic()+max(2.0, min(8, args.duration*.15))
            while time.monotonic() < drain and len(self.rtt) < len(self.send_at):
                rclpy.spin_once(self, timeout_sec=.05)

        def send_robot_to_pc(self, p, rid):
            self.send_control({'cmd':'start','rid':rid})
            self.wait_status(rid,'sent', args.duration+30)
            deadline = time.monotonic()+max(2.0, min(8,args.duration*.15))
            while time.monotonic() < deadline:
                rclpy.spin_once(self, timeout_sec=.05)

    node = MainClient()
    jsonl = Path(args.jsonl)
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    try:
        with jsonl.open('a', encoding='utf-8') as f:
            for rep in range(1, args.repeats+1):
                for profile in args.profiles.split(','):
                    for direction in args.directions.split(','):
                        row = node.case(args.mode, profile, direction, rep)
                        f.write(json.dumps(row, ensure_ascii=False)+'\n')
                        f.flush()
                        d = row['rtt_ms']
                        print(f"  RTT avg={d['avg']}ms P95={d['p95']}ms P99={d['p99']}ms "
                              f"rx={row['received_count']}/{row['sent_count']} "
                              f"loss={row['loss_pct']}% actual={row['actual_rx_hz']}Hz", flush=True)
                        time.sleep(.7)
        node.send_control({'cmd':'quit','rid':uuid.uuid4().hex[:12]})
        for _ in range(4):
            rclpy.spin_once(node, timeout_sec=.1)
    finally:
        node.destroy_node()
        rclpy.shutdown()


def payload_size(p):
    if p[1] == 'String': return p[3]
    if p[1] == 'Twist': return 48
    if p[1] == 'Odometry': return 760
    if p[1] == 'LaserScan': return p[3]*8+32
    if p[1] == 'PointCloud2': return p[3]*16+128
    if p[1] == 'Image':
        w,h,fmt = p[3]
        return w*h*(2 if fmt == '16UC1' else 3)+128
    raise ValueError(p[0])


def shell_env(mode, role, args):
    env = os.environ.copy()
    env['RMW_IMPLEMENTATION'] = MODES[mode]
    env['ROS_DOMAIN_ID'] = str(args.main_domain if role == 'main' or mode != 'zenoh' else args.robot_zenoh_domain)
    env['ROS_AUTOMATIC_DISCOVERY_RANGE'] = 'LOCALHOST' if mode == 'zenoh' else 'SUBNET'
    env.pop('ROS_LOCALHOST_ONLY', None)
    env.pop('ROS_STATIC_PEERS', None)
    return env


def select_profiles(value):
    names = GROUPS.get(value, value.split(','))
    bad = [name for name in names if name not in PROFILE_MAP]
    if bad:
        raise ValueError(f'알 수 없는 프로파일: {bad}. 선택 가능: {list(PROFILE_MAP)}')
    return names


def build_shell_command(args, mode, role, extra):
    env = shell_env(mode, role, args)
    # Source ROS before overriding the environment. This includes the requested Python interpreter.
    envs = ' '.join(f'{v}={shlex.quote(env[v])}' for v in ('RMW_IMPLEMENTATION', 'ROS_DOMAIN_ID','ROS_AUTOMATIC_DISCOVERY_RANGE'))
    cmd = ' '.join(shlex.quote(x) for x in extra)
    extra_setup = getattr(args, 'main_setup', None) if role == 'main' else getattr(args, 'robot_setup', None)
    overlay = (' && source '+shlex.quote(extra_setup)) if extra_setup else ''
    return f'source /opt/ros/jazzy/setup.bash{overlay} && unset ROS_LOCALHOST_ONLY ROS_STATIC_PEERS && {envs} {cmd}'


def wait_agent_ready(proc, timeout=35):
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f'SSH 로봇 agent 비정상 종료 (code {proc.returncode})')
        rd, _, _ = select.select([proc.stdout], [], [], min(1.0, deadline-time.monotonic()))
        if rd:
            line = proc.stdout.readline()
            if not line:
                continue
            if line.strip() == 'AGENT_READY':
                return
            print('  robot:',line.rstrip(),flush=True)
    raise RuntimeError('로봇 agent READY 대기 시간 초과. SSH 인증/ROS 환경/실행 파일을 확인하세요.')


def control_main(args):
    profiles = select_profiles(args.profiles)
    modes = ['fastdds','cyclone','zenoh'] if args.modes == 'all' else args.modes.split(',')
    if any(m not in MODES for m in modes):
        raise ValueError('modes는 all 또는 fastdds,cyclone,zenoh 조합만 가능합니다')
    dirs = ['pc_to_robot','robot_to_pc'] if args.directions == 'both' else args.directions.split(',')
    if any(d not in ('pc_to_robot','robot_to_pc') for d in dirs):
        raise ValueError('directions는 both 또는 pc_to_robot/robot_to_pc')
    total_cases = len(profiles)*len(modes)*len(dirs)*args.repeats
    seconds = total_cases*(args.duration+3) + len(modes)*25
    print('=== ROS2 DDS / Zenoh 안전 저부하 벤치마크 ===')
    print('주의: 저전압 이력이 있는 OpenCR 전원 구성에서는 낮은 Hz도 안전을 보장하지 않습니다.')
    print('기본 all은 100k PointCloud, 720p RGB를 제외합니다; 필요 시 --profiles heavy로 별도 실행.')
    print('Backend:',', '.join(modes), '| 프로파일:',', '.join(profiles))
    print('방향:', ', '.join(dirs), '| 반복:',args.repeats, '| 각 조건:',args.duration,'초')
    print(f'예상 실행시간: 약 {seconds/60:.1f}분 (네트워크 상황에 따라 증가)')
    print(f'DDS Main/Robot Domain={args.main_domain}; Zenoh Main={args.main_domain} Robot={args.robot_zenoh_domain}')
    if args.dry_run:
        return 0
    if 'zenoh' in modes:
        try:
            with socket.create_connection((args.router_ip,args.router_port),timeout=3):
                pass
        except OSError as ex:
            raise RuntimeError(f'Zenoh Router tcp/{args.router_ip}:{args.router_port} 연결 실패: {ex}')
    folder = Path(args.output).expanduser().resolve() / ('bench_'+datetime.now().strftime('%Y%m%d_%H%M%S'))
    folder.mkdir(parents=True, exist_ok=True)
    jsonl = folder/'raw_runs.jsonl'
    cfg = {'profiles':profiles,'modes':modes,'directions':dirs,'duration':args.duration,
           'repeats':args.repeats,'main_domain':args.main_domain,
           'robot_zenoh_domain':args.robot_zenoh_domain,'router_ip':args.router_ip,
           'robot_ip':args.robot_ip, 'note':'Synthetic messages; actual ROS2 message types; no physical sensors.'}
    (folder/'experiment_config.json').write_text(json.dumps(cfg,indent=2,ensure_ascii=False),encoding='utf-8')
    print('결과 저장:',folder, flush=True)
    errors = []
    try:
        for mode in modes:
            print('\n========',mode,'========',flush=True)
            bridge = None
            remote = None
            try:
                # Main starts its own isolated Bridge only for the Zenoh phase.
                if mode == 'zenoh':
                    oldenv = os.environ.copy()
                    os.environ.update(shell_env(mode, 'main', args))
                    try:
                        bridge = spawn_bridge(f'tcp/127.0.0.1:{args.router_port}')
                    finally:
                        os.environ.clear()
                        os.environ.update(oldenv)
                    time.sleep(2)
                    if bridge.poll() is not None:
                        raise RuntimeError('메인 Bridge 시작 실패; /tmp/ros_bench_bridge_*.log 확인')
                robot_file = f'~/communication_test/{Path(__file__).name}' if args.robot_file is None else args.robot_file
                # Do not quote ~ expansion away in the remote shell; use $HOME/path.
                if robot_file.startswith('~/'):
                    robot_file = '$HOME/'+robot_file[2:]
                agent_cmd = build_shell_command(args,mode,'robot',
                    ['python3','-u',robot_file,'agent','--mode',mode,'--router-ip',args.router_ip,
                     '--router-port',str(args.router_port)])
                # Expand the trusted $HOME shell path while quoting all other args.
                agent_cmd = agent_cmd.replace(shlex.quote(robot_file),robot_file)
                # SSH reads passwords from /dev/tty if keys are not installed.
                remote = subprocess.Popen(['ssh','-T','-o','ConnectTimeout=10',
                    f'{args.robot_user}@{args.robot_ip}', 'bash -c '+shlex.quote(agent_cmd)],
                    stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=None,text=True,bufsize=1)
                wait_agent_ready(remote)
                time.sleep(2)
                command = ['python3','-u',str(Path(__file__).resolve()), 'client',
                           '--mode',mode,'--duration',str(args.duration),'--repeats',str(args.repeats),
                           '--profiles',','.join(profiles),'--directions',','.join(dirs),'--jsonl',str(jsonl)]
                result = subprocess.run(['bash','-c',build_shell_command(args,mode,'main',command)], check=False)
                if result.returncode:
                    raise RuntimeError(f'{mode} client exit code {result.returncode}')
            except (RuntimeError, OSError, KeyboardInterrupt) as exc:
                errors.append(f'{mode}: {exc}')
                print('ERROR:', errors[-1],flush=True)
                if isinstance(exc, KeyboardInterrupt):
                    raise
            finally:
                close_process(remote,'robot SSH agent',seconds=5)
                close_process(bridge,'main Bridge',seconds=3)
    finally:
        create_reports(folder, cfg)
    print('\n=== 완료 ===')
    print('결과:',folder)
    for file in sorted(folder.iterdir()):
        print(' -',file.name)
    if errors:
        print('일부 모드 실패:', ' | '.join(errors))
        return 1
    return 0


def flatten(row):
    d = {k:v for k,v in row.items() if not isinstance(v,(dict,list))}
    for parent, value in row.items():
        if isinstance(value,dict):
            for subkey, item in value.items():
                if isinstance(item,dict):
                    for last, val in item.items():
                        d[f'{parent}_{subkey}_{last}'] = val
                else:
                    d[f'{parent}_{subkey}'] = item
    return d


def strnum(v, n=2):
    return '-' if v is None else f'{v:.{n}f}'


def create_reports(folder, config):
    raw = folder/'raw_runs.jsonl'
    rows = []
    if raw.exists():
        for line in raw.read_text(encoding='utf-8').splitlines():
            if line.strip():
                try: rows.append(json.loads(line))
                except json.JSONDecodeError: pass
    if not rows:
        (folder/'REPORT.md').write_text('# 결과 없음\n통신 경로 및 로그를 확인하세요.\n',encoding='utf-8')
        return
    flat = [flatten(r) for r in rows]
    keys = list(dict.fromkeys(k for r in flat for k in r))
    with (folder/'all_runs.csv').open('w',newline='',encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f,fieldnames=keys)
        writer.writeheader()
        writer.writerows(flat)
    grouped = {}
    for r in rows:
        grouped.setdefault((r['profile'],r['direction'],r['backend']),[]).append(r)
    summary = []
    for (profile,direction,backend), items in sorted(grouped.items()):
        samples = lambda f: [val for val in (f(r) for r in items) if isinstance(val,(float,int))]
        avg = lambda f: round(statistics.mean(samples(f)),3) if samples(f) else None
        summary.append({
            'profile':profile,'direction':direction,'backend':backend,'runs':len(items),
            'rtt_avg_ms_mean':avg(lambda r:r['rtt_ms']['avg']),
            'rtt_trim5_ms_mean':avg(lambda r:r['rtt_ms']['trim5_avg']),
            'rtt_p95_ms_mean':avg(lambda r:r['rtt_ms']['p95']),
            'rtt_p99_ms_mean':avg(lambda r:r['rtt_ms']['p99']),
            'rtt_max_ms_max':max(samples(lambda r:r['rtt_ms']['max']),default=None),
            'rtt_run_avg_std_ms':round(statistics.pstdev(samples(lambda r:r['rtt_ms']['avg'])),3) if len(samples(lambda r:r['rtt_ms']['avg']))>1 else None,
            'loss_pct_mean':avg(lambda r:r['loss_pct']),
            'send_schedule_misses_mean':avg(lambda r:r['send_schedule_misses']),
            'received_hz_mean':avg(lambda r:r['actual_rx_hz']),
            'rx_gap_jitter_p95_ms_mean':avg(lambda r:r['receive_gap']['jitter_abs_ms']['p95']),
            'rx_gap_max_ms_max':max(samples(lambda r:r['receive_gap']['gap_ms']['max']),default=None),
            'pc_cpu_pct_one_core':avg(lambda r:r['pc_cpu']['app_cpu_pct_one_core']),
            'robot_cpu_pct_one_core':avg(lambda r:r['robot_cpu']['app_cpu_pct_one_core']),
            'robot_bridge_cpu_pct_one_core':avg(lambda r:r['robot_cpu']['bridge_cpu_pct_one_core']),
            'pc_bridge_cpu_pct_one_core':avg(lambda r:r['pc_cpu']['bridge_cpu_pct_one_core']),
        })
    with (folder/'comparison_summary.csv').open('w',newline='',encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f,fieldnames=list(summary[0]))
        writer.writeheader()
        writer.writerows(summary)
    lines = ['# ROS2 DDS / Zenoh 통신 벤치마크 결과','',
        f"- 측정조건: {config['duration']}초/조건, {config['repeats']}회 반복, 방향 {', '.join(config['directions'])}",
        '- 세 가지 방식: FastDDS 직접 / CycloneDDS 직접 / CycloneDDS+Zenoh Bridge+Router',
        '- 각 메시지는 실제 ROS2 타입을 사용하지만 페이로드는 합성 데이터(실제 센서, Nav2, FMS 로직 미포함)',
        '- RTT는 발신 호스트 단조 시계로 측정. 단방향 지연은 Chrony 동기 검증 없이 신뢰할 수 없어 요약에서 제외',
        '- RTT 5% 절사평균을 참고값으로 함께 제공. P95/P99/최대값은 제거하지 않은 원본 기준',
        '- 센서 QoS BEST_EFFORT, 나머지 RELIABLE. 모든 백엔드에서 동일한 타입/크기/주기를 사용',
        '- 과부하로 놓친 송신 스케줄은 send_schedule_misses로 기록되며 실제 송신 메시지 손실과 구분',
        '- 처리량은 메시지 페이로드 크기 근사로 계산(실제 네트워크 패킷 바이트와 다름)',
        '- CPU는 프로그램 실행 및 통신 Bridge/Router를 분리하여 측정하며 1코어=100% 기준',
        '- 서로 다른 RMW 및 네트워크 환경 차이를 통제하지 않으면 순수 프로토콜 성능 비교는 불가',
        '', '## 조건별 비교 (반복 측정 평균)','',
        '| 프로파일 | 방향 | 방식 | 횟수 | RTT 평균 ms | 절사평균 ms | P95 ms | P99 ms | 손실 % | 수신 Hz | Jitter P95 ms |',
        '|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|']
    for r in summary:
        lines.append('| '+ ' | '.join([r['profile'],r['direction'],r['backend'],str(r['runs']),
            strnum(r['rtt_avg_ms_mean']), strnum(r['rtt_trim5_ms_mean']),strnum(r['rtt_p95_ms_mean']),
            strnum(r['rtt_p99_ms_mean']),strnum(r['loss_pct_mean']),strnum(r['received_hz_mean']),
            strnum(r['rx_gap_jitter_p95_ms_mean'])])+' |')
    lines += ['', '## 결과 파일', '',
        '- `comparison_summary.csv`: 프로파일/방향/방식별 반복 평균 비교',
        '- `all_runs.csv`: 모든 반복 측정의 상세 통계 및 CPU (Excel 열기 가능)',
        '- `raw_runs.jsonl`: 원본 실행별 통계 데이터. 개별 패킷 타임스탬프는 저장하지 않음',
        '- `experiment_config.json`: 실험 설정과 실행 조건',
        '', '## 판단 시 주의사항', '',
        '1. 통신 지연이 한 번 크게 튀었다면 P99/최대값에서 그대로 분석해야 합니다. 절사평균만으로 결론 내리지 마세요.',
        '2. Python 콜백/직렬화/스케줄링이 포함된 종단 간 측정으로 Bridge 자체의 순수 네트워크 지연이 아닙니다.',
        '3. 단방향 수신시간은 양쪽 시계가 동기화됐고 오프셋이 검증된 경우에만 사용할 수 있습니다.',
        '4. 측정 중 백그라운드 FMS 작업, 네트워크 혼잡, Wi-Fi/LAN 변화는 비교 결과에 영향을 줍니다.',
        '5. 센서 메시지는 값이 고정된 합성 메시지로 실제 센서의 시간·압축·동적 분포를 대변하지 않습니다.']
    (folder/'REPORT.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    print('보고서 생성:',folder/'REPORT.md', flush=True)


def make_parser():
    parser = argparse.ArgumentParser(description='FastDDS vs CycloneDDS vs Zenoh ROS2 benchmark')
    sub = parser.add_subparsers(dest='cmd',required=True)
    def common(p):
        p.add_argument('--router-ip',default='192.168.0.16')
        p.add_argument('--router-port',type=int,default=7447)
    p = sub.add_parser('all', help='메인 PC: SSH로 로봇 구동 후 3모드 자동 실행')
    common(p)
    p.add_argument('--robot-user',default='turtlebot2')
    p.add_argument('--robot-ip',default='192.168.0.102')
    p.add_argument('--robot-file',default=None,help='로봇에 저장된 .py 파일 경로. 기본 ~/communication_test/ros_multi_bench.py')
    p.add_argument('--main-setup',default=None,help='메인 PC에 ROS2 overlay setup.bash가 추가로 필요한 경우')
    p.add_argument('--robot-setup',default=None,help='로봇 ROS2 overlay setup.bash가 추가로 필요한 경우 (원격 경로)')
    p.add_argument('--main-domain',type=int,default=67)
    p.add_argument('--robot-zenoh-domain',type=int,default=68)
    p.add_argument('--modes',default='all',help='all 또는 fastdds,cyclone,zenoh')
    p.add_argument('--profiles',default='all',help='all(저부하 기본: 고용량 2개 제외)/core/sensor/heavy 또는 목록')
    p.add_argument('--directions',default='both',help='both / pc_to_robot / robot_to_pc')
    p.add_argument('--duration',type=float,default=60)
    p.add_argument('--repeats',type=int,default=1)
    p.add_argument('--output',default='~/ros_bench_results')
    p.add_argument('--dry-run',action='store_true')
    p = sub.add_parser('agent',help='로봇 SSH agent (일반적으로 자동 실행)')
    common(p)
    p.add_argument('--mode',choices=list(MODES),required=True)
    p = sub.add_parser('client',help='메인 내부 실행용')
    p.add_argument('--mode',choices=list(MODES),required=True)
    p.add_argument('--duration',type=float,required=True)
    p.add_argument('--repeats',type=int,required=True)
    p.add_argument('--profiles',required=True)
    p.add_argument('--directions',required=True)
    p.add_argument('--jsonl',required=True)
    return parser


def main():
    parser = make_parser()
    args = parser.parse_args()
    try:
        if args.cmd == 'all':
            if args.duration <= 0 or args.repeats <= 0:
                parser.error('duration과 repeats는 0보다 커야 합니다')
            return control_main(args)
        if args.cmd == 'agent':
            remote_agent(args)
        elif args.cmd == 'client':
            local_client(args)
        return 0
    except (RuntimeError, ValueError) as exc:
        print('ERROR:', exc, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('사용자 중단: 수집된 결과는 보존되며 중간 보고서를 생성했습니다.', file=sys.stderr)
        return 130


if __name__ == '__main__':
    sys.exit(main())
