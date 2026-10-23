#!/usr/bin/env python3

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import paho.mqtt.client as mqtt


# =============================================================
# Argument
# =============================================================

parser = argparse.ArgumentParser()

parser.add_argument("--robot-id", required=True, help="OMX ID (예: omx1)")

parser.add_argument("--broker-ip", required=True, help="FMS MQTT Broker IP")

args = parser.parse_args()

robot_id = args.robot_id
broker_ip = args.broker_ip
broker_port = 1883

PICKUP_ITEMS = {"omx1": ("A", "B"), "omx2": ("C", "D")}
WORKSTATION_IDS = ("omx3", "omx4")

if robot_id not in PICKUP_ITEMS and robot_id not in WORKSTATION_IDS:
    parser.error("robot-id는 omx1, omx2, omx3, omx4 중 하나여야 합니다.")


# =============================================================
# Path
# =============================================================

directory = Path(__file__).resolve().parent

state_path = Path(os.environ.get("OMX_INVENTORY_STATE_PATH", "~/.local/state/lerobot/omx_inventory.json")).expanduser()

new_order_script = directory / "omx_new_order.sh"
omx3_script = directory / "run_e_interactive.py"
omx3_policy = os.environ.get(
    "OMX3_POLICY_PATH",
    "outputs/train/warehouse_item_e_132_merged_act_300k/checkpoints/250000/pretrained_model",
)
omx3_port = os.environ.get(
    "OMX3_FOLLOWER_PORT",
    "/dev/serial/by-id/usb-ROBOTIS_OpenRB-150_100E8DB15157375037202020FF0F2015-if00",
)



# =============================================================
# MQTT / OMX 상태
# =============================================================

client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)

job_lock = threading.Lock()

current_job_id = None
current_state = "NOT_IMPLEMENTED" if robot_id == "omx4" else "IDLE"
current_orders = None
omx3_process = None


# =============================================================
# MQTT Publish
# =============================================================


# 관련을 발행한다.
def publish(topic, payload):
    message = json.dumps(payload)

    client.publish(topic, message)

    print(f"[SEND] {topic}: {payload}", flush=True)


# 상태를 전송한다.
def send_status():
    publish(f"{robot_id}/status", {"state": current_state})


# 응답을 전송한다.
def send_ack(job_id, accepted=True):
    publish(f"{robot_id}/ack", {"job_id": job_id, "accepted": accepted})


# 진행을 전송한다.
def send_progress(job_id, current, total):
    publish(f"{robot_id}/progress", {"job_id": job_id, "current": current, "total": total})


# 결과를 전송한다.
def send_result(job_id, success, message=""):
    publish(f"{robot_id}/result", {"job_id": job_id, "success": success, "message": message})


# =============================================================
# FMS items -> OMX 주문 변환
# =============================================================


# 주문을 만든다.
def make_orders(items):

    if not isinstance(items, dict):
        raise ValueError("items는 객체 형식이어야 합니다.")

    allowed_items = PICKUP_ITEMS.get(robot_id)

    if allowed_items is None:
        raise ValueError("작업대 하차 동작은 아직 구현되지 않았습니다.")

    if any(item not in allowed_items for item in items):
        raise ValueError(f"{robot_id}에서 처리하지 않는 품목이 있습니다.")

    orders = []

    for item in allowed_items:

        quantity = items.get(item, 0)

        if isinstance(quantity, bool):
            raise ValueError(f"{item} 수량이 올바르지 않습니다.")

        try:
            quantity = int(quantity)

        except (TypeError, ValueError):
            raise ValueError(f"{item} 수량이 올바르지 않습니다.")

        if quantity < 0 or quantity > 8:
            raise ValueError(f"{item} 수량은 0~8이어야 합니다.")

        if quantity > 0:
            orders.append((item, quantity))

    if not orders:
        raise ValueError(f"실행할 {'/'.join(allowed_items)} 주문이 없습니다.")

    return orders


# =============================================================
# 시간 출력
# =============================================================


# 작업 시작 이후 경과 시간을 계산한다.
def elapsed(seconds):

    h, rem = divmod(max(0, int(seconds)), 3600)

    m, s = divmod(rem, 60)

    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"

    return f"{m:02d}:{s:02d}"


# =============================================================
# OMX 실제 주문 실행
# =============================================================


