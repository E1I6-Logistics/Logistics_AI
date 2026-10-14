# Logistics FMS

물류 로봇의 위치·경로·주문을 한 화면에서 관리하는 관제 시스템입니다. FastAPI 백엔드, React/Vite 대시보드, ROS 2 Jazzy, Nav2, Zenoh, MQTT, 시뮬레이션과 AI 경로 선택 기능을 사용합니다.

## 현재 기능

- 지도에 로봇 위치, 방향, 경로, 점유·예약 상태를 표시합니다.
- 상단에서 **시뮬 · 알고리즘 / 시뮬 · AI / 실제 · 알고리즘 / 실제 · AI**를 선택합니다. AI 모드에서는 현재 경로 선택에 AI가 적용되고 배차·작업대 선택과 교통 제어는 알고리즘을 사용합니다.
- 관제 화면에서 A/B/C/D 품목을 합계 1~5개로 주문합니다. 주문은 대기열에서 가용 로봇에 배차되며 픽업대 N5(A/B), N6(C/D)를 거쳐 작업대와 로봇 스테이션으로 이동합니다.
- 하단 주문 패널에서 진행 단계, 로봇별 주문 품목, 픽업대·작업대 상태와 실패 사유를 확인합니다. 목록은 5건 높이로 스크롤하며 패널 높이를 드래그하거나 접을 수 있습니다.
- 하단 목록에서 주문을 선택해 수동으로 취소할 수 있습니다. 배정된 로봇은 적재 상태와 관계없이 기존 경로 예약을 이용해 담당 스테이션으로 복귀합니다.
- 지도 목적지 이동, 실제 로봇 원격 제어, 긴급 정지·해제를 지원합니다. 실제 로봇을 연결할 때는 ROS 2 액션 서버와 Zenoh 네트워크가 준비되어야 합니다.
- 실패한 시뮬레이션 주문은 실패 사유와 복귀 상태를 표시하고, 복귀한 뒤 수동으로 재시도할 수 있습니다. 실제 주문은 현장 적재물과 OMX 결과를 확인한 뒤 다시 등록해야 합니다.
- AI 경로는 Ollama, Laya, Kev 등의 설정된 모델을 사용할 수 있습니다. 모델 서버와 GPU 준비 여부는 선택한 모델에 따라 다릅니다.

## 폴더 구조

```text
backend/
  app/                 FastAPI, 주문·배차·교통·ROS 2 서비스
  simulation/          시뮬레이션 실행기와 AI 경로 평가 도구
frontend/              React 관제 화면
maps/                  지도 파일
routes/                GeoJSON 경로 그래프
robots_ws/             Logistics_AMR ROS 2 작업공간 (Git 제외)
scripts/
  setup.sh             메인/로봇 PC 일괄 설치 및 검증
  verify_env.sh        역할별 환경 검증
  main/                메인 PC Zenoh 패키지와 셸 설정
  robot/               로봇 PC Zenoh 패키지와 셸 설정
tests/                 단위 테스트, 시나리오, 수동 MQTT 점검 도구
infra/                 Zenoh Router·MQTT Docker Compose 예시
doc/                   상세 기술 문서
.env.example           공통 환경변수 양식
.env                   로컬 설정과 비밀키 (Git 제외)
start_fms.sh / stop_fms.sh
```

시뮬레이션 코드는 `backend/simulation/`에 있습니다. 백엔드 주문·배차 로직은 `backend/app/`에 있고 시뮬레이션에서도 같은 주문 흐름을 사용합니다.

## 설치

Ubuntu 24.04(x86_64 또는 aarch64/Jetson), 인터넷 연결과 `sudo` 권한이 필요합니다. 설치는 시스템 APT 패키지와 사용자 Python/Node 환경을 변경합니다. **메인 PC와 로봇 PC에서 각각 한 번씩** 아래 명령을 실행합니다.

```bash
# 메인 PC: ROS 2, Docker, Node, Python, Zenoh, AI 의존성, Kev, ROS 작업공간, 프런트엔드
cd /path/to/AX_Logistics
bash scripts/setup.sh main

# 각 로봇 PC: ROS 2와 로봇 측 Zenoh 패키지
cd /path/to/AX_Logistics
bash scripts/setup.sh robot
```

