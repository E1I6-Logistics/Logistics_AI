"""실제 FMS 종료 없이 독립 stop/start 실행 순서와 웹 계약을 검증한다."""
import fcntl
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from backend.app.services import fms_restart as restart
from backend.app.routers import system as api
from backend.app.services import omx_maintenance
from tests.test_omx_status import configured, setup


@pytest.fixture
def files(tmp_path, monkeypatch):
    root = tmp_path / 'fms'
    root.mkdir()
    logs = root / 'logs'; logs.mkdir()
    for name in ('stop_fms.sh', 'start_fms.sh'):
        (root / name).write_text('#!/bin/bash\nexit 0\n')
    (logs / 'backend.pid').write_text(str(os.getpid()))
    monkeypatch.setattr(restart, 'BASE_DIR', root)
    monkeypatch.setenv('FMS_LOG_DIR', str(logs))
    return root, logs


def test_status_requires_launcher_and_live_instance(files):
    root, logs = files
    assert restart.status()['available']
    (logs / 'backend.pid').write_text('99999999')
    assert not restart.status()['available']
    (logs / 'backend.pid').unlink()
    assert not restart.status()['available']


@pytest.mark.parametrize('record', ['[]', 'broken', '{"at":null}', '{"at":0}'])
def test_bad_or_stale_status_is_ignored(files, record):
    _, logs = files
    (logs / 'web_restart.json').write_text(record)
    assert restart.status()['restart'] == {}


def test_request_detaches_and_passes_lock_without_executing_stack(files, monkeypatch):
    root, logs = files
    def launch(command, **options):
        assert command[:3] == [restart.sys.executable, '-m', 'backend.app.services.fms_restart']
        assert options['start_new_session'] and options['cwd'] == root
        assert options['stdin'] == subprocess.DEVNULL
        assert options['pass_fds']
        # 연속 요청은 파일 잠금으로 거절한다.
        with pytest.raises(HTTPException) as e: restart.request_restart()
        assert e.value.status_code == 409
        return SimpleNamespace(wait=lambda timeout: 0)
    monkeypatch.setattr(restart.subprocess, 'Popen', launch)
    response = restart.request_restart()
    assert response['accepted']
    assert restart.status()['restarting']
    assert json.loads((logs / 'web_restart.json').read_text())['token'] == response['token']
    # 요청자 쪽 FD는 닫혔으며 테스트에서는 실제 worker가 없으므로 잠금도 남지 않는다.
    with (logs / 'web_restart.lock').open('r+') as file:
        fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_launcher_failure_is_reported_and_lock_released(files, monkeypatch):
    _, logs = files
    monkeypatch.setattr(restart.subprocess, 'Popen', Mock(side_effect=OSError('missing python')))
    with pytest.raises(HTTPException) as error: restart.request_restart()
    assert error.value.status_code == 503
    assert restart.status()['restart']['phase'] == 'FAILED'
    with (logs / 'web_restart.lock').open('r+') as file:
        fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_worker_runs_stop_then_start_in_temporary_fixture_only(files):
    root, logs = files
    events = root / 'events'
    (root / 'stop_fms.sh').write_text('printf "stop:%s\\n" "$FMS_KEEP_TRANSPORT" >> events\n')
    (root / 'start_fms.sh').write_text('printf "start:%s\\n" "$FMS_CLEAN_START" >> events\n')
    restart.worker(root, logs, 'test-token', 'old', delay=0)
    assert events.read_text().splitlines() == ['stop:1', 'start:0']
    assert restart.status()['restart']['phase'] == 'COMPLETE'


@pytest.mark.parametrize('failing', ['stop_fms.sh', 'start_fms.sh'])
def test_worker_failure_runs_cleanup_and_keeps_failure_evidence(files, failing):
    root, logs = files
    (root / failing).write_text('exit 7\n')
    restart.worker(root, logs, 'test-token', 'old', delay=0)
    record = restart.status()['restart']
    assert record['phase'] == 'FAILED' and failing in record['message']


def test_script_timeout_terminates_its_own_process_group(files, monkeypatch):
    root, _ = files
    child = Mock(pid=12345)
    child.wait.side_effect = [subprocess.TimeoutExpired('fixture', 1), 0]
    monkeypatch.setattr(restart.subprocess, 'Popen', Mock(return_value=child))
    kill = Mock(); monkeypatch.setattr(restart.os, 'killpg', kill)
    with pytest.raises(RuntimeError, match='시간 초과'):
        restart.run_script(root, 'start_fms.sh', 1)
    kill.assert_called_once_with(12345, restart.signal.SIGTERM)