# 주문을 실행한다.
def execute_orders(job_id, orders):

    started = time.monotonic()

    total_quantity = sum(qty for _, qty in orders)

    completed_before = 0

    print("[ORDER] 주문 순서: " + " -> ".join(f"{item} {qty}개" for item, qty in orders), flush=True)

    for index, (item, qty) in enumerate(orders, 1):

        # omx2의 물리 모델은 로컬 A/B 명령을 각각 C/D 품목에 사용한다.
        local_item = {"C": "A", "D": "B"}.get(item, item)

        print(f"[ORDER] [{index}/{len(orders)}] " f"{item} {qty}개 등록", flush=True)

        # 기존 OMX 주문 등록 스크립트 사용
        result = subprocess.run(["bash", str(new_order_script), local_item, str(qty)])

        if result.returncode != 0:
            raise RuntimeError(f"{item} 주문 등록 실패")

        if not state_path.exists():
            raise RuntimeError(f"상태 파일이 없습니다: {state_path}")

        accepted = json.loads(state_path.read_text())

        token = accepted["order_started_at_epoch"]

        last_progress = None

        # -----------------------------------------------------
        # OMX inventory 상태 감시
        # -----------------------------------------------------

        while True:

            state = json.loads(state_path.read_text())

            current_order = (state.get("order_item"), state.get("order_quantity"), state.get("order_started_at_epoch"))

            expected_order = (local_item, qty, token)

            if current_order != expected_order:
                raise RuntimeError("주문 상태가 외부에서 변경되었습니다.")

            done = int(state["order_completed"])

            remaining = int(state["remaining"])

            total_completed = completed_before + done

            # 진행 상태가 바뀌었을 때만 progress 전송
            if total_completed != last_progress:

                send_progress(job_id, total_completed, total_quantity)

                print(
                    f"[ORDER] {item} " f"완료={done}/{qty}, " f"전체=" f"{total_completed}/" f"{total_quantity}",
                    flush=True,
                )

                last_progress = total_completed

            # 해당 품목 작업 완료
            if remaining == 0:

                if done != qty:
                    raise RuntimeError(f"{item} 완료 수량 불일치: " f"완료={done}, 주문={qty}")

                print(f"[ORDER] " f"{item} 주문 완료", flush=True)

                completed_before += qty

                break

            time.sleep(0.5)

    print("[ORDER] 전체 주문 완료 | " f"총 소요시간: " f"{elapsed(time.monotonic() - started)}", flush=True)


# OMX3: MQTT 작업 요청 1건당 E 모델 프로그램을 한 번 실행한다.
def execute_unload(job_id, items):
    if robot_id != "omx3":
        raise RuntimeError(f"{robot_id} 하차 동작은 구현되지 않았습니다.")

    policy_path = Path(omx3_policy).expanduser()
    if not policy_path.is_absolute():
        policy_path = directory / policy_path
    if not (policy_path / "config.json").is_file():
        raise FileNotFoundError(f"OMX3 정책 파일 없음: {policy_path / 'config.json'}")
    if not omx3_script.is_file():
        raise FileNotFoundError(f"OMX3 실행 파일 없음: {omx3_script}")
    if not Path(omx3_port).exists():
        raise FileNotFoundError(f"OMX3 컨트롤러 연결 확인 필요: {omx3_port}")

    env = os.environ.copy()
    env.update({
        "E_DISPLAY_DATA": "false",
        "E_FOLLOWER_PORT": omx3_port,
        "E_POLICY_PATH": str(policy_path),
    })
    print(f"[OMX3] Job {job_id}: E 모델 실행 {policy_path}", flush=True)
    global omx3_process
    proc = subprocess.Popen(
        [sys.executable, str(omx3_script)], cwd=directory, env=env,
        start_new_session=True,
    )
    omx3_process = proc
    try:
        returncode = proc.wait()
    finally:
        omx3_process = None
    if returncode != 0:
        raise RuntimeError(f"OMX3 프로그램 비정상 종료 (exit={returncode})")
    # exit=0은 프로그램 정상 종료이지 물리적 하차 성공을 검증한 값은 아니다.



# =============================================================
# 실제 작업 Thread
# =============================================================


# 작업을 실행한다.
def run_job(job_id, orders):

    global current_job_id
    global current_state
    global current_orders

    success = False
    result_message = ""

    try:

        current_state = "WORKING"

        send_status()

        if robot_id == "omx3":
            execute_unload(job_id, orders)
        else:
            execute_orders(job_id, orders)

        success = True

    except Exception as exc:

        print(f"[ORDER] 작업 실패: {exc}", flush=True)

        result_message = str(exc)

    finally:
        # 작업이 끝난 뒤 상태를 먼저 정리해야 Result 직후의 다음 Command를 받을 수 있다.
        current_job_id = None
        current_orders = None
        current_state = "IDLE"

        try:
            send_status()
        finally:
            job_lock.release()

        if success:
            print(f"[ORDER] Job 완료: {job_id}", flush=True)

        done_message = ("Script exited normally; physical completion unverified"
                        if robot_id == "omx3" else "Order completed")
        send_result(job_id, success, done_message if success else result_message)


# =============================================================
# MQTT Command 처리
# =============================================================