인수 없이 `bash scripts/setup.sh`를 실행하면 `main` 역할입니다. 스크립트는 기존 설치를 재사용하고 마지막에 `scripts/verify_env.sh`를 자동 실행합니다. 메인 PC에서는 다음 항목도 자동 준비합니다.

- `~/venv/robot` 가상환경에 `backend/requirements.txt`, `backend/simulation/requirements.txt`, Kev 서버 의존성 설치
- `robots_ws/Logistics_AMR`가 없으면 `dev` 브랜치를 clone하고, 기존 작업공간도 `dev` 브랜치인지 확인한 뒤 ROS 의존성 설치와 `colcon build --symlink-install` 실행
- `frontend/`의 npm 의존성 설치
- 루트 `.env`와 `infra/docker-compose.yml`이 없을 때만 예시 파일에서 생성
- `~/.axbashrc` 또는 `~/.axrobotbashrc`를 생성해 `~/.bashrc`에서 불러오기

`start_fms.sh`는 Kev 소스를 기본적으로 `../kev`에서 찾습니다. 설치 스크립트도 같은 위치에 없으면 clone합니다. 위치를 바꾸려면 `FMS_KEV_DIR`를 지정합니다. Kev를 사용하지 않을 환경에서는 설치와 실행 모두 `FMS_KEV_ENABLED=0`으로 설정합니다. Kev 모델 가중치는 설치 중 내려받지 않고 첫 서버 실행 시 선택한 모델에 맞춰 준비됩니다.
메인 PC의 `~/.axbashrc`에는 Kev 경로 선택 설정이 들어갑니다. 이미 설치한 PC는 `bash scripts/setup.sh main`을 다시 실행하거나 해당 파일에 `scripts/main/bashrc_main.conf`의 Kev 설정을 반영해야 합니다.

설치 후 새 터미널을 열어 셸 설정을 적용합니다. Docker 그룹에 처음 추가된 경우에는 로그아웃 후 다시 로그인합니다. 필요하면 역할별 점검을 다시 실행합니다.

```bash
bash scripts/verify_env.sh main
bash scripts/verify_env.sh robot
```

로봇의 실제 주행에는 해당 장비의 TurtleBot 펌웨어·센서·Nav2 설정도 필요합니다. 로봇별 하드웨어 설정은 [ROS 2/Zenoh 안내](doc/ros2-zenoh-guide.md)를 확인합니다.

## 환경 설정

루트 `.env.example`은 공유용 양식이고 `.env`가 실제 로컬 설정 파일입니다. `.env`는 Git에서 제외됩니다. `start_fms.sh`와 Python 백엔드·평가 도구가 이 파일을 읽으며 이미 셸에 export된 환경변수가 우선합니다. API 키는 `.env`에만 입력합니다.

| 변수 | 용도 |
| --- | --- |
| `FMS_ROBOT_MODE` | 서버 시작 운용 모드: `simulation` 또는 `real` |
| `FMS_MAP_YAML`, `FMS_ROUTE_GRAPH` | 사용할 지도와 경로 그래프 |
| `LLM_PROVIDER`, `OLLAMA_MODEL`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` | LLM 경로 기능 |
| `ROUTE_SELECTOR`, `LAYA_*`, `KEV_*` | AI 경로 선택기와 모델 서버 |
| `FMS_KEV_ENABLED`, `FMS_KEV_DIR`, `FMS_KEV_RUN` | Kev 자동 실행 여부·소스 위치·모델 |
| `FMS_BACKEND_PORT`, `FMS_FRONTEND_PORT`, `FMS_KEV_PORT` | 서비스 포트 |
| `FMS_VENV_DIR`, `FMS_ROS_WS_SETUP` | Python 가상환경과 ROS 작업공간 setup 경로 |

AI 모드에서 선택할 모델은 백엔드 설정에 활성화되어 있어야 합니다. Ollama 모델을 사용한다면 Ollama 서버와 모델을 별도로 준비해야 합니다. Laya와 Kev 로컬 실행에는 해당 모델에 맞는 GPU·메모리가 필요할 수 있습니다.

## 시작과 종료

```bash
cd /path/to/AX_Logistics
./start_fms.sh
./stop_fms.sh
```

시작 스크립트는 Docker Compose로 Zenoh Router와 MQTT Broker를 시작하고, 호스트에서 ROS 2 daemon, Zenoh Bridge, FastAPI, Vite, Kev를 시작합니다. 각 서비스의 준비 상태를 확인하며 실패하면 이번 시작에서 띄운 스택을 정리합니다.

| 서비스 | 기본 주소 |
| --- | --- |
| 관제 화면 | `http://127.0.0.1:5173/` |
| 백엔드 상태 | `http://127.0.0.1:8000/health` |
| API 문서 | `http://127.0.0.1:8000/docs` |
| Zenoh Router / REST | `7447` / `8001` |
| MQTT | `1883` |
| Kev | `http://127.0.0.1:8011/` |

