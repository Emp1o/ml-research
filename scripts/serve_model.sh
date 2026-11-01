#!/usr/bin/env bash
# Main-container entrypoint. Weights are already on the volume: the
# download-model initContainer put them there (or left an earlier snapshot).
# This script does not contact the Hub. It exits if the snapshot is missing,
# then serves that local directory.
# Serve flags come from the ConfigMap. API_KEY comes from the Secret.

set -euo pipefail

: "${MODEL_ID:?MODEL_ID is required}"
: "${MODEL_DIR:?MODEL_DIR is required}"
: "${API_KEY:?API_KEY is required}"
: "${DTYPE:?DTYPE is required}"
: "${MAX_MODEL_LEN:?MAX_MODEL_LEN is required}"
: "${GPU_MEMORY_UTILIZATION:?GPU_MEMORY_UTILIZATION is required}"

export PYTHONUNBUFFERED=1

weights_present() {
  [[ -f "${MODEL_DIR}/config.json" ]] || return 1
  shopt -s nullglob
  local files=("${MODEL_DIR}"/*.safetensors "${MODEL_DIR}"/*.bin)
  shopt -u nullglob
  [[ ${#files[@]} -gt 0 ]]
}

if ! weights_present; then
  echo "model weights missing at ${MODEL_DIR}; download-model initContainer did not leave a snapshot" >&2
  exit 1
fi

echo "serving local weights at ${MODEL_DIR}"

# Local files only. A later start must not call the Hub again.
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1

exec vllm serve "${MODEL_DIR}" \
  --served-model-name "${SERVED_MODEL_NAME:-${MODEL_ID}}" \
  --dtype "${DTYPE}" \
  --max-model-len "${MAX_MODEL_LEN}" \
  --gpu-memory-utilization "${GPU_MEMORY_UTILIZATION}" \
  --enforce-eager \
  --host 0.0.0.0 \
  --port 8000 \
  --api-key "${API_KEY}"
