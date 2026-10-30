#!/usr/bin/env bash
# On first start, download HF weights onto the mounted volume.
# If config.json and weight files are already there, skip the download
# and start vLLM on that local directory.
# Model id, dtype, context length, and the other serve flags come from
# the environment (ConfigMap). HF_TOKEN and API_KEY come from the Secret.

set -euo pipefail

: "${MODEL_ID:?MODEL_ID is required}"
: "${MODEL_DIR:?MODEL_DIR is required}"
: "${API_KEY:?API_KEY is required}"
: "${DTYPE:?DTYPE is required}"
: "${MAX_MODEL_LEN:?MAX_MODEL_LEN is required}"
: "${GPU_MEMORY_UTILIZATION:?GPU_MEMORY_UTILIZATION is required}"

MODELS_ROOT="${MODELS_ROOT:-/models}"
export HF_HOME="${HF_HOME:-${MODELS_ROOT}/.hf-home}"
export HF_HUB_ENABLE_HF_TRANSFER="${HF_HUB_ENABLE_HF_TRANSFER:-0}"
export PYTHONUNBUFFERED=1
mkdir -p "${HF_HOME}" "${MODEL_DIR}"

# Empty or the committed placeholder must not be sent to the Hub.
# A real token (gated models) is exported for huggingface_hub.
if [[ -z "${HF_TOKEN:-}" || "${HF_TOKEN}" == "REPLACE_ME" ]]; then
  unset HF_TOKEN
  unset HUGGING_FACE_HUB_TOKEN
else
  export HUGGING_FACE_HUB_TOKEN="${HF_TOKEN}"
fi

weights_present() {
  [[ -f "${MODEL_DIR}/config.json" ]] || return 1
  shopt -s nullglob
  local files=("${MODEL_DIR}"/*.safetensors "${MODEL_DIR}"/*.bin)
  shopt -u nullglob
  [[ ${#files[@]} -gt 0 ]]
}

if weights_present; then
  echo "model weights present at ${MODEL_DIR}; skipping download"
else
  echo "downloading ${MODEL_ID} into ${MODEL_DIR}"
  python - <<'PY'
import os
from huggingface_hub import snapshot_download

snapshot_download(
    repo_id=os.environ["MODEL_ID"],
    local_dir=os.environ["MODEL_DIR"],
)
PY
  if ! weights_present; then
    echo "download finished but no weights found in ${MODEL_DIR}" >&2
    exit 1
  fi
  echo "download complete: ${MODEL_DIR}"
fi

# Local files only. A later start must not call the Hub again.
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1

exec vllm serve "${MODEL_DIR}" \
  --served-model-name "${SERVED_MODEL_NAME:-${MODEL_ID}}" \
  --dtype "${DTYPE}" \
  --max-model-len "${MAX_MODEL_LEN}" \
  --gpu-memory-utilization "${GPU_MEMORY_UTILIZATION}" \
  --host 0.0.0.0 \
  --port 8000 \
  --api-key "${API_KEY}"
