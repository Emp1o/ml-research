# CPU inference. vLLM 0.31.0 (>= 0.27). Not 0.32: CPU slot_mapping bug #61131.
# Model weights are not copied into the image.
FROM vllm/vllm-openai-cpu:v0.31.0-arm64

COPY requirements.txt /opt/safeboard/requirements.txt
COPY logging/request_logger.py \
    /opt/venv/lib/python3.12/site-packages/vllm/entrypoints/serve/utils/request_logger.py
COPY logging/log_response.py \
    /opt/venv/lib/python3.12/site-packages/vllm/entrypoints/serve/middleware/log_response.py
COPY logging/apply_logging_patch.py /tmp/apply_logging_patch.py

RUN python /tmp/apply_logging_patch.py \
    && rm -f /tmp/apply_logging_patch.py \
    && useradd --create-home --uid 1000 --shell /usr/sbin/nologin safeboard \
    && mkdir -p /home/safeboard/.cache/huggingface /home/safeboard/.cache/vllm \
    && chown -R safeboard:safeboard /home/safeboard

ENV HOME=/home/safeboard \
    HF_HOME=/home/safeboard/.cache/huggingface \
    VLLM_CACHE_ROOT=/home/safeboard/.cache/vllm \
    XDG_CACHE_HOME=/home/safeboard/.cache

WORKDIR /home/safeboard
USER safeboard
