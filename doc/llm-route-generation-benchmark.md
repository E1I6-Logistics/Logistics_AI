# 로컬 모델 경로 생성·선택 벤치마크

> 최신 재수집 기준은 문서 마지막의 **8조건 전체 재수집 (factorial8-v1)** 절이다.
> 이전 4·5조건 결과와 입력 계약은 과거 기록이며, 현재 스크립트 기본 실행은 8조건이다.

코드가 저장된 Edge `weight`를 사용해 A*로 계산한 최단 경로와 로컬 모델의 결과를 비교한다.

- **Ollama 경로 생성 시험**: 모델이 최단 경로를 직접 생성한다.
- **Laya·Kev 경로 선택 시험**: 코드가 만든 경로 후보 중 하나를 선택한다.

두 시험은 서로 다른 과제이므로 결과를 같은 지표로 해석하지 않는다. 이 실행기는
로봇 이동 명령을 보내지 않으며 FastAPI, ROS 2, Zenoh, 데이터베이스를 사용하지 않는다.

모델 선정 근거, 과거 결과, 상세 문제 해결 방법은
[`llm-route-generation-benchmark-reference.md`](./llm-route-generation-benchmark-reference.md)에
정리한다.

## 1. 시험 환경

| 항목 | PC | Jetson |
| --- | --- | --- |
| 장비 | 데스크톱 | Jetson Orin Nano Developer Kit |
| CPU | Intel Core i7-11700 | Cortex-A78AE 6코어 (`aarch64`) |
| GPU | GeForce RTX 4070 SUPER (VRAM 12GB) | Orin 내장 GPU (CPU와 메모리 공유) |
| 메모리 | 32GB | 8GB (프로젝트 사용 가능 약 4.5GB) |
| 저장장치 | Samsung 970 EVO Plus 1TB | 64GB SD 카드 (프로젝트 사용 가능 약 15GB) |

두 장비의 결과를 비교할 때는 Git 커밋, checkpoint, 입력, 반복 횟수와 모델 설정을
같게 유지한다. 출력 폴더 이름만 `pc-rtx4070`과 `jetson-orin`으로 구분한다.

## 2. 공통 설치

### 2.1 저장소와 Python 확인

```bash
cd ~/Logistics_FMS
git branch --show-current
git rev-parse HEAD
git status --short
python3 --version
df -h /
free -h
```

Kev는 Python 3.12 이상 3.14 미만을 요구한다. PC와 Jetson에서 같은 Python 3.12
가상환경을 사용한다.

가상환경이 없다면 한 번만 생성한다.

```bash
python3 -m venv ~/venv/robot
source ~/venv/robot/bin/activate
python -m pip install --upgrade pip setuptools wheel
```

이미 있다면 활성화만 한다.

```bash
source ~/venv/robot/bin/activate
```

### 2.2 Jetson CUDA PyTorch 설치

Jetson에서는 프로젝트 requirements보다 먼저 검증된 CUDA PyTorch를 설치한다.

```bash
source ~/venv/robot/bin/activate

python -m pip install \
  --no-cache-dir \
  --force-reinstall \
  "torch==2.8.0" \
  --index-url https://download.pytorch.org/whl/cu129
```

PC에 CUDA PyTorch가 이미 설치되어 있다면 이 단계는 생략한다.

### 2.3 프로젝트 의존성 설치

Ollama provider와 Laya 의존성은 하나의 파일에서 관리한다.

```bash
cd ~/Logistics_FMS
source ~/venv/robot/bin/activate
python -m pip install -r backend/simulation/requirements.txt
python -m pip check
```

Laya를 포함한 Python 패키지는 `backend/simulation/requirements.txt`에서 통합 관리한다.

### 2.4 환경변수 파일 생성

```bash
cd ~/Logistics_FMS
cp -n .env.example .env
```

`.env`에는 API key를 커밋하지 않는다. 백엔드와 경로 평가 도구가 이 파일을
자동으로 읽는다. 원격 Ollama를 사용할 때는 이 파일에 `OLLAMA_HOST=...`를
추가하거나 실행 터미널에서 `export OLLAMA_HOST=...`로 지정한다.

### 2.5 CUDA 실제 연산 확인

`torch.cuda.is_available()`뿐 아니라 실제 행렬 연산까지 확인한다.

```bash
python - <<'PY'
import torch

print("torch:", torch.__version__)
print("torch CUDA:", torch.version.cuda)
print("supported arch:", torch.cuda.get_arch_list())
print("CUDA available:", torch.cuda.is_available())
print("device:", torch.cuda.get_device_name(0) if torch.cuda.is_available() else "none")

a = torch.randn((1024, 1024), device="cuda")
b = torch.randn((1024, 1024), device="cuda")
c = a @ b
torch.cuda.synchronize()

print("result:", c.mean().item())
print("CUDA operation: SUCCESS")
PY
```

Jetson에서는 실행 중 다른 터미널에서 `sudo tegrastats`의 `GR3D_FREQ`도 확인한다.

## 3. 구조와 재현 조건 검증

| 고정 기준 | 값 |
| --- | --- |
| 실행 커밋 | 실행 시 `manifest.json`의 `executed_commit`에 자동 기록 |
| 그래프 | `routes/test_int.geojson` |
| 그래프 크기 | 노드 13개, 유향 간선 30개 |
| 그래프 SHA-256 | `a2a02f6d97738997206ef7866e1e70e1e2c4c1ca0377fc6cd3675544abac92db` |
| V1 Direct 프롬프트 SHA-256 | `f71b207f4ccfa32b72de15f0f09ec340ff083768d9ebff6bd29805eff8fdff72` |
| V2 Direct 프롬프트 SHA-256 | `47e58ad5ebe0f5dede1985128bb3055c800ab785a87372ce80ddf2fb25c4660c` |
| V2 CoT 프롬프트 SHA-256 | `4b540e4280ed752d645eccf27e7c52812c3cda34b0bc812685019ef15826afff` |
| V2 Iterative 프롬프트 SHA-256 | `161bc29b88a67f7d2f5f1a36358fdfaab6721aca1bb102a36e6f191ddfac0eb5` |

원본 GeoJSON과 CompactRouteGraph 입력은 단일 비교 CLI의 `--llm-graph`로 선택할 수 있다.
V1·V2 일괄 벤치마크의 입력은 현재 CompactRouteGraph 또는
CompactAdjacencyGraph만 지원하므로 원본 GeoJSON 속도 비교에는 입력 형식 지원을 추가해야 한다.
입력 크기·토큰 수·응답시간을 비교할 때는 같은 맵과 Start/Target, 모델 설정을 사용한다.

장비 비교 전 `git rev-parse HEAD`와 `git status --short`를 확인한다. 두 V2 설정의
`comparison_condition_sha256`이 같으면 Graph, 모델, Start/Target, Ground Truth와
공통 모델 설정이 동일하다는 뜻이다.

구조 검증은 모델을 호출하지 않는다.

```bash
python -m backend.simulation.evaluation.benchmark \
  --config backend/simulation/evaluation/route_generation_benchmark_jetson.json \
  --check
```

그래프 해시, 노드·간선 수, 코드 최단 경로와 기준값 중 하나라도 다르면 중단한다.
프롬프트나 설정을 바꾸면 설정 파일을 복사하고 `benchmark_id`도 새 값으로 변경한다.

## 4. Ollama 경로 생성 시험

### 4.1 Ollama 설치와 서버 확인

Linux 또는 Jetson에 Ollama가 없다면 설치한다.

```bash
curl -fsSL https://ollama.com/install.sh | sh
sudo systemctl start ollama
sudo systemctl status ollama
ollama --version
curl http://127.0.0.1:11434/api/tags
```

Jetson에서는 동시에 모델 하나와 요청 하나만 처리한다. systemd 서비스를 사용한다면
다음 환경변수를 service override에 설정하고 재시작한다.

```ini
[Service]
Environment="OLLAMA_MAX_LOADED_MODELS=1"
Environment="OLLAMA_NUM_PARALLEL=1"
```

```bash
sudo systemctl daemon-reload
sudo systemctl restart ollama
```

systemd 서비스를 사용하지 않을 때만 다음처럼 직접 실행한다.

```bash
OLLAMA_MAX_LOADED_MODELS=1 OLLAMA_NUM_PARALLEL=1 ollama serve
```

### 4.2 모델 설치

Jetson 시험은 Ollama 기본 태그 5종을 사용한다.

```bash
ollama pull qwen3:0.6b
ollama pull deepseek-coder:1.3b
ollama pull llama3.2:3b
ollama pull qwen3:4b
ollama pull gemma3:4b

ollama list
```

PC V2는 위 5종에 다음 5종을 추가해 총 10종을 사용한다.

```bash
ollama pull llama3.1:8b
ollama pull mistral-nemo:12b
ollama pull gemma3:12b
ollama pull deepseek-coder:6.7b
ollama pull phi4:14b

ollama list
```

DeepSeek-R1은 `think=false`에서도 최종 JSON 전에 출력 예산을 소진했으므로 V2 대상에서
제외하고, non-thinking 비교군인 `deepseek-coder:1.3b`와
`deepseek-coder:6.7b`를 사용한다. 두 장비에서 같은 태그를 비교할 때는
`ollama list`의 ID가 같은지 확인한다.

원격 Ollama 서버를 사용한다면 벤치마크를 실행할 터미널에서 지정한다.

```bash
export OLLAMA_HOST=http://서버주소:11434
curl "$OLLAMA_HOST/api/tags"
```

### 4.3 고정 설정

| 항목 | PC 10종 선별 | Jetson 5종 / 장비 비교 |
| --- | --- | --- |
| 설정 파일 | `route_generation_benchmark.json` | `route_generation_benchmark_jetson.json` |
| 모델 수 | 10종 | 5종 |
| `num_ctx` | 4096 | 4096 |
| 반복 | 경로별 5회 | 경로별 5회 |
| 예열 | 모델별 1회 | 모델별 1회 |

공통값은 `temperature=0`, `seed=20260928`, `num_ctx=4096`, `num_predict=512`,
요청 제한 120초, 최대 시도 1회, `keep_alive=5m`이다. DeepSeek 비교군은
non-thinking인 `deepseek-coder:1.3b`와 `deepseek-coder:6.7b`를 사용하므로
R1 전용 장문 thinking 예산을 적용하지 않는다. 0.15초는 응답 완료 후 실시간성
충족 여부를 분류하는 기준이며 요청 제한 시간이 아니다.

### 4.4 실행

Jetson:

