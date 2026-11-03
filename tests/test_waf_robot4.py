"""WAF는 전용 관측만 수신하며 일반 Fleet/경로/속도 명령을 사용하지 않는다."""
from concurrent.futures import Future
from types import SimpleNamespace as NS
from unittest.mock import Mock
from uuid import uuid4
import pytest
from backend.app.services.waf_telemetry import WafTelemetry
from backend.app.services.pallet_replenishment import PalletReplenishment, ReplenishmentError
from backend.app.ros2.waf_client import WafActionTransport
from backend.app.services.zenoh_connection_manager import ZenohConnectionManager


def pose(x=1., y=2., frame='map'):
    return NS(header=NS(frame_id=frame), pose=NS(position=NS(x=x,y=y), orientation=NS(x=0.,y=0.,z=0.,w=1.)))


def test_discovery_and_independent_telemetry_freshness():
    clock=[100.];t=WafTelemetry(clock=lambda:clock[0])
    assert not t.snapshot()['seen'] and t.snapshot()['battery'] is None
    t.sync_connections([dict(name='robot1',connected=True),dict(name='robot4',connected=True,ip='192.168.0.44')])
    t.observe_battery(NS(percentage=.75,voltage=12.3));t.observe_pose(pose(),'/robot4/amcl_pose')
    row=t.snapshot();assert row['robot_id']=='robot4' and row['connected'] and row['seen']
    assert row['battery']==75 and row['battery_voltage']==12.3
    assert row['pose']['x']==1 and row['pose']['frame_id']=='map'
    clock[0]+=6
    row=t.snapshot();assert row['connection_state']=='UNKNOWN' and not row['pose_fresh'] and row['pose'] is None
    assert row['battery']==75  # 배터리와 위치 주기는 분리한다.
    t.sync_connections([]);assert t.snapshot()['connection_state']=='DISCONNECTED' and t.snapshot()['seen']
    clock[0]+=25
    assert t.snapshot()['battery'] is None


def test_missing_or_invalid_battery_never_becomes_fake_percentage():
    t=WafTelemetry()
    t.observe_battery(NS(percentage=-1.,voltage=12.4))
    assert t.snapshot()['battery'] is None and t.snapshot()['battery_voltage']==12.4
    t.observe_battery(NS(percentage=float('nan'),voltage=float('inf')))
    assert t.snapshot()['battery'] is None and t.snapshot()['battery_voltage'] is None
    t.observe_pose(pose(x=float('nan')),'bad')
    assert t.snapshot()['pose'] is None


def test_external_action_status_and_raw_feedback_are_not_inferred_from_pose(tmp_path,monkeypatch):
    t=WafTelemetry();t.sync_connections([dict(name='robot4',connected=True)])
    t.observe_action_status(NS(status_list=[NS(status=2)]),'/robot4/pallet_replace')
    t.observe_feedback(NS(feedback=NS(stage='ALIGN_2')),'/robot4/pallet_replace')
    service=PalletReplenishment(tmp_path/'waf.json',telemetry=t)
    client=Mock(server_is_ready=lambda:True,action_name='/robot4/pallet_replace');service.attach(client,NS)
    row=service.snapshot()
    assert row['robot']['activity']=='RUNNING' and row['robot']['feedback']['stage']=='ALIGN_2'
    assert row['busy'] and row['job'] is None
    with pytest.raises(ReplenishmentError):service.start('A',str(uuid4()))
    client.send_goal_async.assert_not_called()
    t.observe_action_status(NS(status_list=[NS(status=4)]),'/robot4/pallet_replace')
    assert not service.snapshot()['busy'] and service.snapshot()['robot']['activity']=='READY'


def test_robot4_action_address_preferred_and_legacy_fallback_keeps_goal_handle():
    native=Mock(server_is_ready=lambda:False)
    legacy=Mock(server_is_ready=lambda:True)
    transport=WafActionTransport([('/robot4/pallet_replace',native),('/pallet_replace',legacy)])
    assert transport.server_is_ready() and transport.action_name=='/pallet_replace'
    transport.send_goal_async(NS(destination=1));legacy.send_goal_async.assert_called_once()
    native.server_is_ready=lambda:True
    assert transport.server_is_ready() and transport.action_name=='/robot4/pallet_replace'
    transport.send_goal_async(NS(destination=4));native.send_goal_async.assert_called_once()


def test_zenoh_robot4_namespace_is_discovered_without_fixed_three_robot_limit(monkeypatch):
    z=ZenohConnectionManager()
    def get(path):
        if path.endswith('/router'):
            return [dict(value=dict(sessions=[dict(peer='waf',links=[dict(dst='tcp/192.168.0.44:7447')])]))]
        return [dict(key='@/waf/ros2/route/topic/pub/robot4/battery_state')]
    monkeypatch.setattr(z,'_get_json',get)
    assert z.connections()[0]['name']=='robot4'



def test_external_goal_does_not_make_old_completed_handle_cancellable(tmp_path):
    t=WafTelemetry();service=PalletReplenishment(tmp_path/'waf.json',telemetry=t)
    service.attach(Mock(server_is_ready=lambda:True, action_name='/robot4/pallet_replace'),NS)
    service.job=dict(state='SUCCEEDED',item='A',request_id='previous')
    service.goal_handle=Mock()
    t.observe_action_status(NS(status_list=[NS(status=2)]),'/robot4/pallet_replace')
    row=service.snapshot()
    assert row['busy'] and row['robot']['activity']=='RUNNING'
    assert not row['can_cancel']
