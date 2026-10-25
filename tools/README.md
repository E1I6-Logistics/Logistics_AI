# ROS2 통합 통신 벤치마크 (메인 PC ↔ Robot2)

**3개 비교 대상**

1. **FastDDS**: `rmw_fastrtps_cpp`, 메인/로봇 동일 Domain, DDS 직접, `SUBNET`
2. **CycloneDDS**: `rmw_cyclonedds_cpp`, 메인/로봇 동일 Domain, DDS 직접, `SUBNET`
3. **Zenoh**: 메인/로봇 양쪽 `rmw_cyclonedds_cpp`, ROS2 DDS Bridge 2개 + 메인 PC의 기존 `zenohd` Router, `LOCALHOST`

실제 로봇 Bringup, LiDAR, 카메라, Nav2 실행 없이 **ROS2의 실제 메시지 타입**으로 합성 페이로드를 보내고 수신합니다. 실제 FMS의 API/Action/서비스 처리 시간을 측정하지는 않습니다. `/cmd_vel`, `/scan` 등 실제 운용 Topic에는 송신하지 않습니다. **별도의 `/bench3/` 전용 Topic**을 사용합니다.

## 1. 준비 및 배포

메인 PC (`ugie01`, `~/AX_Logistics/tools`)에서 다음 순서로 실행합니다.

```bash
cd ~/AX_Logistics/tools
# 웹에서 다운로드한 Python 파일이 ~/Downloads에 있다고 가정
cp ~/Downloads/ros_multi_bench.py .
python3 -m py_compile ros_multi_bench.py
scp ros_multi_bench.py turtlebot2@192.168.0.102:~/communication_test/
```

Robot2 (`turtlebot2`, `~/communication_test`)에 파일이 생겼는지 확인:

```bash
cd ~/communication_test
ls -l ros_multi_bench.py
python3 -m py_compile ros_multi_bench.py
```

**전제 조건**:
- 양쪽 `/opt/ros/jazzy/setup.bash` 정상 설치, `python3`에서 `rclpy`, `std_msgs`, `geometry_msgs`, `nav_msgs`, `sensor_msgs` import 가능
- 양쪽에서 FastDDS와 CycloneDDS RMW 설치
- 양쪽에서 `zenoh-bridge-ros2dds` 명령을 찾을 수 있음
- 메인 PC Zenoh Router가 `192.168.0.16:7447`에서 실행 중 (`docker ps` 참고)
- 메인 PC에서 Robot2로 SSH 접속 가능 (`ssh turtlebot2@192.168.0.102`)
- CPU 측정은 `psutil` 설치가 권장됨 (미설치 시 앱 CPU만 기록)
- 다른 대용량 네트워크 작업을 멈춘 상태로 테스트할 것

> Zenoh Bridge가 `/opt/ros/jazzy`가 아닌 별도 작업공간에 설치되어 있으면 `--robot-setup /home/turtlebot2/사용중인_ws/install/setup.bash` 또는 `--main-setup /home/ugie01/사용중인_ws/install/setup.bash`를 추가할 수 있습니다. 예시는 실제 파일 경로로 바꿔야 합니다.

> 도구는 **기존 Router를 재사용**합니다. 테스트용 Bridge는 도구가 양쪽에 임시로 새로 시작하고 종료하며, 기존 FMS 프로세스를 종료하지 않습니다. 기존 Bridge가 Domain 15/0을 사용하므로 테스트용 Domain **67(메인) / 68(Zenoh 로봇)** 을 사용해 충돌을 줄였습니다. 직접 DDS 테스트는 양쪽 Domain 67로 자동 설정합니다. 기존 FMS가 Domain 67/68을 사용 중이라면 값을 변경하세요.

## 2. 명령 하나로 사전검증 (3모드 전부)

메인 PC에서만 실행합니다. Robot2에서 별도 명령 실행은 필요하지 않습니다. 원격 SSH 인증을 요청할 수 있습니다.

