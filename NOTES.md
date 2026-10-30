# SafeBoard ML Research — рабочие заметки

Краткие факты для итогового отчёта. Не путать с README.

## Этап 2 — smoke: vLLM CPU + один chat-запрос

**Дата:** 2026-10-30

**Статус:** успешно (JSON `chat.completion`, `finish_reason: stop`)

### Образ

- Рабочий: `vllm/vllm-openai-cpu:v0.31.0-arm64`
- Не использовать: `latest` / `latest-arm64` (= **v0.32.0** на момент проверки)

На **v0.32.0** сервер поднимается (`Application startup complete`), но **первый** `POST /v1/chat/completions` роняет EngineCore:

- место: `compute_slot_mapping` → CPU fallback `_compute_slot_mapping_kernel_impl`
- ошибка: `RuntimeError: Boolean value of Tensor with more than one value is ambiguous`
- причина: разъезд сигнатур Triton-ядра и CPU-fallback (баг [vLLM #61131](https://github.com/vllm-project/vllm/issues/61131))
- обход: закрепить **v0.31.0** (требование задания: vLLM ≥ 0.27 — выполняется)

### Модель

- `Qwen/Qwen2.5-0.5B-Instruct`
- Почему не 1.5B: Docker Desktop VM ~7.65 GiB RAM; веса ~0.9 GiB + KV/overhead не влезают стабильно вместе с утилизацией памяти

### Команда запуска

```bash
docker run --rm -it \
  --shm-size=1g \
  -p 8000:8000 \
  -v "$HOME/.cache/huggingface:/root/.cache/huggingface" \
  -e HF_HUB_ENABLE_HF_TRANSFER=0 \
  vllm/vllm-openai-cpu:v0.31.0-arm64 \
  --model Qwen/Qwen2.5-0.5B-Instruct \
  --dtype float16 \
  --max-model-len 512 \
  --gpu-memory-utilization 0.5 \
  --api-key safeboard-local-key
```

### Флаги (зачем)

| Флаг | Зачем |
|------|--------|
| `--shm-size=1g` | дефолтный `/dev/shm` 64M мало для multiprocessing vLLM |
| `--dtype float16` | меньше RAM, чем bf16 на CPU в этом образе |
| `--max-model-len 512` | ограничивает размер KV-cache |
| `--gpu-memory-utilization 0.5` | на CPU — доля RAM под KV/пул (0.92 давал OOM) |
| `--api-key …` | Bearer для OpenAI-совместимого API |
| HF cache volume | не качать веса заново при каждом `docker run` |

### Smoke-запрос

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer safeboard-local-key" \
  -d '{
    "model": "Qwen/Qwen2.5-0.5B-Instruct",
    "messages": [{"role": "user", "content": "Say hello in one short sentence."}],
    "max_tokens": 32
  }'
```

**Ответ (фрагмент):** `content`: `Hello! It's nice to meet you!`, `system_fingerprint`: `vllm-0.31.0-…`, `usage`: prompt 36 / completion 10.

### Промежуточные проблемы (для отчёта)

1. Малый `/dev/shm` → увеличить `--shm-size`
2. Высокий `--gpu-memory-utilization` → OOM → снизить до `0.5`
3. 1.5B не влезает в лимит Docker Desktop → 0.5B + `max-model-len 512`
4. v0.32.0 crash на первом запросе → pin `v0.31.0-arm64`

## План по дням (зафиксировано 30.10.2026)

| Блок | Когда | Обязательно |
|---|---|---|
| **1** | 30.10 | Этап 2: модель + один ответ vLLM на CPU (+ черновик Dockerfile, если успеешь) |
| **2** | 31.10 | Этап 3: образ rootless, pinned, логи без текста |
| **3** | 1.11 | Этап 4: Deploy/Service + PVC + Secret + ConfigMap + Bearer |
| **4** | 2.11 → **3.11 до ~18:00** | Кэш на PVC + README с нуля → полировка → **сдача**; бонусы только если база уже зелёная |

Практический дедлайн сдачи: **~3.11.2026 18:00 MSK** (не тянуть до формальной даты портала 6.11).
