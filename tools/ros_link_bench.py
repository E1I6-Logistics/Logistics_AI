#!/usr/bin/env python3
"""ROS 2 DDS direct vs Zenoh Bridge ROS 2 path benchmark (ROS 2 Jazzy).

One file is run on each machine:
  Robot: python3 ros_link_bench.py server
  PC:    python3 ros_link_bench.py client --mode dds --suite quick

ROS 2 environment and network route are configured *outside* this script.
"""

import argparse
import csv
import json
import os
from pathlib import Path
import statistics
import time
import uuid

try:
    import psutil
except ImportError:
    psutil = None


def cpu_seconds(t):
    return float(t.user + t.system)


def cpu_percent(cpu_delta, elapsed):
    # Percentage of ONE CPU core, so 100% means one fully utilized core.
    return round(100.0 * max(0.0, cpu_delta) / elapsed, 2) if elapsed > 0 else None


def system_cpu_percent(before, after):
    if before is None or after is None:
        return None
    idle = (after.idle + getattr(after, 'iowait', 0.0)) - (before.idle + getattr(before, 'iowait', 0.0))
    total = sum(after) - sum(before)
    return round(100.0 * (1.0 - idle / total), 2) if total > 0 else None


class CpuSnapshot:
    """Read process CPU times twice, avoiding ongoing sampling overhead."""

    def __init__(self):
        self.when = time.monotonic()
        self.app_cpu = time.process_time()
        self.system = psutil.cpu_times() if psutil else None
        self.services = {}
        if psutil:
            for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
                try:
                    name = (proc.info['name'] or '').lower()
                    cmd = ' '.join(proc.info['cmdline'] or []).lower()
                    if 'zenoh-bridge-ros2dds' in name or 'zenoh-bridge-ros2dds' in cmd:
                        kind = 'bridge'
                    elif name == 'zenohd':
                        kind = 'router'
                    else:
                        continue
                    self.services[proc.pid] = (kind, cpu_seconds(proc.cpu_times()))
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue

    def finish(self):
        elapsed = max(time.monotonic() - self.when, 1e-9)
        result = {
            'app_cpu_pct_core': cpu_percent(time.process_time() - self.app_cpu, elapsed),
            'system_cpu_pct': system_cpu_percent(self.system, psutil.cpu_times()) if psutil else None,
            'bridge_cpu_pct_core': None,
            'router_cpu_pct_core': None,
        }
        if psutil:
            total = {'bridge': 0.0, 'router': 0.0}
            found = {'bridge': False, 'router': False}
            for pid, (kind, start) in self.services.items():
                try:
                    total[kind] += max(0.0, cpu_seconds(psutil.Process(pid).cpu_times()) - start)
                    found[kind] = True
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            for kind in ('bridge', 'router'):
                if found[kind]:
                    result[f'{kind}_cpu_pct_core'] = cpu_percent(total[kind], elapsed)
        return result


def make_data(run_id, seq, timestamp_ns, size):
    header = f'D|{run_id}|{seq}|{timestamp_ns}|'
    if len(header) > size:
        raise ValueError('Payload size is smaller than the header')
    return header + 'X' * (size - len(header))


