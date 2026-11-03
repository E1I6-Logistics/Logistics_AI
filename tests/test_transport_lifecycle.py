"""Real stop script with fake tools; never stop host processes or Docker."""
import os
from pathlib import Path
import shutil
import subprocess
import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('keep', ['0', '1'])
def test_stop_preserves_shared_broker_only_when_requested(tmp_path, keep):
    shutil.copy(ROOT / 'stop_fms.sh', tmp_path / 'stop_fms.sh')
    (tmp_path / 'infra').mkdir()
    (tmp_path / 'infra/docker-compose.yml').write_text('services: {}')
    tools = tmp_path / 'bin'
    tools.mkdir()
    for name in ('ros2', 'pgrep', 'docker'):
        script = '#!/bin/bash\n'
        if name == 'docker':
            script += 'printf "%s\\n" "$*" >> "$TEST_DOCKER_LOG"\n[[ "$1" == container ]] && exit 1\nexit 0\n'
        elif name == 'pgrep':
            script += 'exit 1\n'
        else:
            script += 'exit 0\n'
        p = tools / name
        p.write_text(script)
        p.chmod(0o755)
    log = tmp_path / 'docker-events'
    env = dict(os.environ, PATH=str(tools) + ':' + os.environ['PATH'], FMS_KEEP_TRANSPORT=keep,
               FMS_ROS_SETUP=str(tmp_path / 'missing'), TEST_DOCKER_LOG=str(log),
               FMS_LOG_DIR=str(tmp_path / 'logs'))
    subprocess.run(['bash', str(tmp_path / 'stop_fms.sh')], env=env, capture_output=True,
                   text=True, check=True, timeout=10)
    events = log.read_text() if log.exists() else ''
    assert ('stop zenoh-router mqtt-broker' in events) == (keep == '0')