def test_http_restart_has_no_order_or_mode_gate(files, monkeypatch):
    request = Mock(return_value={'accepted': True, 'token': 'token', 'previous_instance': 'old'})
    monkeypatch.setattr(restart, 'request_restart', request)
    app = FastAPI(); app.include_router(api.router)
    with TestClient(app) as client:
        assert client.get('/api/system/web').status_code == 200
        assert client.post('/api/system/web/restart', json={'confirm': False}).status_code == 400
        assert client.post('/api/system/web/restart', json={'confirm': 'true'}).status_code == 422
        assert client.post('/api/system/web/restart', json={'confirm': True, 'command': 'anything'}).status_code == 422
        response = client.post('/api/system/web/restart', json={'confirm': True})
        assert response.status_code == 202 and response.json()['accepted']
    request.assert_called_once()


def test_explicit_omx_ssh_identity(configured, tmp_path, monkeypatch):
    manager, _ = configured
    key = tmp_path / 'test-key'; key.write_text('fixture-only')
    monkeypatch.setenv('FMS_OMX_SSH_IDENTITY', str(key))
    run = Mock(return_value=SimpleNamespace(returncode=0))
    monkeypatch.setattr(omx_maintenance.subprocess, 'run', run)
    assert omx_maintenance.restart_service(manager, 'omx1')['accepted']
    assert run.call_args.args[0][1:5] == ['-i', str(key), '-o', 'IdentitiesOnly=yes']


def test_missing_omx_key_never_executes(configured, tmp_path, monkeypatch):
    manager, _ = configured
    monkeypatch.setenv('FMS_OMX_SSH_IDENTITY', str(tmp_path / 'missing'))
    run = Mock(); monkeypatch.setattr(omx_maintenance.subprocess, 'run', run)
    with pytest.raises(HTTPException) as error: omx_maintenance.restart_service(manager, 'omx1')
    assert error.value.status_code == 503
    run.assert_not_called()
    assert manager._maintenance_devices == set()


def test_detached_worker_survives_launcher_and_releases_lock(files):
    import sys
    import time
    root, logs = files
    (root / 'stop_fms.sh').write_text('printf "stop:%s\\n" "$PPID" >> events\n')
    (root / 'start_fms.sh').write_text('printf "start:%s\\n" "$PPID" >> events\n')
    descriptor = os.open(logs / 'web_restart.lock', os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    code = """
import sys
from pathlib import Path
from backend.app.services import fms_restart as r
r.BASE_DIR=Path(sys.argv[1])
folder=r.BASE_DIR/'logs'
r.directory=lambda:folder
sys.argv=['fixture','--worker','--lock-fd',sys.argv[2],'--token','detached-fixture','--previous-instance','old']
r.main()
"""
    with (logs / 'fixture.log').open('w') as output:
        launcher = subprocess.Popen([sys.executable, '-c', code, str(root), str(descriptor)],
            cwd=Path(__file__).resolve().parents[1], pass_fds=(descriptor,),
            stdin=subprocess.DEVNULL, stdout=output, stderr=output, start_new_session=True)
        assert launcher.wait(timeout=5) == 0
    os.close(descriptor)
    # API 실행기는 종료됐지만 별도 worker가 잠금을 유지한다.
    with (logs / 'web_restart.lock').open('r+') as file:
        with pytest.raises(BlockingIOError):
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        record = restart.status()['restart']
        if record.get('phase') in ('COMPLETE', 'FAILED'): break
        time.sleep(.05)
    assert record['phase'] == 'COMPLETE', (logs / 'fixture.log').read_text()
    events = (root / 'events').read_text().splitlines()
    assert [value.split(':')[0] for value in events] == ['stop', 'start']
    assert all(int(value.split(':')[1]) != launcher.pid for value in events)
    with (logs / 'web_restart.lock').open('r+') as file:
        fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_restart_reloads_current_env_without_shell_execution(files, monkeypatch):
    root, _ = files
    monkeypatch.setenv('FMS_OMX_SSH_TARGETS', '{}')
    (root / '.env').write_text('FMS_OMX_SSH_TARGETS={"omx1":"omx01@192.168.0.151"}\nFMS_CLEAN_START=1\n# comment\nnot-valid=$(do-not-execute)\n')
    result = restart.launch_environment(root)
    assert result['FMS_OMX_SSH_TARGETS'] == '{"omx1":"omx01@192.168.0.151"}'
    assert result['FMS_CLEAN_START'] == '0'
    assert result['FMS_KEEP_TRANSPORT'] == '1'
    assert 'not-valid' not in result