# 명령 이벤트를 처리한다.
def on_command(data):

    global current_job_id
    global current_state
    global current_orders

    job_id = data.get("job_id")
    items = data.get("items")

    if not job_id:

        print("[MQTT] job_id가 없습니다.", flush=True)

        return

    if robot_id == "omx4":
        print("[MQTT] omx4 하차 동작은 아직 구현되지 않았습니다.", flush=True)
        send_ack(job_id, False)
        return

    print()
    print("==============================")
    print("COMMAND RECEIVED")
    print("==============================")
    print(f"Job   : {job_id}")
    print(f"Items : {items}")
    print("==============================")

    # =========================================================
    # 현재 실행 중인 job과 동일
    # =========================================================

    if current_job_id == job_id:

        try:
            same_orders = (True if robot_id == "omx3"
                           else tuple(make_orders(items)) == current_orders)
        except ValueError:
            same_orders = False

        if not same_orders:
            print(f"[MQTT] 같은 Job ID에 다른 주문이 들어왔습니다: {job_id}", flush=True)
            send_ack(job_id, False)
            return

        print(f"[MQTT] 이미 실행 중인 Job: " f"{job_id}", flush=True)

        send_ack(job_id, True)

        return

    # =========================================================
    # 다른 작업 진행 중
    # =========================================================

    if not job_lock.acquire(blocking=False):

        print(f"[MQTT] OMX BUSY - " f"주문 거절: {job_id}", flush=True)

        send_ack(job_id, False)

        return

    # =========================================================
    # 주문 검증
    # =========================================================

    try:

        # OMX3은 품목/수량에 관계없이 command 1건당 한 번 실행한다.
        orders = ("TRIGGER",) if robot_id == "omx3" else make_orders(items)

    except Exception as exc:

        print(f"[MQTT] 주문 검증 실패: " f"{exc}", flush=True)

        send_ack(job_id, False)

        send_result(job_id, False, str(exc))

        job_lock.release()

        return

    # =========================================================
    # 주문 접수
    # =========================================================

    current_job_id = job_id
    current_orders = tuple(orders)
    current_state = "WORKING"

    send_ack(job_id, True)

    # MQTT callback이 막히지 않도록
    # 실제 OMX 작업은 별도 Thread에서 실행
    worker = threading.Thread(target=run_job, args=(job_id, orders), daemon=True)

    worker.start()


# =============================================================
# MQTT Callback
# =============================================================


# 연결 이벤트를 처리한다.
def on_connect(client, userdata, flags, rc):

    print(f"[MQTT] Connected: {rc}", flush=True)

    topic = f"{robot_id}/command"

    client.subscribe(topic)

    print(f"[MQTT] Subscribe: " f"{topic}", flush=True)

    send_status()


# disconnect 이벤트를 처리한다.
def on_disconnect(client, userdata, rc):

    print(f"[MQTT] Disconnected: {rc}", flush=True)


# message 이벤트를 처리한다.
def on_message(client, userdata, msg):

    try:

        data = json.loads(msg.payload.decode())

    except Exception as exc:

        print(f"[MQTT] JSON Error: {exc}", flush=True)

        return

    if msg.topic == (f"{robot_id}/command"):

        on_command(data)


client.on_connect = on_connect
client.on_disconnect = on_disconnect
client.on_message = on_message


# =============================================================
# 종료 시 OMX3의 자식 프로세스도 종료 (로봇팔 안전정지는 별도 보장 필요)
# =============================================================


def stop_omx3_process(signum, frame):
    if robot_id == "omx3" and omx3_process is not None:
        try:
            os.killpg(omx3_process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    raise SystemExit(128 + signum)


# =============================================================
# Main
# =============================================================


# 명령행 프로그램의 실행 절차를 시작한다.
def main():

    signal.signal(signal.SIGTERM, stop_omx3_process)
    signal.signal(signal.SIGINT, stop_omx3_process)

    print("==============================")
    print("OMX MQTT Client")
    print("==============================")
    print(f"OMX ID      : {robot_id}")
    print(f"Broker      : {broker_ip}")
    print(f"Broker Port : {broker_port}")
    print(f"Order Script: {new_order_script}")
    print(f"State File  : {state_path}")
    print("==============================")

    if robot_id in PICKUP_ITEMS and not new_order_script.exists():
        raise FileNotFoundError(f"omx_new_order.sh가 없습니다: {new_order_script}")
    if robot_id == "omx3" and not omx3_script.is_file():
        raise FileNotFoundError(f"run_e_interactive.py가 없습니다: {omx3_script}")

    client.connect(broker_ip, broker_port, 60)

    client.loop_start()

    try:

        while True:

            # FMS 연결 확인용 heartbeat
            send_status()

            time.sleep(2)

    except KeyboardInterrupt:

        print("\n[MQTT] Stopping...", flush=True)

    finally:

        client.loop_stop()

        client.disconnect()


if __name__ == "__main__":
    main()
