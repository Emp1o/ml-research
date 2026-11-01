# Технические заметки

Почему решение собрано так. Команды локального запуска — в README.

## Версия vLLM и модель

Базовый образ — `vllm/vllm-openai-cpu:v0.31.0-arm64`. `requirements.txt` — freeze этого образа, пакет `vllm==0.31.0+cpu`. Dockerfile кладёт файл в образ как запись состава окружения. Пакеты уже стоят в базовом образе и при сборке заново не ставятся.

vLLM 0.32.0 для этого стенда не подходит. Сервер на `v0.32.0` доходит до `Application startup complete`, но первый `POST /v1/chat/completions` роняет EngineCore: CPU-fallback `compute_slot_mapping` → `_compute_slot_mapping_kernel_impl` падает с `RuntimeError: Boolean value of Tensor with more than one value is ambiguous`. Это разъезд сигнатуры Triton-ядра и CPU-fallback, [vLLM #61131](https://github.com/vllm-project/vllm/issues/61131). Образ закреплён на 0.31.0. Требование задания (vLLM ≥ 0.27) выполнено. На базовом образе `v0.31.0-arm64` chat-запрос на CPU доходит до `finish_reason: stop`.

Модель — `Qwen/Qwen2.5-0.5B-Instruct`. `Qwen2.5-1.5B-Instruct` на VM Docker Desktop около 7.65 GiB вместе с KV-cache и накладными расходами стабильно не помещалась. Контекст ограничен `MAX_MODEL_LEN=512`. Dtype — `float16`. fp16 и bf16 обычно оба по 2 байта на параметр, так что float16 не экономит память относительно bf16.

## Образ vLLM и rootless

Веса в слои образа не копируются. На сборке `Dockerfile`:

- `logging/request_logger.py` и `logging/log_response.py` заменяют одноимённые модули в site-packages vLLM;
- `logging/apply_logging_patch.py` правит установленный vLLM 0.31 и удаляется из финального слоя;
- `scripts/serve_model.sh` копируется в `/opt/safeboard/serve_model.sh`. Скрипт скачивания в этот образ не входит;
- создаётся пользователь `safeboard` (uid 1000, shell `nologin`), процесс идёт от `USER safeboard`.

Deployment повторяет то же снаружи: `runAsUser`, `runAsGroup` и `fsGroup` равны 1000, `runAsNonRoot: true`. Эти поля заданы на поде, поэтому их наследуют и initContainer, и контейнер `vllm`. `HOME`, `HF_HOME` и `VLLM_CACHE_ROOT` в образе vLLM указывают в домашний каталог этого пользователя. Каталог весов — отдельный том `/models`.

## Легковесный образ загрузчика

Скачивание вынесено из entrypoint основного контейнера. Отдельный `Dockerfile.downloader` собирает `safeboard-model-downloader:0.1`. База — `python:3.12.16-slim-bookworm` (патч-тег, не плавающий `python:slim`; манифест multi-arch, в том числе arm64). В образе нет vLLM.

Скрипту нужны bash (shebang), `useradd` из пакета `passwd` и `/usr/sbin/nologin` из пакета `login`. В `python:3.12.16-slim-bookworm` они уже есть; Dockerfile ставит их явно, затем делает `pip install` из `requirements-downloader.txt`. Версии совпадают с freeze образа vLLM для `huggingface_hub==1.33.0` и его обязательных зависимостей. Дополнительные extras не ставятся.

Пользователь образа — `safeboard`, uid и gid 1000, `USER safeboard`. Скрипт дополнительно завершается с кодом 1, если его запустили от root: иначе файлы на томе оказались бы чужими для контейнера `vllm`. `scripts/download_model.sh` — единственный ENTRYPOINT.

`HF_HOME` в этом образе — `/home/safeboard/.cache/huggingface`, не каталог на PVC. `huggingface_hub` 1.33 копирует в `local_dir` обычные файлы и не оставляет там симлинки в кэш. После завершения initContainer домашний каталог пропадает. На томе остаётся снимок в `MODEL_DIR`. Повторный старт смотрит на эти файлы. Кэш Hub на PVC не дублирует веса: лимит тома 5 Gi рассчитан на сам снимок.

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

Под поднимает два контейнера на одном томе `/models`.

InitContainer `download-model`: образ `safeboard-model-downloader:0.1`, `imagePullPolicy: Never`, команда `/opt/safeboard/download_model.sh`. Из ConfigMap ему передаются только `MODEL_ID` и `MODEL_DIR`, из Secret — только `HF_TOKEN`. `API_KEY` в этот контейнер не попадает.

Скрипт считает веса скачанными, если в `MODEL_DIR` есть `config.json` и хотя бы один файл `*.safetensors` или `*.bin`. Иначе вызывает `huggingface_hub.snapshot_download` в этот каталог и печатает `downloading …` и `download complete: …`. Если после вызова весов нет, скрипт завершается с кодом 1, и основной контейнер не стартует. Повторный старт печатает `model weights present at …; skipping download` и выходит с кодом 0. `HF_HUB_ENABLE_HF_TRANSFER=0`.

Контейнер `vllm`: образ `safeboard-vllm:0.31`, `imagePullPolicy: Never`, команда `/opt/safeboard/serve_model.sh`. ENTRYPOINT базового образа (`vllm serve`) манифест перекрывает. Скрипт не скачивает веса. Если снимка в `MODEL_DIR` нет, он завершается с кодом 1. Иначе печатает `serving local weights at …`, выставляет `HF_HUB_OFFLINE=1` и `TRANSFORMERS_OFFLINE=1` и заменяет себя на `vllm serve` локального каталога (`--host 0.0.0.0`, порт 8000).

`HF_HOME` обоих образов лежит в домашнем каталоге контейнера, не на томе. Подстановка `/models/.hf-home` в скрипте загрузчика срабатывает только когда переменная пуста; в образе она задана, поэтому кэш Hub на PVC не пишется. Признак «веса на месте» завязан на файлы в `MODEL_DIR`. Домашний каталог при пересоздании пода пропадает; каталог на томе остаётся.

PVC `safeboard-models`: 5 Gi, `ReadWriteOnce`, монтирование в `/models` и у initContainer, и у `vllm`. Реплика одна, стратегия Deployment — `Recreate`, чтобы следующий под забирал том после остановки предыдущего.

Пустой `HF_TOKEN` и значение `REPLACE_ME` скрипт загрузчика снимает с окружения и в Hub не отправляет. Непустое значение экспортируется как `HUGGING_FACE_HUB_TOKEN`. `API_KEY` обязателен для `serve_model.sh` и передаётся в `--api-key`.

`/dev/shm` в контейнере по умолчанию 64 MiB, воркерам vLLM этого мало. Под монтирует `emptyDir` (`medium: Memory`, `sizeLimit: 1Gi`) только в контейнер `vllm`. InitContainer этот том не монтирует.

Readiness/liveness и `resources` не заданы. Веса качает initContainer, затем процесс vLLM на CPU долго поднимает модель и только потом слушает порт 8000. Проба в это окно перезапустила бы под, а лимит без замера пика легко оборвал бы загрузку. Для первого прогона это известный обмен.

## ConfigMap и Secret

ConfigMap `safeboard-vllm` подключён к контейнеру `vllm` через `envFrom`. `serve_model.sh` читает `MODEL_ID`, `MODEL_DIR`, `DTYPE`, `MAX_MODEL_LEN`, `GPU_MEMORY_UTILIZATION` и при наличии `SERVED_MODEL_NAME`. InitContainer получает из того же ConfigMap только `MODEL_ID` и `MODEL_DIR`.

`VLLM_CPU_KVCACHE_SPACE=1` скрипт не разбирает: переменную наследует процесс `vllm serve`. Для CPU-бэкенда это бюджет KV-cache в гибибайтах. Единица держит резерв небольшим на VM Minikube. Пока переменная задана, `--gpu-memory-utilization` размер CPU KV почти не меняет.

`VLLM_NO_USAGE_STATS=1` и `DO_NOT_TRACK=1` тоже наследуются процессом. vLLM не шлёт usage stats.

`--enforce-eager` прописан в `scripts/serve_model.sh` и запечён в образ vLLM. На прогоне 1 ноября 2026 в аргументах движка было `enforce_eager True`. Смена флага — правка скрипта и новая сборка образа `safeboard-vllm:0.31`. Образ загрузчика при этом пересобирать не нужно.

Secret `safeboard-vllm` подключён к контейнеру `vllm` тем же `envFrom`. InitContainer читает из него только `HF_TOKEN`. В git лежат заглушки: `API_KEY: REPLACE_ME`, пустой `HF_TOKEN`. Боевые значения задаются локально перед `kubectl apply` и в коммит не входят. Под читает env на старте: после смены Secret его нужно перезапустить.

Файлы стека — в `k8s/base`. `k8s/base/kustomization.yaml` перечисляет PVC, Secret, ConfigMap, Deployment и Service. `k8s/kustomization.yaml` подключает этот base, поэтому `kubectl apply -k k8s` и `kubectl apply -k k8s/base` отдают в кластер те же объекты: те же имена, лейблы и заглушки Secret.

Имя в ответе `/v1/models` берётся из `SERVED_MODEL_NAME`. Новые параметры: `kubectl apply -f k8s/base/configmap.yaml` и `kubectl rollout restart deployment/safeboard-vllm`. Новый `MODEL_ID` при старом `MODEL_DIR`, если в каталоге уже есть веса, загрузку не повторяет: initContainer смотрит на файлы, а не на id репозитория.

## Память и Minikube

Размер CPU KV задаёт `VLLM_CPU_KVCACHE_SPACE` (в ConfigMap `1`, гибибайты). Пока переменная задана, `--gpu-memory-utilization` на этот размер почти не влияет. Флаг в ConfigMap оставлен `0.5`: без переменной он задаёт долю RAM узла под воркер, и на раннем прогоне `0.92` давал OOM. Контекст ограничен `512`.

Запас памяти VM Docker Desktop для этого стенда — 8–9 GiB. Образ vLLM — arm64; узел Minikube с драйвером docker на Mac M4 тоже arm64. Легковесный образ — multi-arch манифест `python:3.12.16-slim-bookworm`, на M4 собирается arm64. Прямой `minikube image load` может зависнуть. Надёжный путь — `docker save safeboard-vllm:0.31 | minikube image load -` и то же для `safeboard-model-downloader:0.1`.

Проверка 1 ноября 2026 (MSK), Mac M4, Minikube, ещё со скачиванием в entrypoint основного контейнера: под поднялся, в логе есть `Application startup complete` и `enforce_eager True`, `GET /v1/models` с Bearer вернул 200. После `kubectl delete pod` следующий старт написал `model weights present … skipping download` и снова дошёл до готовности сервера. Та же строка пропуска теперь печатается initContainer `download-model`; контейнер `vllm` только отдаёт локальный снимок.

## Нагрузочный прогон

`scripts/load_test.py` ходит в уже поднятый эндпоинт и манифесты не меняет. Метрики и команда — в README. Таблица заполнена прогоном Minikube на Mac M4 (MSK) 2026-11-01 против `safeboard-vllm` (UTC 2026-11-01T13:02:51Z). Рассуждение про `--max-num-seqs`, `--max-num-batched-tokens`, `MAX_MODEL_LEN` и `VLLM_CPU_KVCACHE_SPACE` — разбор документации и этого CPU-стенда, не результат замера.

`python3 scripts/test_load_test.py` проверяет разбор SSE и локальную заглушку. Это не нагрузка на vLLM. `load-test-results.md` скрипт пишет только по `--output` и в git не входит.