```bash
python -m backend.simulation.evaluation.benchmark \
  --config backend/simulation/evaluation/route_generation_benchmark_jetson.json \
  --output backend/simulation/benchmark_results/jetson-$(date +%Y%m%d-%H%M%S)
```

PC 10종 선별:

```bash
python -m backend.simulation.evaluation.benchmark \
  --config backend/simulation/evaluation/route_generation_benchmark.json \
  --output backend/simulation/benchmark_results/pc-$(date +%Y%m%d-%H%M%S)
```

PC와 Jetson의 순수 장비 차이를 비교할 때는 PC에서도 Jetson 설정 파일을 사용한다.

```bash
python -m backend.simulation.evaluation.benchmark \
  --config backend/simulation/evaluation/route_generation_benchmark_jetson.json \
  --output backend/simulation/benchmark_results/pc-matched-$(date +%Y%m%d-%H%M%S)
```

두 설정은 `num_ctx=4096`으로 같지만 모델 수가 다르므로, PC 10종 전체와 Jetson 5종 전체를 직접 비교하지 않고 공통 5종만 장비 비교에 사용한다.

### 4.5 재개와 결과 재추출

중단된 Ollama 시험은 같은 설정과 결과 폴더로 재개한다.

```bash
python -m backend.simulation.evaluation.benchmark \
  --config backend/simulation/evaluation/route_generation_benchmark_jetson.json \
  --resume backend/simulation/benchmark_results/<실행 폴더>
```

모델을 다시 호출하지 않고 `trials.jsonl`에서 요약과 CSV를 재생성한다.

```bash
python -m backend.simulation.evaluation.benchmark \
  --export-results backend/simulation/benchmark_results/<실행 폴더>
```

### 4.6 DeepSeek-R1 thinking·출력 예산 진단 (PC, 본시험 제외)

과거 PC V1의 R1 1.5B·14B 결과는 각각 25/25회에서
`done_reason=length`, `eval_count=512`였다. 요청에는 `think=false`가
있었지만 응답에는 `thinking`이 있었고 최종 JSON 경로를 얻지 못했다.
이 기록은 **출력 예산 소진**을 보여주며, `think=false` 자체가 실패의
원인이라는 증거는 아니다. 과거 결과는 이전 그래프의 측정값이므로 현재
`routes/test_int.geojson` 시험과 정확도·속도를 직접 비교하지 않는다.

아래 두 설정은 V1 입력·프롬프트·모델·그래프·5개 경로가 같고,
`think`만 `false` / `true`로 다르다. 두 설정 모두
`num_predict=2048`, `num_ctx=4096`, timeout 180초,
모델별 예열 1회, 경로별 반복 1회다. R1 진단은 `selector_iterative_benchmark.json`의 활성 모델
본시험과 결과 폴더를 분리한다.

PC에서 R1 모델을 준비하고 설정을 확인한다. `--check`는 Ollama를 호출하지 않는다.

```bash
ollama pull deepseek-r1:1.5b
ollama pull deepseek-r1:14b

python -m backend.simulation.evaluation.benchmark \
  --config backend/simulation/evaluation/deepseek_r1_thinking_off_diagnostic.json --check
python -m backend.simulation.evaluation.benchmark \
  --config backend/simulation/evaluation/deepseek_r1_thinking_on_diagnostic.json --check
```

두 조건을 각각 실행한다.

```bash
python -m backend.simulation.evaluation.benchmark \
  --config backend/simulation/evaluation/deepseek_r1_thinking_off_diagnostic.json \
  --output backend/simulation/benchmark_results/pc-r1-think-off-$(date +%Y%m%d-%H%M%S)

python -m backend.simulation.evaluation.benchmark \
  --config backend/simulation/evaluation/deepseek_r1_thinking_on_diagnostic.json \
  --output backend/simulation/benchmark_results/pc-r1-think-on-$(date +%Y%m%d-%H%M%S)
```

각 결과의 `trials.jsonl`과 `model_summary.csv`를 확인한다.
`requested_think`, `thinking_observed`, `think_control_ignored`,
`done_reason`, `eval_count`, `output_budget_exhausted`로
thinking 적용 여부와 길이 제한을 판단한다. 최종 모델 평가는
JSON 성공률, 유효 경로, A* 기준 최단 경로 일치율, 응답시간으로 판단한다.
2048토큰에서도 `done_reason=length`가 반복되면 0% 정확도를 모델의
경로 계산 능력으로 단정하지 말고 출력 예산 소진으로 따로 표기한다.

## 5. Laya 경로 선택 시험

`convaiinnovations/laya-multilingual`이 코드가 만든 후보 중 하나를 선택한다. Ollaya
서버는 사용하지 않고 Hugging Face checkpoint를 PyTorch CUDA로 직접 실행한다.
최단 경로 질문은 `routes/test_int.geojson`의 새 지도와 저장된 정수 Edge `weight`를 사용하며,
V1과 같은 5개 Start/Target 각각에 대해 코드가 만든 유효 경로 후보 5개를 전달한다.
정답 최단 경로는 선택지 1~5번에 한 번씩 배치한다.

### 5.1 설정

`.env`:

```dotenv
ROUTE_SELECTOR=laya
LAYA_HF_MODEL=convaiinnovations/laya-multilingual
LAYA_DEVICE=cuda:0
LAYA_REQUIRE_CUDA=true
# V1 후보 선택 입력이 잘리지 않도록 PC와 Jetson에서 같은 값 사용
LAYA_MAX_LENGTH=4096
HF_HOME=/home/<사용자명>/.cache/huggingface
# HF_TOKEN=
```

공개 checkpoint에는 일반적으로 `HF_TOKEN`이 필요하지 않다. `LAYA_REQUIRE_CUDA=true`이면
CUDA를 사용할 수 없거나 CPU fallback이 발생했을 때 시험을 실패 처리한다.
`LAYA_MAX_LENGTH`는 글자 수가 아니라 질문 head까지 포함한 토큰 예산이다. Laya가
허용하는 범위는 1~8192이며, V1 재측정에서는 PC와 Jetson 모두 4096으로
고정한다.

### 5.2 V1 후보 선택 반복 측정

```bash
python -m backend.simulation.evaluation.selector_latency_benchmark \
  --output backend/simulation/benchmark_results/<환경>-laya-latency-$(date +%Y%m%d-%H%M%S) \
  --repeats 5 --warmups 1 --deadline 0.15

python -m backend.simulation.evaluation.selector_question_types_benchmark \
  --output backend/simulation/benchmark_results/<환경>-laya-question-types-$(date +%Y%m%d-%H%M%S) \
  --repeats 5 --warmups 1

```

`<환경>`에는 `pc-rtx4070` 또는 `jetson-orin`을 사용한다. 첫 명령은 직관·사고 질문과
실제 맵 5개 최단거리 질문을 한국어와 영어로 시험하고 정답을 다섯 선택지 위치에
옮긴다. 기본 반복 5회 기준 총 350회이며, 이 중 실제 경로 선택은 250회다. 두 번째
명령은 choice·score·noul 질문을 시험한다.

여기서 반복은 동일한 V1 후보 선택 문제를 통계 측정을 위해 5회 실행한다는 뜻이다.
다음 노드를 여러 번 선택해 하나의 경로를 만드는 V2 Iterative 시험과는 다르다.

V1 Laya 재측정 결과는 `selector_latency_trials.jsonl`에 `usage`와
`state_truncated`로 기록된다. 정상 결과라면 요약 파일에서
`state_truncated_count=0`, `state_truncation_unreported_count=0`이고,
`full_input_preserved_count`가 성공한 요청 수와 같아야 한다.
`usage.state_tokens_dropped`가 0보다 크거나 `state_truncated=true`이면 해당 실행은
V1 재측정 결과에서 제외한다.

### 5.3 V1 AllRoutes 전체 경로 선택 시험

기존 V1 시험은 코드가 고른 경로 후보 5개 중 하나를 선택한다. AllRoutes 시험은
각 Start/Target 사이의 **모든 유효 단순 유향 경로**를 생성해 한 번의 선택 문제로
전달한다. 단순 경로는 같은 노드를 두 번 방문하지 않는 경로다.

모델 입력은 다음과 같이 구성한다.

- `state`: Start/Target과 전체 `CompactRouteGraph`
- `criteria`: `path=[0, 3, 4, 6]` 형태의 전체 경로 후보
- 후보에는 거리나 정답 표시를 넣지 않음
- 모델은 CompactRouteGraph의 기존 Edge `weight` 합을 비교해 최단 경로를 선택
- 후보 순서는 언어·경로·반복마다 고정 seed로 섞고 그 seed를 결과에 기록

현재 실제 맵의 후보 수는 다음과 같다.

| Start → Target | 전체 단순 경로 수 |
| --- | ---: |
| 2 → 11 | 6 |
| 8 → 5 | 6 |
| 3 → 1 | 6 |
| 4 → 0 | 6 |
| 12 → 0 | 6 |

Laya는 그래프 입력과 최대 6개의 선택지를 모두 보존해야 하므로 AllRoutes 실행 전에
토큰 예산을 별도로 늘린다.

```bash
export LAYA_MAX_LENGTH=8192
export LAYA_HEAD_MAX_LENGTH=2048
```

`LAYA_MAX_LENGTH`는 전체 입력 예산이고, `LAYA_HEAD_MAX_LENGTH`는 선택지 head가 사용할
수 있는 최대 토큰 수다. 선택지가 잘리면 해당 실행은 유효한 비교 결과로 사용하지 않는다.

Laya 실행:

```bash
ROUTE_SELECTOR=laya \
python -m backend.simulation.evaluation.selector_all_routes_benchmark \
  --output backend/simulation/benchmark_results/<환경>-laya-all-routes-$(date +%Y%m%d-%H%M%S) \
  --repeats 5 --warmups 1 --deadline 0.15
```

Kev 벤치마크 명령만 실행해서는 안 된다. 처음 사용하는 환경에서는 다음 순서가
필수다.

