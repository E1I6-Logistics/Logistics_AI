"""등록된 Pi의 사용자 OMX 서비스만 재시작. OS 재부팅·임의 명령은 지원하지 않는다."""
import json
import logging
import os
from pathlib import Path
import re
import subprocess

from fastapi import HTTPException

from .omx_stock import PALLETS

LOG = logging.getLogger(__name__)


def target_for(robot_id):
    try:
        targets = json.loads(os.getenv("FMS_OMX_SSH_TARGETS", "{}"))
        target = targets.get(robot_id) if isinstance(targets, dict) else None
        if target is None:
            return None
        # SSH 옵션/쉘 메타문자가 들어갈 수 없도록 user@host만 받는다.
        if not isinstance(target, str) or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}@[a-zA-Z0-9][a-zA-Z0-9.-]{0,252}", target):
            raise ValueError("invalid target")
        return target
    except (ValueError, TypeError):
        return None


def restart_service(manager, robot_id):
    if robot_id not in PALLETS:
        raise HTTPException(404, "등록되지 않은 OMX입니다")
    target = target_for(robot_id)
    if target is None:
        raise HTTPException(503, "이 장비의 SSH 재시작 주소가 설정되지 않았습니다")
    with manager._pending_lock:
        omx = manager.get_omx(robot_id)
        if (robot_id in manager._maintenance_devices or any(k[0] == robot_id for k in manager._pending_jobs)
                or (omx is not None and omx.state in ("WORKING", "COMMAND_SENT"))):
            raise HTTPException(409, "작업 진행·응답 대기 또는 재시작 처리 중입니다")
        manager._maintenance_devices.add(robot_id)
    try:
        # 호스트키는 미리 등록해야 하며 비밀번호 입력을 기다리지 않는다.
        command = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                   "-o", "ConnectTimeout=5", "-o", "ConnectionAttempts=1", "-o", "ServerAliveInterval=3",
                   "-o", "ServerAliveCountMax=1", target,
                   "systemctl", "--user", "restart", f"omx-manager@{robot_id}.service"]
        identity = os.getenv("FMS_OMX_SSH_IDENTITY", "").strip()
        if identity:
            key = Path(identity).expanduser()
            if not key.is_file():
                raise HTTPException(503, "OMX SSH 키 파일이 없습니다. setup_omx_ssh.sh를 실행하세요")
            command[1:1] = ["-i", str(key), "-o", "IdentitiesOnly=yes"]
        LOG.info("[OMX] 서비스 재시작 요청: %s", robot_id)
        result = subprocess.run(command, capture_output=True, text=True, timeout=65, check=False)
        if result.returncode:
            LOG.warning("[OMX] SSH 서비스 재시작 실패: %s code=%s", robot_id, result.returncode)
            raise HTTPException(502, "재시작 요청 실패: SSH 키·호스트키·사용자 서비스 등록 상태를 확인하세요")
        if omx is not None:
            omx.connected = False
            omx.pallet_observation.clear()
        LOG.info("[OMX] 서비스 재시작 명령 처리 완료: %s, 대기 상태 수신 확인 필요", robot_id)
        return {"omx_id": robot_id, "accepted": True,
                "message": "서비스 재시작을 요청했습니다. OMX 연결·대기 상태 복귀를 확인하세요"}
    except subprocess.TimeoutExpired:
        LOG.warning("[OMX] SSH 재시작 응답 시간 초과: %s", robot_id)
        raise HTTPException(504, "SSH 응답 시간 초과. 요청이 전달됐을 수 있으므로 OMX 상태를 먼저 확인하세요")
    except OSError:
        raise HTTPException(503, "SSH 실행 파일 또는 실행 환경을 확인하세요")
    finally:
        with manager._pending_lock:
            manager._maintenance_devices.discard(robot_id)
