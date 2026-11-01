#!/usr/bin/env bash
# 운영 route_context와 분리하여 네 context를 같은 JSON 시험 설정으로 비교한다.
set -euo pipefail

if [[ "${1:-}" == "--help" || $# -eq 0 ]]; then
  cat <<'HELP'
사용법:
  bash scripts/run_context_comparison.sh [--check] <kev|laya|ollama> <model> [output_root]

예:
  bash scripts/run_context_comparison.sh kev kev-08b
  bash scripts/run_context_comparison.sh ollama gemma3:1b

--check: 모델을 호출하지 않고 해석된 실행 명령만 출력
시험 조건: selector_iterative_benchmark.json의 graph/settings를 읽음
운영 context: route_context에서 선택하며 이 비교 시험에는 적용하지 않음
HELP
  exit 0
fi

check_only=0
if [[ "${1:-}" == "--check" ]]; then
  check_only=1
  shift
fi
if (( $# < 2 || $# > 3 )); then
  printf '%s\n' "selector와 model, 선택적으로 output_root를 지정하세요." >&2
  exit 2
fi

selector="$1"
model="$2"
case "$selector" in
  kev|laya|ollama) ;;
  *) printf '%s\n' "지원 selector: kev, laya, ollama" >&2; exit 2 ;;
esac

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"
benchmark_python="${BENCH_PYTHON:-python}"
config="backend/simulation/evaluation/selector_iterative_benchmark.json"
output_root="${3:-backend/simulation/benchmark_results/context4-$(date +%Y%m%d-%H%M%S)}"
model_tag="${model//\//--}"
model_tag="${model_tag//:/-}"

# JSON 설정을 먼저 검증하고 CLI의 공통 옵션으로 변환한다.
arguments="$("$benchmark_python" - "$config" "$selector" "$model" <<'PY'
import sys
from pathlib import Path
from backend.simulation.evaluation.selector_iterative_route_benchmark import load_iterative_config
config_path, selector, model = sys.argv[1:]
resolved = load_iterative_config(Path(config_path), [model] if selector == "ollama" else None)
settings = resolved["settings"]
values = [
    ("--graph", resolved["graph_path"]),
    ("--case-mode", settings["case_mode"]),
    ("--case-count", settings["case_count"]),
    ("--candidate-scope", settings["candidate_scope"]),
    ("--context-mode", settings["context_mode"]),
    ("--repeats", settings["repeats"]),
    ("--warmups", settings["warmups"]),
    ("--max-steps", settings["max_steps"]),
    ("--deadline", settings["deadline_seconds"]),
]
if settings.get("route_seed") is not None:
    values.append(("--route-seed", settings["route_seed"]))
for flag, value in values:
    if value is not None:
        print(flag)
        print(value)
PY
)"
mapfile -t common_args <<< "$arguments"

export ROUTE_SELECTOR="$selector"
case "$selector" in
  kev) export KEV_MODEL="$model"; export KEV_REQUIRE_CUDA=1 ;;
  laya)
    export LAYA_HF_MODEL="$model"
    export LAYA_REQUIRE_CUDA=1
    export LAYA_MAX_LENGTH=8192
    ;;
  ollama) export OLLAMA_MODEL="$model" ;;
esac

for profile in legacy minimal minimal_distance legacy_compass_distance; do
  output="$output_root/$selector-$model_tag/$profile"
  if [[ "$selector" == "ollama" ]]; then
    command=("$benchmark_python" -m backend.simulation.evaluation.selector_iterative_route_benchmark
      --config "$config" --model "$model")
  else
    command=("$benchmark_python" -m backend.simulation.evaluation.selector_iterative_route_benchmark
      "${common_args[@]}")
  fi
  command+=(--state-profile "$profile" --output "$output")
  printf '\ncontext=%s\n' "$profile"
  if (( check_only )); then
    printf '%q ' "${command[@]}"
    printf '\n'
  else
    "${command[@]}"
  fi
done
