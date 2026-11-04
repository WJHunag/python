#!/usr/bin/env sh
set -eu

INPUT_DIR="${INPUT_DIR:-/root/workspace/input}"
OUTPUT_DIR="${OUTPUT_DIR:-/root/workspace/output}"
DISTRIBUTED_CONFIG_DIR="${DISTRIBUTED_CONFIG_DIR:-/root/workspace/smoke_config/distributed_config}"
DETECTION_BACKEND="${DETECTION_BACKEND:-yolov6_api}"
YOLOV6_API_URL="${YOLOV6_API_URL:-http://yolov6_infer:8000/infer}"
ULTRALYTICS_WEIGHTS="${ULTRALYTICS_WEIGHTS:-/root/workspace/weights/ultralytics/best.pt}"
ULTRALYTICS_IMGSZ="${ULTRALYTICS_IMGSZ:-640}"
DEVICE="${DEVICE:-cuda:0}"
SCORE_THR="${SCORE_THR:-0.7}"
NIGHT_SCORE_THR="${NIGHT_SCORE_THR:-0.9}"
GROUP_SIZE="${GROUP_SIZE:-8}"
JSON_PATH="${JSON_PATH:-/root/workspace/smoke_camera_config.json}"
UI_SERVER_UPDATE_URL="${UI_SERVER_UPDATE_URL:-http://192.168.4.110:58080/image/image_update}"

mkdir -p "$INPUT_DIR" "$OUTPUT_DIR" "$DISTRIBUTED_CONFIG_DIR"

exec python -u multi_image_detection.py "$INPUT_DIR" \
    --out-dir "$OUTPUT_DIR" \
    --distributed-config-dir "$DISTRIBUTED_CONFIG_DIR" \
    --backend "$DETECTION_BACKEND" \
    --api-url "$YOLOV6_API_URL" \
    --ultralytics-weights "$ULTRALYTICS_WEIGHTS" \
    --imgsz "$ULTRALYTICS_IMGSZ" \
    --device "$DEVICE" \
    --score-thr "$SCORE_THR" \
    --night-score-thr "$NIGHT_SCORE_THR" \
    --group-size "$GROUP_SIZE" \
    --json-path "$JSON_PATH" \
    --ui-server-update-url "$UI_SERVER_UPDATE_URL" \
    "$@"
