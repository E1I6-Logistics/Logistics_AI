"""Startup configuration must not synthesize robot data or send goals."""
import json
import os
import subprocess
import sys
import pytest

@pytest.mark.parametrize('values,expected', [({}, ['real','ai']), ({'FMS_ROBOT_MODE':'simulation','FMS_OPERATION_MODE':'algorithm'}, ['simulation','algorithm']), ({'FMS_ROBOT_MODE':'real','FMS_OPERATION_MODE':'algorithm'}, ['real','algorithm'])])
def test_startup_mode_defaults_and_explicit_override(values,expected):
    env={k:v for k,v in os.environ.items() if k not in ('FMS_ROBOT_MODE','FMS_OPERATION_MODE')}
    env.update(values)
    code='import json; from backend.app.services.mode_service import mode_manager as m; print(json.dumps([m.mode,m.operation_mode]))'
    p=subprocess.run([sys.executable,'-c',code],env=env,capture_output=True,text=True,check=True)
    assert json.loads(p.stdout)==expected

def test_invalid_operation_mode_is_rejected():
    env=dict(os.environ,FMS_OPERATION_MODE='unknown')
    p=subprocess.run([sys.executable,'-c','import backend.app.config'],env=env,capture_output=True,text=True)
    assert p.returncode and 'FMS_OPERATION_MODE must be' in p.stderr