```bash
cd ~/AX_Logistics/tools
python3 ros_multi_bench.py all \
  --robot-ip 192.168.0.102 \
  --robot-user turtlebot2 \
  --router-ip 192.168.0.16 \
  --profiles small \
  --duration 5 \
  --repeats 1
```

- `fastdds` → `cyclone` → `zenoh` 자동 순서 실행
- 메인→로봇, 로봇→메인 양방향 각각 테스트
- `RMW_IMPLEMENTATION`, `ROS_DOMAIN_ID`, `ROS_AUTOMATIC_DISCOVERY_RANGE`는 각각 새 자식 프로세스에서 적용
- SSH로 로봇 테스트용 Agent를 실행하고, Zenoh 구간에서만 테스트용 Bridge를 자동 시작/종료
- 기존 `.bashrc`를 수정하거나 `ros_normal`/`ros_local`을 호출할 필요가 없음

**실제 네트워크 전송 없이 조건별 예상시간만 확인:**

```bash
python3 ros_multi_bench.py all --dry-run
```

## 3. 전체 벤치마크 (기본 60초 / 3회 반복 / 양방향 / 3모드)

```bash
cd ~/AX_Logistics/tools
python3 ros_multi_bench.py all \
  --robot-ip 192.168.0.102 \
  --robot-user turtlebot2 \
  --router-ip 192.168.0.16 \
  --duration 60 \
  --repeats 3 \
  --profiles all \
  --directions both \
  --modes all
```

**시간 주의:** 10개 메시지 프로파일 × 2개 방향 × 3가지 모드 × 3회 × 60초 = **180분의 순수 송신시간**입니다. 초기 연결, ACK 수집, 프로세스 재시작을 포함하면 **3시간 이상**입니다. 이 시간은 명령 실행 전 표시됩니다. `Ctrl+C`로 중지한 경우 일부 데이터만 저장될 수 있습니다.

시간이 부족하면 **60초 / 1회**로 먼저 비교하세요.

```bash
python3 ros_multi_bench.py all --duration 60 --repeats 1 --profiles all
```

전체 시간을 줄이되 통계성을 확보하려면 `--duration 30 --repeats 3` 도 가능합니다.

## 4. 실제 ROS2 메시지 프로파일

| 옵션 | 실제 ROS2 인터페이스 | 합성 데이터 규모 | 기본 속도 | QoS |
|---|---|---|---:|---|
| `small` | `std_msgs/msg/String` | 100B | 100Hz | RELIABLE |
| `twist` | `geometry_msgs/msg/Twist` | 소형 속도형 | 50Hz | RELIABLE |
| `odom` | `nav_msgs/msg/Odometry` | 소형 주행 상태 | 30Hz | RELIABLE |
| `scan720` | `sensor_msgs/msg/LaserScan` | 720 rays, ranges + intensities | 10Hz | BEST_EFFORT |
| `scan1080` | `sensor_msgs/msg/LaserScan` | 1080 rays, ranges + intensities | 10Hz | BEST_EFFORT |
| `cloud30k` | `sensor_msgs/msg/PointCloud2` | 30,000 points, XYZ+intensity, 약 480KB | 20Hz | BEST_EFFORT |
| `cloud100k` | `sensor_msgs/msg/PointCloud2` | 100,000 points, XYZ+intensity, 약 1.6MB | 10Hz | BEST_EFFORT |
| `rgb_vga` | `sensor_msgs/msg/Image` | RGB8 640×480, 약 0.92MB | 30FPS | BEST_EFFORT |
| `rgb_hd` | `sensor_msgs/msg/Image` | RGB8 1280×720, 약 2.76MB | 15FPS | BEST_EFFORT |
| `depth_vga` | `sensor_msgs/msg/Image` | 16UC1 640×480, 약 0.61MB | 30FPS | BEST_EFFORT |

임의로 작은 값을 보내는 것이 아니라 **해당 메시지 타입의 필드와 byte array**를 실제로 채웁니다. `Twist`에는 헤더가 없기 때문에 벤치마크 추적용 시퀀스와 시각을 사용하지 않는 필드에 인코딩합니다. 따라서 실제 로봇 `/cmd_vel`에 게시하면 안 되며 도구도 별도 테스트 Topic에만 게시합니다. 다만 값은 동일한 합성 데이터이며, 실제 드라이버/압축/이미지 처리 오버헤드는 없습니다. 프레임을 전달하면 ACK는 작은 `std_msgs/String`으로 응답하므로 대용량 데이터를 왕복 복제하는 테스트는 아닙니다. 