def p95(numbers):
    if not numbers:
        return None
    values = sorted(numbers)
    return round(values[max(0, (95 * len(values) + 99) // 100 - 1)], 3)


def fmt(value, places=2):
    return '-' if value is None else f'{value:.{places}f}'


def normalized_topic(prefix, name):
    return '/' + '/'.join([part for part in (prefix + '/' + name).split('/') if part])


def save_csv(path, row):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists() or path.stat().st_size == 0
    with path.open('a', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(row.keys()))
        if new:
            writer.writeheader()
        writer.writerow(row)


def launch_ros(args):
    try:
        import rclpy
        from rclpy.node import Node
        from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy, HistoryPolicy
        from std_msgs.msg import String
    except ImportError as e:
        raise SystemExit('ROS 2 Python 환경이 없습니다. source /opt/ros/jazzy/setup.bash 실행 후 재시도하세요.\n' + str(e))

    qos = QoSProfile(
        history=HistoryPolicy.KEEP_LAST,
        depth=1000,
        reliability=ReliabilityPolicy.RELIABLE,
        durability=DurabilityPolicy.VOLATILE,
    )

    req = normalized_topic(args.prefix, 'request')
    rep = normalized_topic(args.prefix, 'reply')

    class Server(Node):
        def __init__(self):
            super().__init__('ros_link_bench_server')
            self.pub = self.create_publisher(String, rep, qos)
            self.sub = self.create_subscription(String, req, self.on_message, qos)
            self.run_id = None
            self.seen = set()
            self.cpu_snapshot = None
            self.finished = None

        def publish(self, payload):
            m = String()
            m.data = payload
            self.pub.publish(m)

        def on_message(self, msg):
            data = msg.data
            if data.startswith('!BEGIN|'):
                rid = data.split('|', 1)[1]
                if self.run_id != rid:
                    self.run_id = rid
                    self.seen.clear()
                    self.cpu_snapshot = CpuSnapshot()
                    self.finished = None
                self.publish('!READY|' + rid)
            elif data.startswith('!END|'):
                rid = data.split('|', 1)[1]
                if rid == self.run_id:
                    if self.finished is None:
                        stats = self.cpu_snapshot.finish()
                        stats['unique_rx'] = len(self.seen)
                        self.finished = '!RESULT|' + rid + '|' + json.dumps(stats, separators=(',', ':'))
                    self.publish(self.finished)
                    print(f'Robot received {len(self.seen)} unique messages; {self.finished.split("|", 2)[2]}', flush=True)
            elif data.startswith('D|'):
                parts = data.split('|', 4)
                if len(parts) < 5 or parts[1] != self.run_id:
                    return
                try:
                    self.seen.add(int(parts[2]))
                except ValueError:
                    return
                self.pub.publish(msg)  # Echo identical ROS2 message.

    class Client(Node):
        def __init__(self):
            super().__init__('ros_link_bench_client')
            self.pub = self.create_publisher(String, req, qos)
            self.sub = self.create_subscription(String, rep, self.on_message, qos)
            self.run_id = None
            self.ready = False
            self.result = None
            self.arrived = {}  # seq -> (arrival_monotonic_ns, roundtrip_ms)

        def publish(self, payload):
            m = String()
            m.data = payload
            self.pub.publish(m)

        def on_message(self, msg):
            data = msg.data
            if data.startswith('!READY|'):
                if data.split('|', 1)[1] == self.run_id:
                    self.ready = True
            elif data.startswith('!RESULT|'):
                parts = data.split('|', 2)
                if len(parts) == 3 and parts[1] == self.run_id:
                    try:
                        self.result = json.loads(parts[2])
                    except json.JSONDecodeError:
                        pass
            elif data.startswith('D|'):
                parts = data.split('|', 4)
                if len(parts) != 5 or parts[1] != self.run_id:
                    return
                try:
                    seq, sent_ns = int(parts[2]), int(parts[3])
                except ValueError:
                    return
                if seq not in self.arrived:
                    now = time.monotonic_ns()
                    self.arrived[seq] = (now, (now - sent_ns) / 1e6)

        def spin(self, seconds):
            rclpy.spin_once(self, timeout_sec=max(0.0, seconds))

        def begin(self):
            self.run_id = uuid.uuid4().hex[:12]
            self.arrived.clear()
            self.ready = False
            self.result = None
            deadline = time.monotonic() + 12.0
            next_send = 0.0
            while not self.ready and time.monotonic() < deadline:
                if time.monotonic() >= next_send:
                    self.publish('!BEGIN|' + self.run_id)
                    next_send = time.monotonic() + 0.5
                self.spin(0.05)
            if not self.ready:
                raise RuntimeError('서버 응답 없음: ROS2 topic / Zenoh Bridge / namespace / allow 필터를 검사하세요.')

        def end(self):
            deadline = time.monotonic() + 5.0
            next_send = 0.0
            while self.result is None and time.monotonic() < deadline:
                if time.monotonic() >= next_send:
                    self.publish('!END|' + self.run_id)
                    next_send = time.monotonic() + 0.5
                self.spin(0.05)
            return self.result or {}

        def run_case(self, mode, name, size, hz, count, outfile):
            print(f'[{mode}] {name}: {size}B x {count}, {hz}Hz', flush=True)
            self.begin()
            pc_cpu = CpuSnapshot()
            period_ns = round(1e9 / hz)
            next_send_ns = time.monotonic_ns()
            timestamps = []
            for seq in range(count):
                while True:
                    remaining_ns = next_send_ns - time.monotonic_ns()
                    if remaining_ns <= 0:
                        break
                    self.spin(min(0.03, remaining_ns / 1e9))
                sent_ns = time.monotonic_ns()
                self.publish(make_data(self.run_id, seq, sent_ns, size))
                timestamps.append(sent_ns)
                next_send_ns += period_ns
            # Let already-sent requests finish and record late echoes separately.
            deadline = time.monotonic() + 2.0
            while len(self.arrived) < count and time.monotonic() < deadline:
                self.spin(0.02)
            pc_stats = pc_cpu.finish()
            robot_stats = self.end()

            duration = max((timestamps[-1] - timestamps[0]) / 1e9 + 1.0 / hz, 1e-9)
            arrived = len(self.arrived)
            latencies = [v[1] for v in self.arrived.values()]
            receive_timestamps = sorted(v[0] for v in self.arrived.values())
            observed_rx_hz = ((arrived - 1) * 1e9 / (receive_timestamps[-1] - receive_timestamps[0])
                              if arrived >= 2 and receive_timestamps[-1] > receive_timestamps[0] else None)
            row = {
                'mode': mode, 'test': name, 'bytes': size, 'target_hz': hz, 'sent': count,
                'pc_received': arrived, 'robot_received': robot_stats.get('unique_rx'),
                'tx_actual_hz': round(count / duration, 2),
                'rx_observed_hz': round(observed_rx_hz, 2) if observed_rx_hz is not None else None,
                'missing_reply_pct': round(100 * (count - arrived) / count, 3),
                'rtt_avg_ms': round(statistics.mean(latencies), 3) if latencies else None,
                'rtt_p95_ms': p95(latencies),
                'app_echo_MiBps': round(arrived * size / duration / (1024 ** 2), 4),
                'pc_app_cpu_pct_core': pc_stats.get('app_cpu_pct_core'),
                'pc_bridge_cpu_pct_core': pc_stats.get('bridge_cpu_pct_core'),
                'pc_router_cpu_pct_core': pc_stats.get('router_cpu_pct_core'),
                'pc_system_cpu_pct': pc_stats.get('system_cpu_pct'),
                'robot_app_cpu_pct_core': robot_stats.get('app_cpu_pct_core'),
                'robot_bridge_cpu_pct_core': robot_stats.get('bridge_cpu_pct_core'),
                'robot_system_cpu_pct': robot_stats.get('system_cpu_pct'),
            }
            save_csv(outfile, row)
            print(f"  RTT 평균/P95: {fmt(row['rtt_avg_ms'], 3)} / {fmt(row['rtt_p95_ms'], 3)} ms"
                  f" | 실제 TX/RX: {fmt(row['tx_actual_hz'])} / {fmt(row['rx_observed_hz'])} Hz"
                  f" | 왕복 누락: {fmt(row['missing_reply_pct'], 3)}%"
                  f" | 응답 처리량: {fmt(row['app_echo_MiBps'], 3)} MiB/s", flush=True)
            print(f"  CPU(1코어 기준): PC 앱 {fmt(row['pc_app_cpu_pct_core'])}%"
                  f" / 로봇 앱 {fmt(row['robot_app_cpu_pct_core'])}%"
                  f" / PC Bridge {fmt(row['pc_bridge_cpu_pct_core'])}%"
                  f" / 로봇 Bridge {fmt(row['robot_bridge_cpu_pct_core'])}%", flush=True)
            if not robot_stats:
                print('  경고: 로봇 측 최종 통계가 도착하지 않았습니다.', flush=True)
            # Prevent data belonging to one test from affecting a subsequent case.
            self.spin(0.2)
            return row

        def watch_recovery(self, mode, seconds, outfile):
            # This reports application-level echo outages, NOT isolated network reconnection latency.
            self.begin()
            print('10Hz로 왕복 응답 감시 시작. 중간에 통신 경로를 수동으로 중단/복구하세요.')
            print('SSH로 접속 중이라면 네트워크 인터페이스를 끄지 마세요.')
            next_send = time.monotonic()
            start = next_send
            seq = 0
            last_received_ns = None
            was_missing = False
            losses = []
            seen_sequences = set()
            while time.monotonic() - start < seconds:
                now = time.monotonic()
                if now >= next_send:
                    self.publish(make_data(self.run_id, seq, time.monotonic_ns(), 100))
                    seq += 1
                    next_send += 0.1
                self.spin(0.02)
                current = set(self.arrived)
                fresh = current - seen_sequences
                if fresh:
                    latest_ns = max(self.arrived[k][0] for k in fresh)
                    if was_missing and last_received_ns is not None:
                        gap = (latest_ns - last_received_ns) / 1e9
                        print(f'응답 복귀, 마지막 정상 응답부터 {gap:.3f}초 공백')
                        losses.append(round(gap, 3))
                        was_missing = False
                    last_received_ns = latest_ns
                    seen_sequences = current
                if (last_received_ns is not None and not was_missing and
                    (time.monotonic_ns() - last_received_ns) / 1e9 > 1.0):
                    was_missing = True
                    print('1초 이상 응답 없음: 통신 중단 추정')
            self.end()
            row = {
                'mode': mode, 'test': 'manual_recovery_watch', 'seconds': seconds,
                'tx_count': seq, 'reply_count': len(self.arrived),
                'outage_count': len(losses), 'max_observed_reply_gap_s': max(losses) if losses else None,
                'reply_gaps_s': ';'.join(map(str, losses)),
            }
            # Separate CSV schema from the ordinary suite.
            path = Path(outfile)
            watch_path = str(path.with_name(path.stem + '_recovery' + path.suffix))
            save_csv(watch_path, row)
            print('복구 감시 저장:', watch_path)

    rclpy.init()
    node = Server() if args.role == 'server' else Client()
    try:
        if args.role == 'server':
            print('로봇 Echo 서버 시작. request:', req, '| reply:', rep, flush=True)
            rclpy.spin(node)
        else:
            if args.recovery is not None:
                node.watch_recovery(args.mode, args.recovery, args.out)
            else:
                # Equal test conditions: 100B RTT, requested rate, and 1/100 KiB payloads.
                cases = [
                    ('rtt_100B', 100, 50, 1000),
                    ('rate_10Hz', 100, 10, 100),
                    ('rate_50Hz', 100, 50, 500),
                    ('rate_100Hz', 100, 100, 1000),
                    ('payload_1KiB', 1024, 10, 100),
                    ('payload_100KiB', 102400, 10, 100),
                ]
                if args.suite == 'quick':
                    cases = [cases[i] for i in (0, 2, 3, 5)]
                if args.suite == 'smoke':
                    cases = [('smoke_100B', 100, 10, 20)]
                if args.suite == 'cpu':
                    cases = [('cpu_50Hz_60s', 100, 50, 3000)]
                for name, size, hz, count in cases:
                    node.run_case(args.mode, name, size, hz, count, args.out)
                print('결과 CSV:', Path(args.out).resolve())
    except KeyboardInterrupt:
        print('\n중단됨')
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


def parse_args():
    ap = argparse.ArgumentParser(description='ROS2 DDS vs Zenoh performance measurement')
    ap.add_argument('role', choices=('server', 'client'))
    ap.add_argument('--mode', choices=('dds', 'zenoh'), default='dds',
                    help='CSV label ONLY. Network routing must be configured externally.')
    ap.add_argument('--prefix', default='/bench',
                    help='ROS 2 topic prefix visible to THIS host (e.g. /bench, /robot1/bench)')
    ap.add_argument('--suite', choices=('smoke', 'quick', 'full', 'cpu'), default='quick')
    ap.add_argument('--recovery', type=int, metavar='SECONDS', default=None,
                    help='Observe reply outage; manually interrupt/restart link during this period')
    ap.add_argument('--out', default='benchmark_results.csv')
    return ap.parse_args()


if __name__ == '__main__':
    launch_ros(parse_args())