실행 로그와 PID 파일은 `logs/`에 있습니다. 백엔드가 시뮬레이션 모드에서도 ROS 2 노드와 커스텀 `turtlebot3_my_msg/action/PrecisionDock`를 가져오므로 `robots_ws/Logistics_AMR` 빌드가 필요합니다.

## 관제 화면과 주문

왼쪽 검은색 내비게이션은 그대로 유지되고, 상단에서 네 가지 운용 모드를 바꿀 수 있습니다. 관제 지도에서 주문을 등록하며 하단 패널에서 주문 대기·픽업·작업대·복귀 흐름을 확인합니다. 주문명 입력을 생략하면 `ORDER-001` 형식으로 자동 지정됩니다. 화면 오른쪽은 선택한 로봇의 상태와 제어 기능입니다.

시뮬레이션 로봇은 `robot1`~`robot3`입니다. 실제 모드에서는 Zenoh로 발견한 로봇과 ROS 2 상태를 사용합니다. AI 모드의 경로 선택 외 배차·작업대·교통 안전 로직은 현재 알고리즘으로 동작합니다.

## 주요 API

| 경로 | 기능 |
| --- | --- |
| `GET /health` | 백엔드 상태 |
| `GET, PUT /api/mode` | 시뮬/실제 및 AI/알고리즘 모드 |
| `GET /api/robots` | 로봇 상태 |
| `GET /api/map/info`, `GET /api/map/image` | 지도 |
| `GET /api/route/graph`, `GET /api/route/nodes` | 경로 그래프 |
| `GET, POST /api/orders` | 주문 목록·등록 |
| `POST /api/orders/{order_id}/retry` | 실패한 시뮬레이션 주문 재시도 |
| `DELETE /api/orders/{order_id}` | 주문 취소 및 배정 로봇 복귀 요청 |
| `POST /api/command/goal-node` | 선택 노드로 이동 |
| `WS /ws/dashboard`, `WS /ws/cmd_vel` | 상태 스트림·수동 제어 |

전체 엔드포인트와 요청 형식은 실행 중인 백엔드의 `/docs`에서 확인합니다.

## 테스트와 점검

```bash
cd /path/to/AX_Logistics
~/venv/robot/bin/python -m unittest discover -s tests -v
cd frontend
npm run typecheck
npm run build
```

`tests/`에는 단위 테스트, `tests/traffic_scenarios.py`와 시나리오 JSON, `tests/manual_omx_client.py`·`tests/manual_omx_server.py` 수동 MQTT 점검 도구가 있습니다. 수동 도구는 자동 단위 테스트에 포함되지 않습니다.

```bash
python -m tests.traffic_scenarios cycle
python -m tests.manual_omx_client --robot-id omx1 --broker-ip 127.0.0.1
python -m tests.manual_omx_server
```

교통 시나리오의 조건과 실행법은 [시나리오 안내](doc/traffic_scenarios.md)에 있습니다. AI 경로 평가 도구는 `backend/simulation/evaluation/`에 있고 별도 절차는 [LLM 경로 평가 안내](doc/llm-route-generation-benchmark.md)에 있습니다.

## 문제 해결

- `turtlebot3_my_msg` import 실패: `robots_ws/Logistics_AMR/install/setup.bash` 생성 여부를 확인하고 작업공간을 다시 빌드합니다.
- 대시보드·API 실행 실패: `logs/frontend.log`, `logs/backend.log`와 설치 점검 결과를 확인합니다.
- Kev 서버 실행 실패: `logs/kev.log`, 모델 이름, GPU 메모리와 `FMS_KEV_DIR`를 확인합니다.
- 실제 로봇이 보이지 않음: `logs/zenoh_bridge.log`, Zenoh Router·로봇 Bridge, ROS domain과 namespace를 확인합니다.
- 주문이 대기 중: 로봇 연결·점유·복귀 상태와 픽업대 예약을 하단 주문 패널에서 확인합니다.
