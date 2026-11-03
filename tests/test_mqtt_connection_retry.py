"""FMS starts its listener while the broker is absent; no ROS/hardware actions."""
from unittest.mock import Mock
from backend.app.services.mqtt_manager import MQTTManager


def test_start_does_not_require_broker_to_be_up(monkeypatch):
    manager=MQTTManager();manager.client=Mock()
    thread=Mock();monkeypatch.setattr('backend.app.services.mqtt_manager.threading.Thread',lambda **kwargs:thread)
    manager.start()
    manager.client.connect.assert_not_called()
    manager.client.connect_async.assert_called_once_with(manager.broker_ip,manager.broker_port,60)
    manager.client.loop_start.assert_called_once()
    assert manager.running
    manager.stop()


def test_reconnect_invalidates_old_docking_evidence():
    manager=MQTTManager()
    manager._on_connect(Mock(),None,{},0)
    manager._on_dock_message(None,None,Mock(topic='dock/station/1/status',payload=b'{"station":1,"status":1}',retain=False,dup=False))
    assert manager.get_dock_snapshot(1)['trusted']
    manager._on_connect_fail(None,None)
    assert not manager.get_dock_snapshot(1)['trusted']
