"""Pure Python tests, requiring no ROS2 installation."""
import json
from types import SimpleNamespace
from pathlib import Path
import tempfile
import unittest

import ros_multi_bench as b

class StatisticsTests(unittest.TestCase):
    def test_percentiles_and_outlier(self):
        vals = [1]*95 + [100]*5
        d = b.metrics(vals)
        self.assertEqual(d['n'],100)
        self.assertEqual(d['median'],1)
        self.assertEqual(d['trim5_avg'],1)
        self.assertEqual(d['max'],100)
        self.assertEqual(d['p99'],100)
        self.assertEqual(d['iqr_outliers'],5)

    def test_jitter_ideal_and_missed(self):
        d = b.gap_metrics([0, 10000000, 20000000, 50000000],100)
        self.assertEqual(d['gap_over_150pct'],1)
        self.assertEqual(d['jitter_abs_ms']['max'],20.0)

    def test_profiles_and_payload(self):
        self.assertEqual(len(b.PROFILES),10)
        self.assertEqual(b.payload_size(b.PROFILE_MAP['cloud100k']),1600128)
        self.assertEqual(b.payload_size(b.PROFILE_MAP['rgb_vga']),921728)
        self.assertEqual(b.select_profiles('all'),list(b.PROFILE_MAP))
        with self.assertRaises(ValueError):
            b.select_profiles('not-a-profile')

    def test_twist_metadata_roundtrip(self):
        p = b.PROFILE_MAP['twist']
        msg = SimpleNamespace(linear=SimpleNamespace(x=.1,y=0.0,z=0.0),
                              angular=SimpleNamespace(x=0.0,y=0.0))
        rid = '9a12345b6cde'
        b.encode_data(msg,p,rid,42)
        rid_out, seq, sent_wall = b.decode_data(msg,p)
        self.assertEqual((rid_out, seq),(rid,42))
        self.assertLess(abs(b.time_ns()-sent_wall),5_000_000_000)

    def test_header_metadata_roundtrip(self):
        p = b.PROFILE_MAP['cloud30k']
        msg = SimpleNamespace(header=SimpleNamespace(frame_id='',
                   stamp=SimpleNamespace(sec=0,nanosec=0)))
        rid = '001122334455'
        b.encode_data(msg,p,rid,789)
        rid_out, seq, sent_wall = b.decode_data(msg,p)
        self.assertEqual((rid_out,seq),(rid,789))
        self.assertGreater(sent_wall,1_000_000_000)

    def test_report_file_creation(self):
        template = {
            'timestamp':'2026-10-19T12:00:00', 'backend':'fastdds',
            'profile':'small', 'description':'100B', 'direction':'pc_to_robot', 'repeat':1,
            'duration_s':5, 'target_hz':50,'qos':'reliable','payload_bytes_approx':100,
            'expected_count':250, 'send_schedule_misses':0,
            'sent_count':250, 'received_count':250,'ack_count':250,'loss_pct':0,
            'ack_missing_pct':0, 'actual_rx_hz':50,'goodput_mbps_approx':.04,
            'rtt_ms': b.metrics([4,5,6]), 'receive_gap':b.gap_metrics([0,20000000,40000000],50),
            'raw_one_way_ms_unverified':b.metrics([2,3]),
            'pc_cpu':{'app_cpu_pct_one_core':2.4,'bridge_cpu_pct_one_core':None},
            'robot_cpu':{'app_cpu_pct_one_core':10,'bridge_cpu_pct_one_core':None}}
        with tempfile.TemporaryDirectory() as td:
            dest=Path(td)
            (dest/'raw_runs.jsonl').write_text(json.dumps(template)+'\n',encoding='utf-8')
            b.create_reports(dest,{'duration':5,'repeats':1,'directions':['pc_to_robot']})
            self.assertIn('FastDDS', (dest/'REPORT.md').read_text(encoding='utf-8'))
            with (dest/'comparison_summary.csv').open(encoding='utf-8-sig') as f:
                contents=f.read()
                self.assertIn('fastdds',contents)
                self.assertIn('rtt_avg_ms_mean',contents)
            self.assertTrue((dest/'all_runs.csv').exists())

if __name__ == '__main__':
    unittest.main(verbosity=2)