원하는 프로파일만 골라서 실행할 수 있습니다.

```bash
# 일반 명령/상태 관련만
python3 ros_multi_bench.py all --profiles core --duration 60 --repeats 3

# 2D/3D LiDAR + 이미지/깊이만
python3 ros_multi_bench.py all --profiles sensor --duration 60 --repeats 3

# 3D와 RGB만
python3 ros_multi_bench.py all --profiles cloud30k,cloud100k,rgb_vga,rgb_hd --duration 60 --repeats 3

# CycloneDDS vs Zenoh만 (RMW 자체는 동일)
python3 ros_multi_bench.py all --modes cyclone,zenoh --duration 60 --repeats 3
```

## 5. 측정 방향과 지표

- **PC → Robot2**: 메인에서 실제 ROS2 인터페이스 송신 → Robot2 수신 기록 → 작은 ACK 반환 → 메인에서 RTT 측정
- **Robot2 → PC**: 로봇에서 실제 ROS2 인터페이스 송신 → 메인 수신 기록 → 작은 ACK 반환 → Robot2에서 RTT 측정
- **RTT**: 발신 측의 `time.monotonic_ns()`로 계산하여 컴퓨터 간 시계 동기화가 없어도 비교 가능
- **One-way 수신 지연**: `Header.frame_id` 등에 타임스탬프를 포함하지만 PC/로봇 시계 동기화 상태가 증명되지 않으면 신뢰할 수 없음. 요약에서는 제외하고 실행별 raw 참고값만 저장
- **원본 평균, 중앙값, 5% 절사평균, P95, P99, 최대값, 표준편차** 측정
- **Jitter**: 목표 메시지 간격과 실제 수신 간격의 차이 절댓값을 P95 등으로 집계
- **실제 수신 Hz, 송신 스케줄 미달, 수신 누락, ACK 누락, 근사 처리량, 메인/로봇 앱 CPU, Bridge CPU** 기록
- 큰 값은 제거하지 않고 P95/P99/최대값에 그대로 포함하며 **절사평균은 보조 지표**로 제공

> CPU에는 Python ROS2 애플리케이션 실행 비용이 포함됩니다. Zenoh Router가 Docker 내부에서 실행되면 프로세스 CPU가 호스트 `psutil`에서 관측되지 않을 수도 있습니다. 모든 컴포넌트의 총 CPU 비용을 측정했다는 뜻은 아닙니다.

## 6. 저장 파일

메인 PC 기준: `~/ros_bench_results/bench_YYYYMMDD_HHMMSS/`

```text
ros_bench_results/
└── bench_YYYYMMDD_HHMMSS/
    ├── experiment_config.json     # 테스트 설정
    ├── raw_runs.jsonl              # 실행별 원본 통계 (성공한 항목 즉시 기록)
    ├── all_runs.csv                # 각 반복별 RTT/손실/Jitter/CPU 상세
    ├── comparison_summary.csv      # FastDDS/CycloneDDS/Zenoh 비교 요약
    └── REPORT.md                   # 한국어 결과 보고서
```

CSV는 Excel에서 열 수 있으며 UTF-8 BOM으로 저장합니다. 실행별 패킷 전체 시계값은 저장하지 않고, 실행별 집계 통계를 저장합니다. 수치 비교는 반복별 값을 우선 사용하세요.

**주의**: 실제 ROS2 RMW/Bridge가 설치된 2대의 물리 장비 환경은 이 패키지 제작 환경에서 제공되지 않아 통신 실측은 진행하지 못했습니다. 구문/순수 통계/보고서 생성/CLI 입력을 오프라인 검증했습니다. 먼저 `--profiles small --duration 5 --repeats 1`을 실행해 연결성을 검증하세요.