1. [6.1 공통 설치](#61-공통-설치)에 따라 Kev 저장소와 서버 의존성을 설치한다.
2. 터미널 1에서 시험할 모델의 서버를 실행한다.
   - 0.8B: [6.2 Kev-0.8B 서버 실행](#62-kev-08b-서버-실행)
   - 4B: [6.4 Kev-4B 서버 실행](#64-kev-4b-서버-실행)
3. `GET /v1/models`에서 `run`, `device`, `backend`, `dtype`을 확인한다.
4. 서버 터미널을 열어 둔 상태로 터미널 2에서 벤치마크를 실행한다.

아래 예시는 Kev-0.8B 서버가 8011 포트에서 실행 중인 경우다.

```bash
cd ~/Logistics_FMS
source ~/venv/robot/bin/activate

ROUTE_SELECTOR=kev \
KEV_HOST=http://127.0.0.1:8011 \
KEV_MODEL=kev-08b \
KEV_REQUIRE_CUDA=true \
python -m backend.simulation.evaluation.selector_all_routes_benchmark \
  --output backend/simulation/benchmark_results/<환경>-kev-08b-all-routes-$(date +%Y%m%d-%H%M%S) \
  --repeats 5 --warmups 1 --deadline 0.15
```

Kev-4B를 시험할 때는 0.8B 서버를 종료하고 4B 서버를 실행한 뒤
`KEV_MODEL=kev-4b`와 결과 폴더 이름을 `kev-4b`로 변경한다. `KEV_MODEL`은 모델을
다운로드하거나 서버에 로드하는 설정이 아니라, 현재 서버의 `--run` checkpoint가
기대한 모델과 일치하는지 검증하고 결과에 모델명을 기록하는 설정이다.

Ollama 실행:

```bash
ROUTE_SELECTOR=ollama OLLAMA_MODEL=qwen3:4b \
python -m backend.simulation.evaluation.selector_all_routes_benchmark \
  --output backend/simulation/benchmark_results/<환경>-qwen3-4b-all-routes-$(date +%Y%m%d-%H%M%S) \
  --repeats 5 --warmups 1 --deadline 0.15
```

결과 폴더에는 다음 파일이 생성된다.

| 파일 | 내용 |
| --- | --- |
| `manifest.json` | 그래프·프롬프트 해시, 후보 수, seed, 모델과 장치 |
| `selector_all_routes_trials.jsonl` | 요청별 후보 순서, 선택 경로, 확률, 시간과 오류 |
| `selector_all_routes_summary.json` | 전체·언어별·경로별 정확도와 지연시간 |
| `selector_all_routes_samples.csv` | 그래프 작성용 행 단위 결과 |

정상 결과는 `error_count=0`이어야 한다. Laya는 추가로
`state_truncated_count=0`, `state_truncation_unreported_count=0`,
`option_completeness_unreported_count=0`, `incomplete_option_trial_count=0`,
Laya와 Kev는
`incomplete_probability_trial_count=0`도 확인한다. Ollama가 선택지별 확률을 반환하지
않는 경우 확률 완전성은 `null`이며 오류가 아니다. 정확도는 후보 수에 따른 무작위
기준선 `1 / candidate_count`와 함께 비교한다.

## 6. Kev 경로 선택 시험

Kev는 코드가 만든 경로 후보 중 하나를 선택하고 각 선택지의 확률을 반환한다.
`KevSelector`는 Kev 서버의 `GET /v1/models`, `POST /v1/systemone` API를
호출한다.

| 시험 모델 | checkpoint | 포트 | 대상 |
| --- | --- | ---: | --- |
| Kev-0.8B | `jaredpalmer/kev-0.8b@v1.0` | 8011 | PC·Jetson 공통 |
| Kev-4B | `jaredpalmer/kev-4b@v1.0` | 8011 | PC 추가 비교 |

> 결과 폴더 이름만으로 모델이 바뀌지는 않는다. 실제 모델은 Kev 서버 실행 명령의
> `--run`으로 결정되며, 벤치마크 전에 `/v1/models`의 `run` 값을 확인한다.
> 두 모델은 같은 8011 포트를 사용하므로 하나의 서버를 종료한 뒤 다른 서버를
> 실행한다. `KEV_MODEL`도 실행한 checkpoint에 맞게 변경해야 한다.

### 6.1 공통 설치

Kev 저장소가 없다면 한 번만 내려받는다.

```bash
test -d ~/kev || git clone https://github.com/jaredpalmer/kev.git ~/kev
```

프로젝트에서 사용하는 Python 가상환경에 Kev 서버 의존성을 설치한다.

```bash
source ~/venv/robot/bin/activate
cd ~/kev

python -m pip install --no-cache-dir -e ".[serve]"
python -m pip check
```

이미 같은 가상환경에서 Kev 서버를 정상 실행했다면 설치를 반복하지 않는다.
CUDA가 준비됐는지 확인한다.

```bash
python - <<'PY'
import torch

print("torch:", torch.__version__)
print("cuda:", torch.cuda.is_available())
print("device:", torch.cuda.get_device_name(0) if torch.cuda.is_available() else "none")
PY
```

### 6.2 Kev-0.8B 서버 실행

터미널 1에서 실행하고 서버 터미널을 계속 열어 둔다.

```bash
source ~/venv/robot/bin/activate
cd ~/kev

KEV_BACKEND=torch \
KEV_DTYPE=bf16 \
KEV_PREFIX_CACHE=0 \
KEV_CUDA_GRAPHS=0 \
KEV_FUSED=0 \
python -m kev.serve \
  --run jaredpalmer/kev-0.8b@v1.0 \
  --port 8011 \
  2>&1 | tee ~/kev-0.8b-server.log
```

첫 실행에서는 Kev checkpoint와 `Qwen/Qwen3.5-0.8B-Base`가 다운로드된다.
`Application startup complete`가 출력되면 터미널 2에서 확인한다.

```bash
curl -sS http://127.0.0.1:8011/v1/models \
  | python -m json.tool

nvidia-smi
```

다음 값이 나와야 한다.

```text
run: jaredpalmer/kev-0.8b
base: Qwen/Qwen3.5-0.8B-Base
device: cuda
backend: torch
dtype: bfloat16
```

### 6.3 Kev-0.8B 벤치마크 실행

터미널 2에서 실행한다.

```bash
cd ~/Logistics_FMS
source ~/venv/robot/bin/activate

export ROUTE_SELECTOR=kev
export KEV_HOST=http://127.0.0.1:8011
export KEV_MODEL=kev-08b
export KEV_TIMEOUT_SECONDS=60
export KEV_REQUIRE_CUDA=true

RESULT_DIR="backend/simulation/benchmark_results/pc-rtx4070-kev-0.8b-latency-$(date +%Y%m%d-%H%M%S)"

python -m backend.simulation.evaluation.selector_latency_benchmark \
  --output "$RESULT_DIR" \
  --repeats 5 \
  --warmups 1 \
  --deadline 0.15
```

0.8B의 choice·score·noul question type 벤치마크도 같은 서버 설정으로 실행한다.

```bash
QUESTION_RESULT_DIR="backend/simulation/benchmark_results/pc-rtx4070-kev-0.8b-question-types-$(date +%Y%m%d-%H%M%S)"

python -m backend.simulation.evaluation.selector_question_types_benchmark \
  --output "$QUESTION_RESULT_DIR" \
  --repeats 5 \
  --warmups 1
```

Jetson에서는 출력 폴더 이름만 다음처럼 변경한다.

```bash
RESULT_DIR="backend/simulation/benchmark_results/jetson-orin-kev-0.8b-latency-$(date +%Y%m%d-%H%M%S)"
QUESTION_RESULT_DIR="backend/simulation/benchmark_results/jetson-orin-kev-0.8b-question-types-$(date +%Y%m%d-%H%M%S)"
```

0.8B 시험이 끝나면 터미널 1에서 `Ctrl+C`로 서버를 종료한 뒤 4B 시험을
시작한다. 두 모델 서버를 동시에 실행하면 GPU 메모리가 중복 사용된다.

### 6.4 Kev-4B 서버 실행

Kev-4B는 터미널 1에서 실행한다.

```bash
source ~/venv/robot/bin/activate
cd ~/kev

KEV_BACKEND=torch \
KEV_DTYPE=bf16 \
KEV_PREFIX_CACHE=0 \
KEV_CUDA_GRAPHS=0 \
KEV_FUSED=0 \
python -m kev.serve \
  --run jaredpalmer/kev-4b@v1.0 \
  --port 8011 \
  2>&1 | tee ~/kev-4b-server.log
```

첫 실행에서는 Kev checkpoint와 `Qwen/Qwen3.5-4B-Base`가 다운로드된다.
`Application startup complete`가 출력되면 터미널 2에서 확인한다.

```bash
curl -sS http://127.0.0.1:8011/v1/models \
  | python -m json.tool

nvidia-smi
```

다음 값이 나와야 한다.

```text
run: jaredpalmer/kev-4b
base: Qwen/Qwen3.5-4B-Base
device: cuda
backend: torch
dtype: bfloat16
```

`run`이 0.8B이거나 `device`가 CPU이면 4B 벤치마크를 시작하지 않는다.

### 6.5 Kev-4B 벤치마크 실행

터미널 2에서 실행한다.

```bash
cd ~/Logistics_FMS
source ~/venv/robot/bin/activate

export ROUTE_SELECTOR=kev
export KEV_HOST=http://127.0.0.1:8011
export KEV_MODEL=kev-4b
export KEV_TIMEOUT_SECONDS=60
export KEV_REQUIRE_CUDA=true

RESULT_DIR="backend/simulation/benchmark_results/pc-rtx4070-kev-4b-latency-$(date +%Y%m%d-%H%M%S)"

python -m backend.simulation.evaluation.selector_latency_benchmark \
  --output "$RESULT_DIR" \
  --repeats 5 \
  --warmups 1 \
  --deadline 0.15
```

4B의 choice·score·noul question type 벤치마크도 같은 서버 설정으로 실행한다.

```bash
QUESTION_RESULT_DIR="backend/simulation/benchmark_results/pc-rtx4070-kev-4b-question-types-$(date +%Y%m%d-%H%M%S)"

python -m backend.simulation.evaluation.selector_question_types_benchmark \
  --output "$QUESTION_RESULT_DIR" \
  --repeats 5 \
  --warmups 1
```

`KEV_MODEL`은 결과 파일에 기록할 모델과 기대 checkpoint를 명시한다. 선택기는
`kev-08b`를 `jaredpalmer/kev-0.8b`, `kev-4b`를 `jaredpalmer/kev-4b`와
대조한다. Kev 서버 요청에는 서버가 제공하는 `kev-latest` API 별칭을 내부적으로
사용한다. 환경변수와 실제 서버의 `run`이 다르면 시험을 시작하지 않는다.

실행 중 GPU 상태는 별도 터미널에서 확인한다.

```bash
watch -n 1 nvidia-smi
```

### 6.6 결과 확인

각 벤치마크를 실행한 터미널에서 latency 결과는 `RESULT_DIR`, question type
결과는 `QUESTION_RESULT_DIR`로 확인한다.

```bash
python -m json.tool "$RESULT_DIR/manifest.json"
python -m json.tool "$RESULT_DIR/selector_latency_summary.json"

python -m json.tool "$QUESTION_RESULT_DIR/manifest.json"
python -m json.tool "$QUESTION_RESULT_DIR/selector_question_type_summary.json"
```

`manifest.json`에서 다음 값을 확인한다.

```text
status: complete
selector_name: kev
requested_model: kev-08b 또는 kev-4b
device.kind: cuda
```

결과 파일은 다음과 같다.

| 파일 | 용도 |
| --- | --- |
| `manifest.json` | 실행 설정, 선택기, 모델과 실제 장치 |
| `selector_latency_trials.jsonl` | 요청별 선택 결과, 확률, 정확도와 시간 |
| `selector_latency_summary.json` | 전체·언어·질문 유형·경로별 집계 |
| `selector_latency_samples.csv` | 그래프 작성과 PC·Jetson 비교용 표 |

결과 폴더 이름과 `/v1/models` 확인 결과를 함께 보관해야 0.8B와 4B 결과를
구분할 수 있다. Kev 모델은 영어 중심이므로 한국어와 영어 정확도를 분리해서
확인한다. CUDA 메모리 부족으로 서버 또는 벤치마크가 종료되면 해당 실행은 성능
결과로 사용하지 않는다.

## 7. 결과 파일과 해석

### 7.1 Ollama

| 파일 | 용도 |
| --- | --- |
| `manifest.json` | Git 상태, 설정·그래프·프롬프트 해시, Ollama 환경과 모델별 CPU/GPU 적재 |
| `warmups.jsonl` | 모델별 예열 결과. 본 집계에서 제외 |
| `trials.jsonl` | 입력, 원시 응답, 재계산 결과와 개별 지표 |
| `summary.json` | 모델별·경로별 집계 |
| `model_summary.csv`, `route_summary.csv` | 모델 및 경로 비교 그래프용 |
| `latency_samples.csv`, `trial_samples.csv` | 지연시간 분포와 개별 실행 분석용 |

정답 판정에는 모델이 보고한 거리를 사용하지 않는다. 반환 경로를 공통
`route_service`로 검증하고 코드가 다시 계산한 거리만 사용한다.

### 7.2 Laya·Kev

| 시험 | 생성 파일 |
| --- | --- |
| latency | `manifest.json`, `selector_latency_trials.jsonl`, `selector_latency_summary.json`, `selector_latency_samples.csv` |
| question types | `manifest.json`, `selector_question_type_trials.jsonl`, `selector_question_type_summary.json`, `selector_question_type_samples.csv` |

- latency 시험만 0.15초 충족률을 계산한다.
- question types 시험에는 `--deadline` 옵션이 없다.
- noul 응답은 현재 `confidence`와 `probabilities`를 별도로 저장하지 않는다.
- selector 시험에는 `--resume`이 없다. 중단되면 새 출력 폴더에서 다시 실행한다.
- 실행 직후 `manifest.json`의 장치는 `unknown`일 수 있으며 첫 추론 후 최종값으로 갱신된다.

두 selector 시험은 현재 OS, JetPack, 전체 Python 패키지 목록을 manifest에 저장하지
않는다. 장비 비교 결과를 보관할 때 다음 정보를 함께 저장한다.

```bash
python --version
python -m pip freeze
uname -a
cat /etc/nv_tegra_release 2>/dev/null || true
```

결과 폴더는 `.gitignore` 대상이다. 공유하거나 장비를 초기화하기 전에 별도로 백업한다.

## 8. 장비 비교 지표

| 구분 | 항목 |
| --- | --- |
| 식별 | 실행 Git 커밋, 설정 해시, `selector_name`, 요청·응답 모델 |
| 장치 | 실제 device, GPU 이름, precision, CPU fallback, 오류 수 |
| 정확도 | 전체, 경로별, 한국어·영어, 질문 유형별 정확도 |
| 시간 | wall·model latency 평균, 중앙값, p95와 0.15초 충족률 |
| 확률 | choice의 confidence와 전체 probabilities |

속도는 중앙값과 p95를 함께 보고 첫 모델 로드 시간은 예열과 분리한다. checkpoint,
입력 길이 또는 `num_ctx`가 다르면 장비 성능 비교와 별도 실험으로 분류한다.

## 9. V1과 V2 경로 생성 실험

### 9.1 구분

V1은 기존 Direct LLM 경로 생성 시험이다. V2는 같은 Graph, 모델, Start/Target,
Ground Truth와 공통 모델 설정으로 다음 6조건을 실행한다.

| V2 조건 | 모델 동작 | 추가 입력 방식 |
| --- | --- | --- |
| `direct_no_rag` | 한 번에 최종 경로 생성 | 전체 Graph |
| `direct_graph_retrieval` | 한 번에 최종 경로 생성 | 검색한 경로 관련 부분 Graph |
| `cot_no_rag` | 한 요청 안에서 단계별 계산 후 경로 생성 | 전체 Graph |
| `cot_graph_retrieval` | 한 요청 안에서 단계별 계산 후 경로 생성 | 검색한 경로 관련 부분 Graph |
| `iterative_full_graph_only` | 전체 Graph와 현재 상태를 보고 다음 Node 하나를 선택해 Target까지 반복 | 전체 Graph만 사용 |
| `iterative_neighbor_context` | 같은 입력에 현재 Node 직접 연결정보를 추가하고 Target까지 반복 | 전체 Graph + 정확 조회한 `available_edges` |

여섯 조건은 하나의 `route_generation_v2_matrix.json`에서 파생된다. 따라서 설정
파일을 여섯 개 복사하지 않고도 조건을 고정할 수 있다. V2는 PC 환경에서만
실행하며 하나의 PC matrix로 조건을 고정한다.


### 9.2 CoT와 A*의 차이

CoT는 모델이 중간 계산 단계를 따라가도록 유도하는 프롬프트 방식이고,
A*는 누적 비용 g와 남은 비용의 하한 추정치 h를 더한 f=g+h 순서로
노드를 탐색하는 최단 경로 알고리즘이다.

이 시험의 CoT 프롬프트는 A* 탐색 절차를 자연어로 설명한다.
누적 비용에 목적지까지 남은 예상 비용을 더해 가장 작은 합의 대기 노드부터 탐색한다.
좌표가 없는 입력이므로 남은 비용은 최소 남은 유향 이동 횟수에 전체 간선의 최소
weight를 곱해 추정한다. 이동 횟수는 목적지에서 들어오는 간선을 역으로 조회해 구한다.
이 추정치는 실제 남은 최단 비용을 넘지 않는 하한이다. 최소 weight가 0이거나 안전한
추정을 만들 수 없으면 0을 사용한다. 좌표나 노드 번호로 가까움을 추측하지 않는다.
더 저렴한 경로를 찾으면 비용과 이전 노드를 갱신하고, 이미 탐색한 노드도 다시 대기시킨다.
목적지를 처음 발견한 순간이 아니라 최소 합의 대기 노드로 선택했을 때 종료한다.
한 번의 API 호출 안에서 전체 탐색과 비용 합산 및 경로 복원을 수행한다.
평가 기준 경로는 저장된 `weight`를 사용하는 코드의 A*로 계산한다.
모델이 절차를 정확히 수행한다는 보장은 없으므로 최종 유향 간선과 비용을 코드로 검증한다.
이전 수식 프롬프트 및 누적 비용만 비교한 프롬프트의 결과와 새 결과는 별도 시험으로 기록한다.
출력 예산 효과를 비교할 때는 같은 새 프롬프트로 512/1024/2048을 재측정한다.
Iterative 조건은 최종 84쌍 시험과 같은 `iterative_route_runner.py` 실행 함수를
사용하되 matrix에 지정된 고정 10쌍만 5회씩 실행한다. 현재 맵에서는 최대 26단계다.
기존 5쌍을 유지하고 나머지 79쌍에서 시드 20260928로 5쌍을 중복 없이 추가했다.
10쌍은 6→5, 11→4, 8→2, 7→0, 12→3, 11→3, 5→6, 0→5, 4→3, 2→0이다.
모두 최종 84쌍에 포함되며 여섯 조건이 동일하게 사용한다.
각 쌍의 Start와 Target은 다르지만 여러 쌍 사이에서는 같은 노드를 다시 사용할 수 있다.
파생 configs의 case_selection에 선정 시드와 정책을 보존한다.
두 반복 조건 모두 전체 Graph·현재/직전 Node·Target·방문 경로·방문 횟수·사용 Edge·
누적 비용과 **이동 가능한 인접 Node criteria**를 전달한다. Neighbor Context 조건만
동일 후보의 `available_edges`를 추가한다. 모델은 `{"choice":"node_2"}`처럼 후보 ID 하나를 반환한다.

- Node별 최대 2회 방문, 같은 방향성 Edge 재사용 금지.
- 직전 Node는 다른 후보가 없을 때만 미사용 역방향 Edge로 복귀 가능.
- 후보 1개는 API 호출 없이 강제 이동하며 `forced_step=true`로 기록.
- 선택 후 실제 Edge와 이동 제한을 검증하고 Target까지 반복.
- 실패 단계·부분 경로·실제 API 호출 수·강제 이동 수·전체 시간을 보존.

이 비교의 가설은 **후보 제한과 검증을 포함한 반복 선택 시스템이 단일 호출보다 유효 도착률을
높이는가**이다. 반복 호출만의 효과를 분리한 시험은 아니다. Python은 인접 후보를 제한하고
강제 이동도 수행하지만 A* 정답으로 다음 노드를 대신 선택하지 않는다.
결론은 새 결과의 유효 도착률 → 유효 도착 중 최단 비용 일치 → 전체 소요 시간 순으로 판단한다.
10쌍 시험은 예비 비교이며, 선정 방식의 일반성을 최종 84쌍에서 확인한다.
기존 13단계·후보 없는 selected_node 방식의 결과와 새 결과를 합산하지 않는다.

### 9.3 Graph Retrieval 정의

입력의 원본은 항상 실제 프론트 Graph의 전체 Node·Edge와 Start/Target이다. V2 모델 입력은
좌표를 제거한 `CompactAdjacencyGraph`이며 경로 비용은 저장된 Edge `weight`만 사용한다.

```json
{
  "type": "CompactAdjacencyGraph",
  "directed": true,
  "nodes": [0, 1],
  "adjacency": {
    "0": [{"to": 1, "weight": 0.352}],
    "1": [{"to": 0, "weight": 0.223}]
  }
}
```

반대 방향 Edge는 별도 항목이며 서로 다른 weight를 그대로 유지한다. Direct·CoT의
Retrieval 적용 조건에서는 LLM 호출 전에 다음 조회를 한 번 수행한다.

1. Start에서 방향성 Edge를 따라 도달 가능한 Node를 구한다.
2. 역방향으로 Target에 도달할 수 있는 Node를 구한다.
3. 두 집합의 교집합과 그 사이 Edge를 조회한다.
4. 각 기준 Node에 대해 인접 Node와 저장된 `weight`만 adjacency에 유지한다.

Retrieval은 최단 경로나 최단 후보를 계산하지 않는다. 따라서 모델 입력에는
`shortest_path`가 없고 다음과 같은 연결 정보만 들어간다.

```json
"1": [
  {"to": 0, "weight": 0.3777115936682494},
  {"to": 2, "weight": 0.41118600261748245}
]
```

현재 Graph는 작고 연결성이 높아 Retrieval 결과가 전체 Graph와 같을 수도 있다.
이 경우 정확도 향상 여부와 함께 입력 표현을 adjacency로 명확히 한 효과를 측정한다.
Retrieval이 Ground Truth Edge를 누락하면 모델 실패와 구분해 기록한다.

Direct·CoT에는 경로 관련 부분 Graph를 만드는 Graph Retrieval 조건이 있다.
Iterative에는 Vector RAG나 부분 Graph Retrieval을 사용하지 않는다. Node ID와 방향성
Edge는 값이 정확히 정의된 구조화 데이터이므로 임베딩 유사도로 찾는 것보다
`adjacency[current_node]`를 조회하는 편이 정확하고 빠르다. 이 조건을
`iterative_neighbor_context`라고 부른다.

매 단계 전체 Graph와 상태는 그대로 보내고, Python이 현재 Node에서 이동 가능한
순환 제한을 만족하는 Edge를 정확히 조회해 다음 형태로 추가한다.

```json
"available_edges": [
  {"node": 1, "weight": 0.3777115936682494},
  {"node": 18, "weight": 0.428476667276754}
]
```

두 Iterative 조건의 입력 차이는 `available_edges` 유무뿐이다.

```text
iterative_full_graph_only
= 전체 Graph + current/previous + target + 방문/사용 Edge 상태 + 누적 비용 + 인접 criteria

iterative_neighbor_context
= 같은 입력 + adjacency[current_node]에서 정확히 조회한 available_edges
```

Vector RAG는 자연어 장애물 보고, 안전 정책, 운송 규칙, 과거 운행 사례처럼 ID로
정확히 조회하기 어려운 비정형 정보를 의미 유사도로 검색할 때 별도 적용한다.

### 9.4 V2 6조건 실행

Ollama를 호출하지 않고 행렬 구조와 Ground Truth를 먼저 확인한다.

```bash
python -m backend.simulation.evaluation.v2_benchmark \
  --matrix backend/simulation/evaluation/route_generation_v2_matrix.json \
  --check
```

최종 비교의 Ollama 3종을 고정 10쌍 × 5회 × 6조건으로 실행한다(총 900회).

```bash
python -m backend.simulation.evaluation.v2_benchmark \
  --matrix backend/simulation/evaluation/route_generation_v2_matrix.json \
  --model gemma3:12b --model deepseek-coder:6.7b --model phi4:14b \
  --output "backend/simulation/benchmark_results/pc-v2-aligned-10pairs-$(date +%Y%m%d-%H%M%S)"
```

V2는 Jetson에서 실행하지 않는다. PC 결과만 V2 비교 자료로 사용한다.

### CoT 출력 예산 확대 재시험

기존 512토큰 결과를 보존한 채 CoT 두 조건만 1024/2048토큰으로 실행한다.
모델·맵·경로·프롬프트·num_ctx=4096은 유지한다. 아래 명령은 timeout=120초도 유지하여
출력 예산만 변경한다. 실행별 3모델 × 2조건 × 10쌍 × 5회 = 300회다.

```bash
for budget in 1024 2048; do
  python -m backend.simulation.evaluation.v2_benchmark \
    --matrix backend/simulation/evaluation/route_generation_v2_matrix.json \
    --only cot_no_rag --only cot_graph_retrieval \
    --model gemma3:12b --model deepseek-coder:6.7b --model phi4:14b \
    --num-predict "$budget" \
    --output "backend/simulation/benchmark_results/pc-v2-cot-budget-${budget}-$(date +%Y%m%d-%H%M%S)" || break
done
```

긴 출력이 120초 timeout에 걸리면, 정확도 진단용 별도 실행에서는
`--timeout-seconds 600`을 추가한다. 이때 512/1024/2048을 모두 같은 600초로
측정해야 출력 예산 효과를 비교할 수 있다. 이전 512/120초 결과와는 두 설정이 다르다.
num_ctx는 입력과 출력을 함께 수용하므로 실제 prompt_eval_count를 확인한다.
num_predict < num_ctx 검사는 최소 검사일 뿐 전체 입력 보존을 보장하지 않는다.

`--check --num-predict 2048`로 모델 호출 없이 설정을 확인할 수 있다.
--check는 기존 동작대로 6조건 전체를 검사하며 --only/--model 필터는 실행 때 적용한다.
변경 값은 matrix_manifest.json의 settings_overrides와 각 조건 manifest.json의
settings에 기록되고, configs/*.json과 비교 조건 해시에도 반영된다.
유효 도착·최단 비용 일치·output_budget_exhaustion_rate·timeout_rate·시간을 함께 비교한다.
출력 완료가 계산 정확성의 보장은 아니다.

### 9.5 결과 확인

V2 최상위 폴더에는 `matrix_manifest.json`, 파생 설정 6개와 조건별 결과 폴더가
생긴다. 각 조건 폴더의 주요 파일은 다음과 같다.

| 파일 | 확인 내용 |
| --- | --- |
| `manifest.json` | Graph·Prompt·공통 조건 해시, 모델별 실제 설정과 장치 |
| `model_summary.csv` | 정확도, 응답시간, API 호출 수, 입출력 토큰 수 |
| `route_summary.csv` | Start/Target별 정확도와 시간 |
| `trials.jsonl` | 최종 경로, raw 진단, retrieval 정보, iterative 단계 |

추가 Retrieval 지표:

| 지표 | 의미 |
| --- | --- |
| `retrieved_node_count` | 모델에 전달된 Node 수 |
| `retrieved_edge_count` | 모델에 전달된 Edge 수 |
| `ground_truth_node_recall` | 정답 경로 Node 포함률 |
| `ground_truth_edge_recall` | 정답 경로 Edge 포함률 |
| `ground_truth_path_available` | Retrieval Graph에 정답 경로 전체가 존재하는지 |

두 조건을 통합 비교한다.

```bash
V2_DIR=backend/simulation/benchmark_results/<pc-v2-결과폴더>

python -m backend.simulation.evaluation.compare_strategy_results \
  --generation DirectNoRAG="$V2_DIR/direct_no_rag" \
  --generation DirectGraphRetrieval="$V2_DIR/direct_graph_retrieval" \
  --generation CoTNoRAG="$V2_DIR/cot_no_rag" \
  --generation CoTGraphRetrieval="$V2_DIR/cot_graph_retrieval" \
  --generation IterativeFullGraph="$V2_DIR/iterative_full_graph_only" \
  --generation IterativeNeighborContext="$V2_DIR/iterative_neighbor_context" \
  --output "$V2_DIR/comparison"
```

비교할 핵심 값은 최단 경로·거리 일치율, 총 응답시간, 실제 입력·출력 토큰,
API 호출 횟수, timeout, 출력 예산 소진율과 Retrieval coverage다.
반복 결과의 trials.jsonl에서 metrics.forced_step_count와 llm.chain_steps도 확인한다.
configs/*.json 및 manifest의 settings.iterative_policy로 새 반복 규칙을 구분한다.
512토큰으로 CoT 출력이 잘리면 그 결과만으로 기법이 열등하다고 결론내리지 않는다.
출력 예산을 바꾼 비교는 모든 조건에 같은 --num-predict 값을 적용해 별도 저장한다.

### 9.6 Ollama·Laya·Kev 공통 다음 노드 반복 선택

모델 종류가 달라도 같은 선택 작업으로 비교할 때 사용한다. 모든 모델은 동일한
`CompactAdjacencyGraph`, 현재 Node, 목표 Node, 방문 Node와 누적 거리를 받는다.
`--graph`로 사용할 GeoJSON을 명시하며, 정수 weight 시험은
`routes/test_int.geojson`을 사용한다. 실제 경로 비용은 좌표로 다시 계산하지 않고 저장된
`weight`를 그대로 사용한다.

경로 생성 방식은 두 가지다.

- `--case-mode random`: 기본 5개 Start→Docking 경로를 만든다. Target은 도킹 Node
  `0~6`, Start는 Target 집합과 겹치지 않으며 유효한 방향성 경로만 사용한다.
- `--case-mode all-pairs`: 전체 Node에서 도킹 Node `0~6`으로 갈 수 있는 모든 조합을
  사용한다. 현재 13 Node 맵에서는 Start와 Target이 같은 7쌍을 제외한 84쌍이다.
- random 모드에서 `--route-seed`를 생략하면 실행마다 새 seed를 만들고 manifest에
  기록한다. 모델·Context 비교에는 반드시 첫 실행과 같은 seed를 재사용한다.

다음 노드 선택 규칙은 아래와 같다.

- 기본값은 `--candidate-scope neighbors --context-mode neighbor_context`다.
- `neighbors`는 현재 Node에서 실제로 나가는 `available_edges`의 Node만 criteria로 준다.
- `available_edges`는 `node`와 저장된 `weight`를 가진다. 벡터 유사도 검색이 아니라
  `adjacency[current_node]`의 정확 조회 결과다.
- Node 재방문은 유효한 도착을 위한 복구 수단으로 허용한다.
- 같은 방향성 Edge는 한 번만 사용할 수 있어 삼각형 이상의 순환도 반복할 수 없다.
- Node별 방문은 최대 2회다.
- 직전 Node는 일반 후보에서 제외하지만 다른 후보가 하나도 없으면 아직 사용하지 않은
  역방향 Edge를 통한 한 단계 backtrack을 허용한다.
- 후보가 하나면 모델을 호출하지 않고 강제 이동하며 `forced_step=true`로 기록한다.
- 기본 최대 이동 횟수는 Node 수의 두 배다. 현재 맵에서는 26회다.

#### 동일 조건 예비 시험

첫 실행에서 seed를 하나 만든다.

```bash
ROUTE_SELECTOR=laya \
python -m backend.simulation.evaluation.selector_iterative_route_benchmark \
  --graph routes/test_int.geojson \
  --case-mode random \
  --candidate-scope neighbors \
  --context-mode full_graph_only \
  --output backend/simulation/benchmark_results/laya-full-graph-$(date +%Y%m%d-%H%M%S) \
  --repeats 5 --warmups 1 --deadline 0.15 --case-count 5
```

생성된 seed를 확인한다.

```bash
python -c 'import json; print(json.load(open("<첫-결과폴더>/manifest.json"))["settings"]["route_seed"])'
```

이후 Laya·Kev·Ollama와 두 Context 조건에 모두 같은 값을 지정한다.

```bash
--graph routes/test_int.geojson \
--case-mode random \
--route-seed <첫-실행-seed>
```

Neighbor Context 조건은 다음처럼 실행한다. `--output`은 생략할 수 있으며, 생략하면
selector와 model 이름을 포함한 폴더가 자동 생성된다.

```bash
ROUTE_SELECTOR=laya \
python -m backend.simulation.evaluation.selector_iterative_route_benchmark \
  --graph routes/test_int.geojson \
  --case-mode random \
  --route-seed <첫-실행-seed> \
  --candidate-scope neighbors \
  --context-mode neighbor_context \
  --repeats 5 --warmups 1 --deadline 0.15 --case-count 5
```

Kev는 서버의 checkpoint와 `KEV_MODEL`을 일치시킨다.

```bash
# Kev-0.8B 서버 실행 중
ROUTE_SELECTOR=kev KEV_HOST=http://127.0.0.1:8011 KEV_MODEL=kev-08b \
python -m backend.simulation.evaluation.selector_iterative_route_benchmark \
  --graph routes/test_int.geojson --case-mode random \
  --route-seed <첫-실행-seed> \
  --candidate-scope neighbors --context-mode neighbor_context \
  --repeats 5 --warmups 1 --deadline 0.15

# 서버를 Kev-4B checkpoint로 교체한 뒤 실행
ROUTE_SELECTOR=kev KEV_HOST=http://127.0.0.1:8011 KEV_MODEL=kev-4b \
python -m backend.simulation.evaluation.selector_iterative_route_benchmark \
  --graph routes/test_int.geojson --case-mode random \
  --route-seed <첫-실행-seed> \
  --candidate-scope neighbors --context-mode neighbor_context \
  --repeats 5 --warmups 1 --deadline 0.15
```

Ollama 단일 모델은 환경 변수 방식으로도 실행할 수 있다.

```bash
ROUTE_SELECTOR=ollama OLLAMA_MODEL=qwen3:4b \
OLLAMA_HOST=http://127.0.0.1:11434 \
python -m backend.simulation.evaluation.selector_iterative_route_benchmark \
  --graph routes/test_int.geojson --case-mode random \
  --route-seed <첫-실행-seed> \
  --candidate-scope neighbors --context-mode neighbor_context \
  --repeats 5 --warmups 1 --deadline 0.15
```

Ollama 여러 모델의 목록과 공통·모델별 파라미터는
`backend/simulation/evaluation/selector_iterative_benchmark.json`에서 관리한다.
`ollama_defaults`에는 공통 host, timeout, keep-alive, temperature, seed,
`num_ctx`, `num_predict`를 두고, 각 `models` 항목은 `enabled`, `think`,
`options`로 필요한 값만 덮어쓴다. 실행 전에는 Ollama 요청 없이 JSON, Graph 파일,
Graph SHA-256과 선택 모델을 검사한다.

```bash
python -m backend.simulation.evaluation.selector_iterative_route_benchmark \
  --config backend/simulation/evaluation/selector_iterative_benchmark.json \
  --check
```

한 모델만 실행한다.

```bash
python -m backend.simulation.evaluation.selector_iterative_route_benchmark \
  --config backend/simulation/evaluation/selector_iterative_benchmark.json \
  --model qwen3:4b \
  --output backend/simulation/benchmark_results/pc-qwen3-4b-iterative-$(date +%Y%m%d-%H%M%S)
```

여러 모델만 골라 같은 Graph, 경로, 반복 조건으로 순서대로 실행한다. `--model`은
반복해서 사용한다.

```bash
python -m backend.simulation.evaluation.selector_iterative_route_benchmark \
  --config backend/simulation/evaluation/selector_iterative_benchmark.json \
  --model qwen3:0.6b \
  --model qwen3:4b \
  --model phi4:14b \
  --output backend/simulation/benchmark_results/pc-selected-models-$(date +%Y%m%d-%H%M%S)
```

`--model`을 생략하면 JSON에서 `enabled: true`인 모델을 모두 실행한다. 각 모델 결과는
상위 결과 폴더 아래 모델명별 하위 폴더에 저장되고, 전체 실행 상태는
`selector_iterative_matrix_manifest.json`에 기록된다. JSON 설정 모드에서는 Graph,
case, seed, 반복 횟수, Context 조건을 JSON 값으로 통일하므로 같은 명령에 별도
`--graph`, `--repeats` 등을 섞지 않는다.

#### 최종 비교 시험

코드와 프롬프트가 확정된 뒤에는 표본 운에 좌우되지 않도록 `all-pairs`로 한 번만 최종
측정한다. all-pairs에는 seed를 사용하지 않는다.

```bash
ROUTE_SELECTOR=<laya|kev|ollama> \
python -m backend.simulation.evaluation.selector_iterative_route_benchmark \
  --graph routes/test_int.geojson \
  --case-mode all-pairs \
  --candidate-scope neighbors \
  --context-mode neighbor_context \
  --repeats 5 --warmups 1 --deadline 0.15
```

실행 전에 selector runtime을 검사한다. Kev 서버 미연결·checkpoint 불일치, Laya 로드
실패, Ollama 모델 미설치는 trial 실패로 섞지 않고 manifest의 `status=failed`,
`failure_stage=selector_preflight`로 기록한 뒤 실행을 중단한다.

결과 판독 시 다음 값을 함께 본다.

| 값 | 의미 |
| --- | --- |
| `reached_target_count` | 유효한 방향성 Edge만 지나 Target에 도착한 횟수 |
| `non_revisiting_valid_path_count` | 재방문 없이 Target에 도착한 횟수 |
| `revisit_trial_count` | 한 번 이상 과거 Node를 다시 방문한 trial 수 |
| `backtrack_step_count` | 후보가 없어 미사용 역방향 Edge로 복귀한 단계 수 |
| `cycle_prevented_count` | Edge 재사용·방문 상한·직전 Node 규칙으로 제외한 후보 수 |
| `max_node_visit_count` | 한 Node의 최대 방문 횟수 |
| `mean_step_excess` | Ground Truth보다 추가로 이동한 평균 단계 수 |
| `exact_path_match_count` | Ground Truth 경로 완전 일치 횟수 |
| `distance_match_count` | Ground Truth 최단 거리 일치 횟수 |
| `mean_distance_ratio` | 선택 경로 거리 ÷ 최단 거리 |

`valid_path`는 “도착 가능한 유효 Edge 경로”를 뜻한다. 최단 경로 품질은
`exact_path_match`, `distance_match`, `distance_ratio`, `step_excess`로 따로 판단한다.
manifest에는 Graph 경로·SHA-256, 생성된 Start/Target, prompt 원문·SHA-256·버전,
selector·model·device가 기록된다.

## 이전 프로토콜: 운영 context 선택과 4조건 수동 비교

운영 경로 생성과 비교 시험의 context 선택은 분리한다.
운영의 KEV·Laya·Ollama는 모두 아래 JSON 설정으로 입력을 선택한다.

파일: `backend/simulation/evaluation/selector_iterative_benchmark.json`

```json
"route_context": {
  "kev": "minimal_distance",
  "laya": "minimal_distance",
  "ollama": "minimal_distance"
}
```

`minimal_distance`는 전체 그래프·현재·목적지·허용 후보와 질문에 후보별
`dist_to_target`을 추가한다. 이력 7개와 방위·접근량·다른 로봇 context는
모델 입력에 넣지 않으며, 방문·간선 제약은 코드에서 유지한다.
점유를 반영한 제한 그래프를 주는 MAPF 우회·복구의 실행 제약은 별도로 유지한다.
이미 실행 중인 백엔드는 변경 코드가 로드되도록 다시 시작한다.
그 후 운영 JSON의 context 선택은 새 preview 요청마다 읽는다.

비교 시험은 `route_context`를 따르지 않고 네 조건을 명시적으로 선택한다.

| 옵션 | 입력 |
| --- | --- |
| `legacy` | 필수 입력 + 이력 7개 |
| `minimal` | 필수 입력만 |
| `minimal_distance` | 필수 입력 + 후보→목적지 직선거리 |
| `legacy_compass_distance` | 기존 기본 + 현재/후보 방위·거리·접근량 |

### 실행 순서

아래 스크립트는 JSON의 `graph`와 `settings`를 읽는다.
현재 설정은 `test_int.geojson`, `all-pairs`, `repeats=5`, `warmups=1`이다.
따라서 모델당 4조건 × 84쌍 × 5회 = 1,680건이다.
임시 함수에 `--repeats 1`이 남아 있는 이전 명령 대신 아래 스크립트를 사용한다.

1. WSL 시험 터미널에서 준비하고 호출 없는 명령 검사를 실행한다.

```bash
cd ~/Logistics_AI
source ~/venv/robot/bin/activate
export CONTEXT_OUT="backend/simulation/benchmark_results/context4-$(date +%Y%m%d-%H%M%S)"

bash scripts/run_context_comparison.sh --check kev kev-08b "$CONTEXT_OUT"
bash scripts/run_context_comparison.sh --check ollama gemma3:1b "$CONTEXT_OUT"
```

2. 별도 WSL 서버 터미널에서 KEV 0.8B를 실행한다.
   이미 같은 checkpoint 서버가 준비되어 있으면 중복 실행하지 않는다.

```bash
source ~/venv/robot/bin/activate
cd ~/kev
KEV_BACKEND=torch KEV_DTYPE=bf16 KEV_PREFIX_CACHE=0 \
KEV_CUDA_GRAPHS=0 KEV_FUSED=0 KEV_TRUNCATE_STATES=0 \
python -m kev.serve --run jaredpalmer/kev-0.8b@v1.0 --host 127.0.0.1 --port 8011
```

3. 시험 터미널에서 KEV 네 조건을 실행한다.

```bash
export KEV_HOST=http://127.0.0.1:8011
bash scripts/run_context_comparison.sh kev kev-08b "$CONTEXT_OUT"
```

4. KEV 완료 후 서버 터미널에서 Ctrl+C로 종료해 GPU 메모리를 비운다.
   이후 시험 터미널에서 Laya를 하나씩 실행한다.

```bash
bash scripts/run_context_comparison.sh laya convaiinnovations/laya "$CONTEXT_OUT"
bash scripts/run_context_comparison.sh laya convaiinnovations/laya-multilingual "$CONTEXT_OUT"
```

Laya는 GPU 실행과 입력 한도 8192를 사용한다. 모델을 불러올 때 시간이 필요할 수 있다.
KEV-4B는 현재 RTX 4050 6GB에서 동일 bf16 전체 GPU 조건으로 실행하지 않는다.
메모리가 충분한 별도 장비에서 4B checkpoint 서버를 준비한 뒤 모델 인자를
`kev-4b`로 바꿔 같은 스크립트를 실행한다. 환경 변수만 바꿔도 서버 모델이 교체되는 것은 아니다.

5. Ollama 설치 목록을 확인하고 네 활성 모델을 순차로 실행한다.

```bash
ollama list
for model in gemma3:1b deepseek-coder:6.7b gemma3:12b phi4:14b; do
  bash scripts/run_context_comparison.sh ollama "$model" "$CONTEXT_OUT" || break
  ollama stop "$model"
done
```

Ollama는 JSON의 공통·모델별 옵션과 `enabled` 검사를 사용한다.
활성화하지 않은 모델은 설정의 `models`에서 등록/활성화해야 한다.

### 결과 확인

결과 루트는 위 `CONTEXT_OUT`이다. 모델·context별로 새 결과를 저장한다.
Ollama는 다중 모델 실행기의 모델별 하위 폴더가 한 단계 더 생긴다.
기존 결과를 덮어쓰거나 새 반복으로 재사용하지 않도록 매 실행에 새 루트를 사용한다.

- `manifest.json`: 지도 SHA, 지시문 SHA, 실제 반복 수, `settings.state_profile`.
- `selector_iterative_route_summary.json`: 도착·최단거리 일치·시간 집계.
- `selector_iterative_route_trials.jsonl`: 경로별·단계별 선택과 실패.
- `steps[].model_input`: 실제 selector에 전달한 state·지시문·후보.
  단일 후보 자동 이동은 모델을 호출하지 않으므로 이 필드가 없다.
- `selector_iterative_route_samples.csv`: 표로 확인할 경로 결과.

```bash
find "$CONTEXT_OUT" -name selector_iterative_route_summary.json
```

비교할 때는 조건별 도착·최단 일치의 건수와 비율, 결정 평균·p95,
경로 생성 시간을 확인한다. 5회 반복을 서로 다른 420개 경로로 해석하지 않는다.
서로 다른 출발·목적지 조합은 84쌍이며 각 조합을 반복 확인한 것이다.


## 이전 프로토콜: 필수 입력 + 방위·접근량 단독 시험

추가 조건 이름은 `minimal_compass_progress`이다. 필수 입력을 고정하고 방위·접근량의
효과를 확인한다. 기존 기본의 이력 7개와 직선거리 필드는 모델 입력에서 제외한다.
기존 4조건과 같은 지도·후보 제한·지시문·측정 방식을 사용한다.
`minimal_distance`와 비교하면 거리 힌트와 방위·접근량 묶음의 차이를 볼 수 있다.
방위와 접근량 각각의 효과를 분리한 시험은 아니다.

### 입력 필드와 계산

| 위치 | 필드 | 자료형·의미 |
| --- | --- | --- |
| state | route_graph | 좌표 없는 전체 CompactAdjacencyGraph 객체 |
| state | current_node | 현재 노드 정수 ID |
| state | target_node | 목적지 노드 정수 ID |
| state | available_edges | 현재 코드가 허용한 인접 후보 배열 |
| state | target_direction | 현재→목적지의 8방위 문자열 |
| 후보 | node | 후보 노드 정수 ID |
| 후보 | weight | 현재→후보의 저장된 간선 가중치; 정수 값은 int, 소수 값은 float |
| 후보 | direction | 현재→후보의 8방위 문자열 |
| 후보 | progress | 현재→목적지 직선거리 − 후보→목적지 직선거리, 소수점 4자리 반올림 |

8방위는 지도 좌표 +x=E, +y=N을 기준으로 `E, NE, N, NW, W, SW, S, SE`이며,
좌표가 같으면 `HERE`이다. `progress`는 양수면 가까워지고 음수면 멀어진다.
계산에 사용하는 좌표는 반올림하지 않고, 최종 차이만 `round(..., 4)`로 반올림한다.
JSON 숫자이므로 `0.5000`처럼 고정 소수 자리수를 보장하지 않는다.
직선거리로부터 계산한 접근량은 실제 통로의 남은 최단거리나 도달 가능성 판정이 아니다.

`dist_to_target`, `target_distance`, 이력 7개, 다른 로봇의 점유·예약·잔여 경로,
ETA·속도는 이 조건의 모델 state에 넣지 않는다. 좌표는 계산에만 사용한다.
방문·간선 재사용·즉시 되돌아가기 제한은 코드에서 계속 적용한다.
필요한 현재·후보·목적지 좌표가 없으면 오류로 처리한다.

예: `test_int.geojson`에서 출발 10, 목적지 1인 첫 단계의 후보 부분.
전체 `route_graph`와 공통 질문·지시문은 기존과 동일하게 함께 전달된다.

```json
{
  "current_node": 10,
  "target_node": 1,
  "target_direction": "SW",
  "available_edges": [
    {"node": 0, "weight": 51, "direction": "W", "progress": 0.2555},
    {"node": 8, "weight": 46, "direction": "E", "progress": -0.422},
    {"node": 9, "weight": 38, "direction": "S", "progress": 0.1244}
  ]
}
```

### 이 조건만 실행하는 순서

명령은 수정된 코드가 반영된 실험 PC의 체크아웃에서 실행한다.
아래는 사용 중인 `~/E1I6/Logistics_AI` 경로의 예시다.
GPU 모델을 여러 개 동시에 실행하지 말고, 각 단계 완료 후 다음으로 진행한다.
JSON `settings`가 all-pairs·repeats=5이면 모델당 84쌍 × 5회 = 420건이다.
운영 `route_context`나 JSON의 기존 실험 설정을 바꿀 필요는 없다.

1. 시험 터미널 준비 및 호출 없는 검사.

```bash
cd ~/E1I6/Logistics_AI
source ~/venv/robot/bin/activate
export COMPASS_OUT="backend/simulation/benchmark_results/minimal-compass-progress-$(date +%Y%m%d-%H%M%S)"

python -m backend.simulation.evaluation.selector_iterative_route_benchmark \
  --config backend/simulation/evaluation/selector_iterative_benchmark.json \
  --state-profile minimal_compass_progress --check
```

2. JSON `models`에서 enabled=true인 Ollama 모델을 모두 실행.
   수동으로 모델명을 나열하지 않는다. `decision_models`의 KEV·Laya는 별도 실행한다.

```bash
ROUTE_SELECTOR=ollama \
python -m backend.simulation.evaluation.selector_iterative_route_benchmark \
  --config backend/simulation/evaluation/selector_iterative_benchmark.json \
  --state-profile minimal_compass_progress \
  --output "$COMPASS_OUT/ollama"
```

3. 사용한 Ollama 모델을 내린 뒤 Laya 두 모델을 순서대로 실행.

```bash
ollama ps
# 위 목록에서 GPU를 사용 중인 모델은 ollama stop 모델명으로 종료한다.
bash scripts/run_context_comparison.sh --state-profile minimal_compass_progress \
  laya convaiinnovations/laya "$COMPASS_OUT"
bash scripts/run_context_comparison.sh --state-profile minimal_compass_progress \
  laya convaiinnovations/laya-multilingual "$COMPASS_OUT"
```

4. KEV는 별도 서버 터미널에서 checkpoint를 먼저 준비한 뒤 실행.
   0.8B와 4B는 서버를 교체하고 한 모델씩 시험한다.

서버 터미널 (0.8B):

```bash
source ~/venv/robot/bin/activate
cd ~/kev
KEV_BACKEND=torch KEV_DTYPE=bf16 KEV_PREFIX_CACHE=0 \
KEV_CUDA_GRAPHS=0 KEV_FUSED=0 KEV_TRUNCATE_STATES=0 \
python -m kev.serve --run jaredpalmer/kev-0.8b@v1.0 --host 127.0.0.1 --port 8011
```

시험 터미널:

```bash
export KEV_HOST=http://127.0.0.1:8011
bash scripts/run_context_comparison.sh --state-profile minimal_compass_progress \
  kev kev-08b "$COMPASS_OUT"
```

0.8B 시험 완료 후 서버를 Ctrl+C로 종료하고, 같은 서버 명령의 `--run`을
`jaredpalmer/kev-4b@v1.0`으로 교체하여 다시 실행한다. 서버 준비 후 시험 터미널:

```bash
bash scripts/run_context_comparison.sh --state-profile minimal_compass_progress \
  kev kev-4b "$COMPASS_OUT"
```

환경 변수만 바꿔서는 서버 checkpoint가 바뀌지 않는다.
같은 bf16 GPU 조건을 유지할 수 있는 메모리가 필요하다.

### 옵션·결과 확인

- `--state-profile minimal_compass_progress`를 주면 새 조건 하나만 실행한다.
- 옵션을 생략하면 기존 4조건만 실행한다. 기존 비교를 다시 돌릴 필요는 없다.
- `--check`를 함께 주면 실행할 명령만 출력하고 모델을 호출하지 않는다.
- 존재하는 결과 폴더는 덮어쓰지 않는다. 재실행할 때는 새로운 COMPASS_OUT을 만든다.
- 실제 측정 입력은 trials JSONL의 `steps[].model_input`에 기록한다.
  질문·후보·state가 기록되며, 모델별 토큰 인코딩 이전의 공통 입력이다.
  후보가 하나인 강제 이동 단계에는 모델 호출과 model_input 기록이 없다.
- 도착·최단거리 일치·추론 시간은 기존 summary·CSV와 동일한 형식으로 저장한다.

```bash
find "$COMPASS_OUT" -name selector_iterative_route_summary.json
```


## 8조건 전체 재수집 (factorial8-v1)

### 설계 검증과 해석 범위

필수 입력을 고정하고 H=경로 이력, D=후보 직선거리, G=방위·접근량 묶음의
포함 여부를 모두 조합한다. 세 요소 각각이 포함/제외이므로 2³=8조건이다.
이는 [NIST의 3요인·2수준 완전요인 설계](https://www.itl.nist.gov/div898/handbook/pri/section3/pri3331.htm)에
해당한다. 다른 두 요소를 고정한 네 비교에서 각 요소의 효과를 확인할 수 있고,
정보 조합에 따라 효과가 달라지는지도 확인할 수 있다.

다만 독립적인 환경 정보 세 가지를 비교한다는 뜻은 아니다.
접근량은 직선거리로부터 계산되고, 같은 현재 노드에서는 후보 거리와 반대 순서다.
D+G에서는 dist_to_target+progress로 현재→목적지 거리도 대략 복원할 수 있다.
따라서 이 시험은 **수치·방위·이력을 어떤 표현으로 주었을 때 모델의 선택이 달라지는가**를
평가한다. 방위와 접근량 각각의 효과는 이 설계에서 분리하지 않는다.
그 두 요소까지 따로 나누려면 4요인 16조건이 필요하며 이번 범위에는 포함하지 않는다.

| 번호 | 조건 | profile | H | D | G |
| --- | --- | --- | --- | --- | --- |
| 1 | 필수 | minimal | 0 | 0 | 0 |
| 2 | 기존(필수+이력) | legacy | 1 | 0 | 0 |
| 3 | 필수+직선거리 | minimal_distance | 0 | 1 | 0 |
| 4 | 필수+방위·접근량 | minimal_compass_progress | 0 | 0 | 1 |
| 5 | 필수+직선거리+방위·접근량 | minimal_compass_distance | 0 | 1 | 1 |
| 6 | 기존+직선거리 | legacy_distance | 1 | 1 | 0 |
| 7 | 기존+방위·접근량 | legacy_compass_progress | 1 | 0 | 1 |
| 8 | 기존+직선거리+방위·접근량 | legacy_compass_distance | 1 | 1 | 1 |

### 최종 모델 입력 계약

필수 state는 `route_graph`, `current_node`, `target_node`, `available_edges`다.
모든 조건에서 choice 질문·선택지·공통 경로 지시문도 동일하게 제공한다.
후보는 같은 방문·간선 제약을 적용해 구성한다. 단일 후보 단계는 모델 호출 없이 이동한다.

- H=1: previous_node, visited_nodes, visit_counts, max_visits_per_node,
  used_edges, backtrack_allowed, accumulated_distance의 7개를 추가한다.
- D=1: 후보마다 `dist_to_target=round(math.dist(후보, 목적지), 4)`만 추가한다.
- G=1: state의 `target_direction`, 후보의 `direction`, `progress`만 추가한다.
  `progress=round(현재→목적지 직선거리-후보→목적지 직선거리, 4)`다.
- 현재→목적지 거리 `target_distance`는 **8조건 모두에서 제외**한다.
  G에서 필요한 현재 거리는 내부 계산에만 사용한다.
- 방위는 +x=E, +y=N의 8방위이고 좌표가 같으면 HERE다.
  좌표와 직선거리 차이는 내부에서 원래 정밀도로 계산하고 최종 값만 반올림한다.
- 노드 ID는 정수다. adjacency의 키와 criteria의 node_N은 JSON 문자열이다.
  간선 weight는 원본 저장값이며 정수 값은 int, 소수 값은 float를 유지한다.
- 다른 로봇의 점유·예약·잔여 경로, 속도·ETA는 모든 조건에서 제외한다.
- 전체 그래프는 좌표 없는 CompactAdjacencyGraph다. 좌표는 거리·방위 계산에만 쓴다.

기존 `legacy_compass_distance`는 target_distance가 포함됐고 입력 키 배치도 달랐다.
새 입력 계약은 단일 구성기를 사용하며 결과의 `context_protocol=factorial8-v1`과
`context_factors`로 식별한다. 이전 실행의 일부 수치를 새 결과에 끼워 넣지 않고
8조건을 새 결과 폴더에서 다시 수집한다. 운영 JSON의 `route_context` 선택값은 변경하지 않는다.

### 활성 모델 전체를 한 번에 실행

일괄 실행기는 다음을 자동 처리한다.

1. JSON의 `models`와 `decision_models` 활성 항목을 읽는다.
2. JSON의 지도 SHA·all-pairs·neighbors·neighbor_context·반복 수를 검증한다.
3. 모델별 8조건을 순차 실행한다. 조건 순서는 고정 시드로 섞어 프로토콜에 기록한다.
4. Laya는 CUDA와 입력 한도 8192를 사용한다.
5. KEV 0.8B·4B는 `~/kev`에서 bf16 서버를 전용 8021 포트에 자동 실행한다.
   준비된 서버의 실제 checkpoint와 CUDA 실행을 확인한 뒤 시험한다.
   모델 완료 또는 실패·중단 시 실행기가 시작한 서버만 종료하고 다음 모델로 전환한다.
6. Ollama는 JSON의 공통·모델별 옵션을 사용하고 모델 시험 후 해당 모델을 해제한다.
7. 실패 모델은 failed로 기록하고 다음 모델을 시도한다. 실패가 하나라도 있으면 종료 코드는 1이다.

하드웨어를 6GB로 가정해 KEV-4B를 건너뛰던 이전 동작과 강제 repeats=1을 제거했다.
활성화된 KEV-4B도 실제 실행을 시도한다. bf16 CUDA 메모리 부족은 실패로 남기며
CPU·양자화로 자동 변경하지 않는다. 처음 사용하는 모델은 다운로드 시간이 필요할 수 있다.
Ollama 모델은 미리 설치하고 Ollama 서비스가 실행되어 있어야 한다.
KEV 소스와 모델 의존성은 활성 가상환경에 준비되어 있어야 한다.
다른 GPU 작업과 기존 수동 KEV 서버는 종료한 상태에서 실행한다.
사용 중인 8021 포트의 서버는 재사용하거나 강제 종료하지 않고 해당 KEV 시험을 실패로 기록한다.

실험 PC의 수정 코드가 반영된 저장소에서 실행한다.

```bash
cd ~/E1I6/Logistics_AI
source ~/venv/robot/bin/activate

# 모델 호출·다운로드·결과 폴더 생성 없이 계획만 확인
python -m backend.simulation.evaluation.iterative_context_matrix --check

# 실제 수집: 활성 모델 전체 × 8조건. KEV 서버 전환도 자동 처리
python -u -m backend.simulation.evaluation.iterative_context_matrix
```

현재 JSON은 8개 활성 모델, 지도 84쌍, repeats=5, warmups=1이다.
따라서 모델당 8×84×5=3,360건, 전체 26,880건이며 워밍업은 측정 건수에서 제외한다.
각 모델의 내부 추론·정밀도·양자화가 서로 같다는 뜻은 아니므로 실행 메타데이터를 함께 비교한다.

KEV 소스 위치가 다르면 다음처럼 지정한다.

```bash
python -u -m backend.simulation.evaluation.iterative_context_matrix --kev-repo /실제/kev/경로
```

일부 모델만 실행해도 8조건은 모두 적용한다. 지정한 모델은 JSON에서 활성 상태여야 한다.

```bash
python -u -m backend.simulation.evaluation.iterative_context_matrix --selector ollama
python -u -m backend.simulation.evaluation.iterative_context_matrix --model kev-4b
```

모델 하나의 수동 실행은 기존 스크립트를 사용할 수 있다.
이 스크립트의 기본 조건도 이제 8개다. KEV 서버는 이 수동 방식에서는 별도로 준비한다.
`--state-profile`을 지정하면 해당 조건만 실행한다.

```bash
bash scripts/run_context_comparison.sh ollama gemma3:1b
bash scripts/run_context_comparison.sh --state-profile legacy_distance ollama gemma3:1b
```

### 결과·진행률·시간의 의미

일괄 실행은 `backend/simulation/benchmark_results/context8-날짜-시각-마이크로초`에
새 폴더를 자동 생성한다. 경로는 시작 시 콘솔에 표시된다.
`--output 경로`로 지정할 수도 있지만 기존 결과가 있는 폴더는 거부한다.
이어 실행이나 기존 4·5조건 데이터 재사용 기능은 제공하지 않는다.

| 파일 | 내용 |
| --- | --- |
| protocol.json | 버전·8조건·요인·실행 순서·84쌍·반복 수·소스/지도/지시문 SHA·장비 |
| config_snapshot.json | 시작 시 고정한 JSON 설정 |
| progress.json | 모델·조건별 상태·완료 경로 수·실패 이유 |
| comparison.csv | 도착·최단 일치 건수/비율, 호출 횟수, 호출당·경로 평균/p95, 성공 경로 평균, 장치 |
| report.md | 전체 비교표와 해석 범위·실패 내역 |
| all_routes_side_by_side.csv | 출발·목적지·반복 번호별 모든 모델/조건의 도착·최단·시간·호출·경로를 옆으로 비교 |
| 모델/조건/selector_iterative_route_trials.jsonl | 단계별 선택과 실제 selector 입력 steps[].model_input |
| 모델/조건/manifest.json | 해당 조건의 실제 설정·프로토콜·실행 상태 |
| 모델/worker.log | 해당 모델의 상세 진행 로그 |
| KEV 모델/server.log | 자동 실행한 KEV 서버의 시작·다운로드·오류 로그 |

도착·최단 일치율의 분모에는 경로 실패도 포함한다. 서버 오류·메모리 부족·보고된
입력 잘림으로 중단한 조건은 실패 상태로 구분하고 정상 완료 수치와 섞지 않는다.
Laya의 잘림 보고는 검사하지만, 미보고 값을 잘리지 않았다는 뜻으로 해석하지 않는다.

호출당 경과 시간은 selector 호출과 응답 처리이며 순수 GPU 연산 시간이 아니다.
전체 경로 시간은 후보 제한·거리/방위/context 계산·입력 기록용 복사·반복 모델 호출·
경로 조립·최종 검증을 포함한다. 지도 로딩·전체 그래프 변환·모델 준비/서버 시작·
결과 파일 저장은 제외한다. 모델당 워밍업 후 같은 경로 생성 타이머로 비교한다.
comparison.csv의 route_mean_s는 실패까지 포함하고 successful_route_mean_s는 성공 경로만 포함한다.

5회 반복은 같은 84쌍의 반복이며 420개의 서로 다른 지도 사례가 아니다.
temperature=0인 모델에서 같은 선택이 반복되어도 통계적으로 독립적인 추론 사례가 추가됐다고
가정하지 않는다. 전체 경로 생성 결과이므로 모델의 이전 선택에 따라 이후 후보가 달라질 수 있다.
조건 순서를 섞어 기록하더라도 GPU 온도·캐시·시간에 따른 실행 순서 효과는 완전히 제거되지 않는다.
