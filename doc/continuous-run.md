# 연속 운용 브랜치와 OMX 연결 표시

작업 브랜치는 `feat/fms-continuous-run`입니다. 기존 `ugie-dev`와 main은 변경하지 않습니다.

## OMX 4대가 동시에 깜빡이는 문제

- 웹 `/api/omx` 조회가 한 번 실패하면 4대 모두 `connected=false`로 바꾸던 처리를 제거했습니다. 조회 실패는 노란색 **조회 지연**으로 표시하고 마지막 장치 상태와 주문·횟수를 보존합니다. 지연된 상태에서는 재시작 조작을 차단하며, 실제 API의 OFFLINE 결과는 그대로 표시합니다.
- 실제 브로커 로그에서도 FMS 종료·시작과 함께 Mosquitto가 재시작하며 4대가 재접속하는 별도 현상을 확인했습니다. 웹 재시작과 `start_fms`의 사전 정리는 공용 MQTT·Zenoh 컨테이너를 유지합니다. 명시적인 `./stop_fms`는 종전처럼 전체를 종료합니다.
- OMX MQTT는 브로커가 처음부터 없어도 실행기를 종료하지 않고 1~5초 간격으로 재연결합니다. ack/progress/result는 QoS 1과 최대 128개 메모리 큐를 사용합니다. 프로세스 종료나 전원 차단까지 큐를 영구 보존하지는 않습니다. 메시지 중복 수신 가능성은 기존 job_id 처리로 구분합니다.

정확한 현장 깜빡임 원인을 확정하려면 발생 시각과 웹의 조회 지연 표시, broker 및 Pi 로그를 함께 대조해야 합니다.

## 자동 재개 범위

이 브랜치의 `start_fms`는 `FMS_CONTINUOUS_RUN=1`을 기본 적용합니다. 일시적인 위치 수신 지연·짧은 연결 단절·종료가 확인된 주행 Action 실패는 최신 위치와 기존 Action 종료를 확인한 후 같은 목적지로 재개합니다. AI가 선택한 경로는 유지하며, 실제 통로 점유가 해소되지 않으면 출발하지 않습니다. 재시도 간격은 최대 30초로 늘립니다.

비상 정지, 취소 종료 미확인, OMX·ArUco·도킹 등 물리 작업 실패를 자동으로 성공 처리하거나 반복 실행하지 않습니다. 10초 이상 AMR 연결 단절 시 기존 작업을 초기화하는 정책도 유지합니다. 따라서 모든 정지를 무조건 제거하는 기능은 아닙니다. 데모 시나리오는 복구 대기를 표시하며 기존 제한 시간 안에서 기다립니다.

## 적용

실행 중인 장비에 자동으로 재시작 명령을 보내지 않습니다. 작업을 마친 후 PC에서 적용합니다.

```bash
cd ~/AX_Logistics
git switch feat/fms-continuous-run
git pull --ff-only
git submodule update --init omx_ws/Logistics_OMX
FMS_KEEP_TRANSPORT=1 ./stop_fms
./start_fms
```

자동 주행 복구만 끄려면 `FMS_CONTINUOUS_RUN=0 ./start_fms`로 시작합니다.

Pi 4대는 각각 다음과 같이 OMX dev를 적용합니다.

```bash
omx_service stop
cd ~/Logistics_OMX
git pull --ff-only origin dev
source ~/.bashrc
omx_service start
```

## 검증과 한계

모의 ROS Action·최신 위치·교통 예약으로 재개, 기존 AI 경로 보존, 비상 정지 및 실제 점유 차단을 검증합니다. 격리된 loopback Mosquitto로 최초 브로커 부재, 연결 후 브로커 재시작, 결과 메시지 큐 재전송을 검사했습니다. 실제 로봇 동작·실제 브로커 재시작은 수행하지 않았으며 현장 주문 시험은 별도로 필요합니다.
