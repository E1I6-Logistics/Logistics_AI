"""FMS 전체 stop → start. 종료되는 웹서버와 분리된 프로세스가 이어받는다."""
import argparse
import fcntl
import json
import logging
import os
import re
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

from fastapi import HTTPException

from ..config import BASE_DIR

INSTANCE_ID = uuid4().hex
LOG = logging.getLogger(__name__)
ACTIVE_PHASES = {"QUEUED", "STOPPING", "STARTING"}


def directory():
    return Path(os.getenv("FMS_LOG_DIR", str(BASE_DIR / "logs"))).expanduser().resolve()


def write_record(path, **record):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".web-restart-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as file:
            json.dump(dict(record, at=time.time()), file, ensure_ascii=False)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def status():
    available = True
    reason = "FMS 전체 종료 후 자동 시작"
    try:
        if int((directory() / "backend.pid").read_text().strip()) != os.getpid():
            raise ValueError("different launcher")
        if not all((BASE_DIR / name).is_file() for name in ("stop_fms.sh", "start_fms.sh")):
            raise ValueError("missing script")
    except (OSError, ValueError):
        available, reason = False, "start_fms.sh로 시작한 FMS에서 사용할 수 있습니다"
    try:
        record = json.loads((directory() / "web_restart.json").read_text())
        if not isinstance(record, dict) or not 0 <= time.time() - record.get("at", 0) < 900:
            record = {}
    except (OSError, ValueError, TypeError):
        record = {}
    return dict(instance_id=INSTANCE_ID, available=available, reason=reason,
                restarting=record.get("phase") in ACTIVE_PHASES, restart=record)


def request_restart():
    details = status()
    if not details["available"]:
        raise HTTPException(503, details["reason"])
    folder = directory()
    try:
        folder.mkdir(parents=True, exist_ok=True)
        fd = os.open(folder / "web_restart.lock", os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        raise HTTPException(503, "FMS 로그 폴더에 재시작 기록을 쓸 수 없습니다")
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise HTTPException(409, "이미 FMS 재시작을 처리 중입니다")
        token = uuid4().hex
        write_record(folder / "web_restart.json", phase="QUEUED", token=token, previous_instance=INSTANCE_ID,
                     message="전체 FMS 종료 후 자동 시작 예약")
        # 독립 Python 실행기가 한 번 더 fork하여 백엔드의 자식 종료 대상에서도 벗어난다.
        with (folder / "web_restart.log").open("a") as log:
            process = subprocess.Popen(
                [sys.executable, "-m", "backend.app.services.fms_restart", "--worker", "--lock-fd", str(fd),
                 "--token", token, "--previous-instance", INSTANCE_ID],
                cwd=BASE_DIR, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True, pass_fds=(fd,))
            try:
                code = process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait()
                raise RuntimeError("재시작 실행기 응답 시간 초과")
        if code:
            raise RuntimeError("독립 재시작 실행기 시작 실패")
        LOG.warning("[FMS] 사용자 요청으로 전체 stop → start 예약 token=%s", token)
        return dict(accepted=True, token=token, previous_instance=INSTANCE_ID,
                    message="전체 FMS 재시작 요청 완료. 서버 복귀 후 자동 새로고침합니다")
    except HTTPException:
        raise
    except (OSError, RuntimeError) as exc:
        write_record(folder / "web_restart.json", phase="FAILED", message=str(exc))
        raise HTTPException(503, "재시작 실행기를 시작하지 못했습니다. logs/web_restart.log를 확인하세요")
    finally:
        # 자식에게 전달된 동일 파일 설명이 잠금을 유지한다. 여기서 LOCK_UN 하지 않는다.
        os.close(fd)


def run_script(root, name, timeout, *, env=None):
    process = subprocess.Popen(["/bin/bash", str(root / name)], cwd=root,
                               stdin=subprocess.DEVNULL, start_new_session=True, env=env)
    try:
        code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL); process.wait()
        raise RuntimeError(name + " 실행 시간 초과")
    if code:
        raise RuntimeError(name + " 실패: exit=" + str(code))



def launch_environment(root):
    # 백엔드가 시작될 때 읽었던 옛 .env 값을 그대로 넘기지 않고 현재 파일을 적용한다.
    environment = os.environ.copy()
    config = root / ".env"
    if config.exists():
        for line in config.read_text().splitlines():
            key, separator, value = line.partition("=")
            if separator and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                environment[key] = value
    environment["FMS_CLEAN_START"] = "0"
    environment["FMS_KEEP_TRANSPORT"] = "1"
    return environment


def worker(root, folder, token, previous_instance, *, delay=2):
    def report(phase, message):
        write_record(folder / "web_restart.json", phase=phase, token=token,
                     previous_instance=previous_instance, message=message)
    try:
        time.sleep(delay)  # API 응답을 브라우저에 먼저 전달한다.
        report("STOPPING", "전체 FMS 종료 중")
        run_script(root, "stop_fms.sh", 180, env=launch_environment(root))
        report("STARTING", "전체 FMS 시작 중")
        environment = launch_environment(root)
        run_script(root, "start_fms.sh", 480, env=environment)
        report("COMPLETE", "전체 FMS 시작 완료")
    except Exception as exc:
        # start 도중 일부 프로세스만 남는 경우도 기존 종료 절차로 정리한다.
        try:
            run_script(root, "stop_fms.sh", 180, env=launch_environment(root))
        except Exception as cleanup:
            print("[FMS] 재시작 실패 후 정리 오류:", cleanup, flush=True)
        report("FAILED", str(exc))
        print("[FMS] 전체 재시작 실패:", exc, flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", action="store_true", required=True)
    parser.add_argument("--lock-fd", type=int, required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--previous-instance", required=True)
    args = parser.parse_args()
    launcher = os.getpid()
    child = os.fork()
    if child:
        return
    # 부모 실행기가 종료돼야 stop_fms.sh의 backend 자식 정리에 잡히지 않는다.
    deadline = time.monotonic() + 5
    while os.getppid() == launcher and time.monotonic() < deadline:
        time.sleep(.02)
    if os.getppid() == launcher:
        raise RuntimeError("재시작 실행기 분리 실패")
    try:
        worker(BASE_DIR, directory(), args.token, args.previous_instance)
    finally:
        os.close(args.lock_fd)


if __name__ == "__main__":
    main()
