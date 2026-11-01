# Технические заметки

Почему решение собрано так. Команды локального запуска — в README.

## Версия vLLM и модель

Базовый образ — `vllm/vllm-openai-cpu:v0.31.0-arm64`. `requirements.txt` — freeze этого образа, пакет `vllm==0.31.0+cpu`. Dockerfile кладёт файл в образ как запись состава окружения. Пакеты уже стоят в базовом образе и при сборке заново не ставятся.

vLLM 0.32.0 для этого стенда не подходит. Сервер на `v0.32.0` доходит до `Application startup complete`, но первый `POST /v1/chat/completions` роняет EngineCore: CPU-fallback `compute_slot_mapping` → `_compute_slot_mapping_kernel_impl` падает с `RuntimeError: Boolean value of Tensor with more than one value is ambiguous`. Это разъезд сигнатуры Triton-ядра и CPU-fallback, [vLLM #61131](https://github.com/vllm-project/vllm/issues/61131). Образ закреплён на 0.31.0. Требование задания (vLLM ≥ 0.27) выполнено. На базовом образе `v0.31.0-arm64` chat-запрос на CPU доходит до `finish_reason: stop`.

Модель — `Qwen/Qwen2.5-0.5B-Instruct`. `Qwen2.5-1.5B-Instruct` на VM Docker Desktop около 7.65 GiB вместе с KV-cache и накладными расходами стабильно не помещалась. Контекст ограничен `MAX_MODEL_LEN=512`. Dtype — `float16`. fp16 и bf16 обычно оба по 2 байта на параметр, так что float16 не экономит память относительно bf16.

## Образ и rootless

Веса в слои образа не копируются. На сборке:

- `logging/request_logger.py` и `logging/log_response.py` заменяют одноимённые модули в site-packages vLLM;
- `logging/apply_logging_patch.py` правит установленный vLLM 0.31 и удаляется из финального слоя;
- `scripts/download_model.sh` копируется в `/opt/safeboard/download_model.sh`;
- создаётся пользователь `safeboard` (uid 1000, shell `nologin`), процесс идёт от `USER safeboard`.

Deployment повторяет то же снаружи: `runAsUser`, `runAsGroup` и `fsGroup` равны 1000, `runAsNonRoot: true`. `HOME`, `HF_HOME` и `VLLM_CACHE_ROOT` в образе указывают в домашний каталог этого пользователя. Каталог весов — отдельный том `/models`.

## Логирование

В stdout/stderr нет текста промпта и ответа. Факт обращения остаётся.

Подменённый `RequestLogger`:

- `log_inputs` пишет id запроса и число токенов промпта;
- `log_outputs` пишет число выходных токенов, `finish_reason` и признак стрима;
- `log_request_body` пишет тип объекта.

Текст промпта, тело запроса и сгенерированный текст в эти вызовы не передаются.

Патч в `entrypoints/launchers/api_server/app_state.py` и `entrypoints/launchers/render/app_state.py` всегда создаёт `RequestLogger`, в том числе при выключенном `enable_log_requests`. В `entrypoints/chat_utils.py` отладочная строка `Input prompt` заменена на длину текста в символах.

`logging/log_response.py` подменяет middleware, которое vLLM включает переменной `VLLM_DEBUG_LOG_API_SERVER_RESPONSE`. Оно пишет путь, HTTP-статус и размер ответа в байтах. Тело ответа не пишет.

## Старт и PVC

Команда контейнера — `/opt/safeboard/download_model.sh`. В манифесте `image: safeboard-vllm:0.31` и `imagePullPolicy: Never`. ENTRYPOINT базового образа (`vllm serve`) манифест перекрывает.

Скрипт считает веса скачанными, если в `MODEL_DIR` есть `config.json` и хотя бы один файл `*.safetensors` или `*.bin`. Иначе вызывает `huggingface_hub.snapshot_download` в этот каталог и печатает `downloading …` и `download complete: …`. Если после вызова весов нет, скрипт завершается с кодом 1. Повторный старт печатает `model weights present at …; skipping download`.

Дальше скрипт выставляет `HF_HUB_OFFLINE=1` и `TRANSFORMERS_OFFLINE=1` и заменяет себя на `vllm serve` локального каталога (`--host 0.0.0.0`, порт 8000). `HF_HUB_ENABLE_HF_TRANSFER=0`.

`HF_HOME` в образе равен `/home/safeboard/.cache/huggingface`. Подстановка `/models/.hf-home` в скрипте срабатывает только когда переменная пуста, поэтому в поде кэш Hub остаётся в домашнем каталоге контейнера. Признак «веса на месте» и повторный офлайн-старт завязаны на файлы в `MODEL_DIR` на PVC, а не на этот кэш. Домашний каталог при пересоздании пода пропадает; каталог на томе остаётся.

PVC `safeboard-models`: 5 Gi, `ReadWriteOnce`, монтирование в `/models`. Реплика одна, стратегия Deployment — `Recreate`, чтобы следующий под забирал том после остановки предыдущего.

Пустой `HF_TOKEN` и значение `REPLACE_ME` скрипт снимает с окружения и в Hub не отправляет. Непустое значение экспортируется как `HUGGING_FACE_HUB_TOKEN`. `API_KEY` обязателен и передаётся в `--api-key`.

`/dev/shm` в контейнере по умолчанию 64 MiB, воркерам vLLM этого мало. Под монтирует `emptyDir` (`medium: Memory`, `sizeLimit: 1Gi`).

Readiness/liveness и `resources` не заданы. Первый старт качает веса и долго не слушает порт 8000: проба в это окно перезапустила бы под, а лимит без замера пика легко оборвал бы загрузку. Для первого прогона это известный обмен.

## ConfigMap и Secret

ConfigMap `safeboard-vllm` подключён через `envFrom`. Скрипт читает `MODEL_ID`, `MODEL_DIR`, `DTYPE`, `MAX_MODEL_LEN`, `GPU_MEMORY_UTILIZATION` и при наличии `SERVED_MODEL_NAME`.

`VLLM_CPU_KVCACHE_SPACE=1` скрипт не разбирает: переменную наследует процесс `vllm serve`. Для CPU-бэкенда это бюджет KV-cache в гибибайтах. Единица держит резерв небольшим на VM Minikube. Пока переменная задана, `--gpu-memory-utilization` размер CPU KV почти не меняет.

`VLLM_NO_USAGE_STATS=1` и `DO_NOT_TRACK=1` тоже наследуются процессом. vLLM не шлёт usage stats.

`--enforce-eager` прописан в скрипте и запечён в образ. На прогоне 1 ноября 2026 в аргументах движка было `enforce_eager True`. Смена флага — правка скрипта и новая сборка.

Secret `safeboard-vllm` подключён тем же `envFrom`. В git лежат заглушки: `API_KEY: REPLACE_ME`, пустой `HF_TOKEN`. Боевые значения задаются локально перед `kubectl apply` и в коммит не входят. Под читает env на старте: после смены Secret его нужно перезапустить.

Имя в ответе `/v1/models` берётся из `SERVED_MODEL_NAME`. Новые параметры: `kubectl apply -f k8s/configmap.yaml` и `kubectl rollout restart deployment/safeboard-vllm`. Новый `MODEL_ID` при старом `MODEL_DIR`, если в каталоге уже есть веса, загрузку не повторяет: скрипт смотрит на файлы, а не на id репозитория.

## Память и Minikube

Размер CPU KV задаёт `VLLM_CPU_KVCACHE_SPACE` (в ConfigMap `1`, гибибайты). Пока переменная задана, `--gpu-memory-utilization` на этот размер почти не влияет. Флаг в ConfigMap оставлен `0.5`: без переменной он задаёт долю RAM узла под воркер, и на раннем прогоне `0.92` давал OOM. Контекст ограничен `512`.

Запас памяти VM Docker Desktop для этого стенда — 8–9 GiB. Образ arm64; узел Minikube с драйвером docker на Mac M4 тоже arm64. Прямой `minikube image load safeboard-vllm:0.31` может зависнуть. Надёжный путь — `docker save safeboard-vllm:0.31 | minikube image load -`.

Проверка 1 ноября 2026 (MSK), Mac M4, Minikube: под поднялся, в логе есть `Application startup complete` и `enforce_eager True`, `GET /v1/models` с Bearer вернул 200. После `kubectl delete pod` следующий старт написал `model weights present … skipping download` и снова дошёл до готовности сервера.
