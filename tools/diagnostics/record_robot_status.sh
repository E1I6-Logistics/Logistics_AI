#!/usr/bin/env bash
set -euo pipefail

# 실제 로봇 측정용으로 /api/robots를 주기적으로 읽어 CSV에 남긴다.
# 백엔드 로그 시각은 초 단위라 2초 ETA 범위를 판단하기에 거칠어서, 도착 시각과 전압을 소수 초로 기록한다.
# 사용법: bash tools/diagnostics/record_robot_status.sh [출력 CSV] [간격(초)]
OUT="${1:-logs/robot_status_$(date +%Y%m%d_%H%M%S).csv}"
INTERVAL="${2:-0.5}"
API="${FMS_API_BASE:-http://127.0.0.1:8000}"

mkdir -p "$(dirname "$OUT")"
echo "time_epoch,time,robot_id,status,current_node,connected,battery,battery_voltage,battery_voltage_min,dispatch_block_reason" > "$OUT"
echo "기록 시작: $OUT (간격 ${INTERVAL}초, Ctrl+C로 종료)" >&2

while true; do
    now="$(date +%s.%N)"
    stamp="$(date -d "@$now" +%H:%M:%S.%3N)"
    # 조회 실패는 건너뛰고 다음 주기에 다시 시도한다.
    if body="$(curl -fsS -m 1 "$API/api/robots" 2>/dev/null)"; then
        jq -r --arg t "$now" --arg s "$stamp" \
            '.[] | [$t, $s, .robot_id, .status, .current_node, .connected, .battery, .battery_voltage, .battery_voltage_min, .dispatch_block_reason] | @csv' \
            <<<"$body" >> "$OUT"
    fi
    sleep "$INTERVAL"
done
