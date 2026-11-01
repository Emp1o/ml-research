# SafeBoard — инференс vLLM на CPU в Minikube

Автор: Салитов М.

Локальный OpenAI-совместимый инференс `Qwen/Qwen2.5-0.5B-Instruct` на CPU в Minikube. Образ `safeboard-vllm:0.31` собран из `vllm/vllm-openai-cpu:v0.31.0-arm64` (vLLM 0.31.0). Веса качает initContainer `download-model` на легковесном rootless-образе `safeboard-model-downloader:0.1` и кладёт их на PVC. Основной контейнер `vllm` Hugging Face не вызывает: он читает локальный каталог с тома.

Проверено 1 ноября 2026 (MSK) на Mac M4 для прежней схемы, где скачивание жило в entrypoint основного контейнера: `GET /v1/models` с Bearer отвечает 200; после `kubectl delete pod` сервер снова доходит до `Application startup complete`, в аргументах движка `enforce_eager True`. Строка `model weights present … skipping download` теперь в логе initContainer `download-model`. Контейнер `vllm` веса не скачивает.

## Быстрый старт

Из корня репозитория, кластер Minikube уже запущен. Первая команда печатает ключ: вставьте его вместо `REPLACE_ME` в `k8s/base/secret.yaml` и не коммитьте файл. `port-forward` занимает терминал — `curl` во втором, когда в логах есть `Application startup complete`.

```bash
openssl rand -hex 32
docker build -t safeboard-vllm:0.31 .
docker build -f Dockerfile.downloader -t safeboard-model-downloader:0.1 .
docker save safeboard-vllm:0.31 | minikube image load -
docker save safeboard-model-downloader:0.1 | minikube image load -
kubectl apply -k k8s
kubectl port-forward svc/safeboard-vllm 8000:8000
curl -sS http://127.0.0.1:8000/v1/models \
  -H "Authorization: Bearer $(kubectl get secret safeboard-vllm -o jsonpath='{.data.API_KEY}' | base64 -d)"
```

## Требования

- Docker Desktop. Память VM — **не ниже 8–9 GiB**: модель, KV-cache и Minikube на меньшем лимите уходят в OOM.
- `kubectl`
- Minikube с драйвером `docker`
- Узел **arm64**. Базовый образ — `v0.31.0-arm64`; сборка и прогон проверены на Mac M4.

Если кластера ещё нет (Docker Desktop уже запущен):

```bash
minikube start --driver=docker --cpus=4 --memory=8192
```

Minikube берёт память из VM Docker Desktop, поэтому лимит Docker должен покрывать эти 8 GiB с запасом.

## Состав репозитория

| Путь | Назначение |
|---|---|
| `Dockerfile` | Rootless-образ vLLM: пользователь `safeboard` (uid 1000), патч логов, скрипт `serve_model.sh` |
| `Dockerfile.downloader` | Отдельный легковесный rootless-образ только для скачивания весов (uid/gid 1000). Не образ vLLM |
| `requirements.txt` | Зафиксированные версии окружения базового образа (`vllm==0.31.0+cpu`) |
| `requirements-downloader.txt` | Зафиксированные зависимости легковесного образа (`huggingface_hub==1.33.0` и его зависимости) |
| `logging/` | Подмена логгера vLLM: факт запроса есть, текст промпта и ответа нет |
| `scripts/download_model.sh` | Скачивание весов на PVC. Если `config.json` и веса уже есть — пропуск. vLLM не запускает |
| `scripts/serve_model.sh` | Запуск `vllm serve` с локального каталога, без обращения к Hugging Face |
| `scripts/load_test.py` | Нагрузочный прогон `POST /v1/chat/completions` со стримом: TTFT, latency, throughput |
| `k8s/` | Kustomize-набор: `kubectl apply -k k8s`. Манифесты в `k8s/base/` (PVC, Secret, ConfigMap, Deployment, Service) |

## Секреты

В git у `k8s/base/secret.yaml` только заглушки: `API_KEY` равен `REPLACE_ME`, `HF_TOKEN` пустой. Настоящий ключ API и токен Hugging Face в репозиторий не коммитятся.

Перед деплоем отредактируйте файл локально. Ключ:

```bash
openssl rand -hex 32
```

Впишите вывод в `API_KEY`. Для публичной `Qwen/Qwen2.5-0.5B-Instruct` оставьте `HF_TOKEN` пустым. Для gated-модели запишите токен Hugging Face в `HF_TOKEN`.

Файл уже в git. После локальной правки не коммитьте её: в коммите должны остаться `API_KEY: "REPLACE_ME"` и пустой `HF_TOKEN`.

## Сборка

Из корня репозитория:

```bash
docker build -t safeboard-vllm:0.31 .
docker build -f Dockerfile.downloader -t safeboard-model-downloader:0.1 .
```

Веса ни в один образ не входят. Теги должны совпасть с `image` в Deployment: у обоих контейнеров `imagePullPolicy: Never`.

Легковесный образ собирается из `Dockerfile.downloader`, база `python:3.12.16-slim-bookworm`. Внутри только `scripts/download_model.sh` и зафиксированный `huggingface_hub`. Пользователь `safeboard`, uid и gid 1000 — те же, что у пода и у образа vLLM, чтобы файлы на PVC читал основной контейнер. Это не образ vLLM и не его entrypoint.

## Загрузка образов в Minikube

Оба образа должны оказаться в демоне Minikube. Прямой `minikube image load` на этой связке может зависнуть. Рабочий путь:

```bash
docker save safeboard-vllm:0.31 | minikube image load -
docker save safeboard-model-downloader:0.1 | minikube image load -
```

## Деплой

Первому старту нужен исходящий доступ initContainer `download-model` к Hugging Face. Контейнер `vllm` за весами в сеть не ходит.

```bash
kubectl apply -k k8s
kubectl rollout status deployment/safeboard-vllm
```

PVC `safeboard-models` — 5 Gi, `ReadWriteOnce`. Сервис `safeboard-vllm` — ClusterIP, порт 8000.

## Kustomize

Манифесты лежат в `k8s/base`: PVC, Secret, ConfigMap, Deployment, Service и `kustomization.yaml`. Корневой `k8s/kustomization.yaml` подключает этот base. Из корня репозитория обе команды собирают один и тот же набор:

```bash
kubectl apply -k k8s
kubectl apply -k k8s/base
```

Посмотреть сборку, не отправляя её в кластер:

```bash
kubectl kustomize k8s
```

Один файл по-прежнему применяется напрямую, например после правки ConfigMap:

```bash
kubectl apply -f k8s/base/configmap.yaml
kubectl rollout restart deployment/safeboard-vllm
```

Secret в git — заглушка (`API_KEY: REPLACE_ME`, пустой `HF_TOKEN`). Перед `kubectl apply -k` впишите свой ключ в `k8s/base/secret.yaml` и не коммитьте эту правку.

## Проверка

```bash
kubectl logs -f deployment/safeboard-vllm -c download-model
kubectl logs -f deployment/safeboard-vllm -c vllm
```

В `download-model` на первом старте — `downloading …` и `download complete: …`. В `vllm` дождитесь `Application startup complete`. В другом терминале:

```bash
kubectl port-forward svc/safeboard-vllm 8000:8000
```

```bash
API_KEY="$(kubectl get secret safeboard-vllm -o jsonpath='{.data.API_KEY}' | base64 -d)"

curl -sS http://127.0.0.1:8000/v1/models \
  -H "Authorization: Bearer ${API_KEY}"
```

Ожидается HTTP 200 и карточка `Qwen/Qwen2.5-0.5B-Instruct`. Запрос без `Authorization` или с чужим Bearer отвечает **401**.

Необязательный chat:

```bash
curl -sS http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${API_KEY}" \
  -d '{
    "model": "Qwen/Qwen2.5-0.5B-Instruct",
    "messages": [{"role": "user", "content": "Say hello in one short sentence."}],
    "max_tokens": 32
  }'
```

Повторный старт с тем же томом:

```bash
kubectl delete pod -l app=safeboard-vllm
kubectl logs deployment/safeboard-vllm -c download-model
kubectl logs -f deployment/safeboard-vllm -c vllm
```

В логе initContainer — `model weights present … skipping download`. Контейнер `vllm` пишет `serving local weights at …` и читает локальный каталог (`HF_HUB_OFFLINE=1` выставляет `scripts/serve_model.sh`).

## Параметры ConfigMap

Ключи `k8s/base/configmap.yaml` попадают в под через `envFrom`. После правки образ пересобирать не нужно:

```bash
kubectl apply -f k8s/base/configmap.yaml
kubectl rollout restart deployment/safeboard-vllm
```

| Ключ | Сейчас | Куда попадает |
|---|---|---|
| `MODEL_ID` | `Qwen/Qwen2.5-0.5B-Instruct` | Репозиторий Hugging Face для первой загрузки |
| `MODEL_DIR` | `/models/Qwen2.5-0.5B-Instruct` | Каталог весов на PVC |
| `SERVED_MODEL_NAME` | `Qwen/Qwen2.5-0.5B-Instruct` | Имя модели в API |
| `DTYPE` | `float16` | `--dtype` |
| `MAX_MODEL_LEN` | `512` | `--max-model-len` |
| `GPU_MEMORY_UTILIZATION` | `0.5` | `--gpu-memory-utilization`. При заданном `VLLM_CPU_KVCACHE_SPACE` на размер CPU KV почти не влияет |
| `VLLM_CPU_KVCACHE_SPACE` | `1` | Переменная окружения vLLM: бюджет KV-cache CPU, в гибибайтах |
| `VLLM_NO_USAGE_STATS` | `1` | Отказ от usage stats vLLM |
| `DO_NOT_TRACK` | `1` | Тот же отказ: процесс не шлёт телеметрию |

Другая модель — новые `MODEL_ID` и `MODEL_DIR` на том же PVC. Старый каталог на томе остаётся. Если в `MODEL_DIR` уже лежат `config.json` и веса, initContainer загрузку пропускает, даже когда `MODEL_ID` сменился.

`--enforce-eager` задаёт `scripts/serve_model.sh`. В ConfigMap этого ключа нет: скрипт копируется в образ vLLM при сборке. На прогоне в аргументах движка было `enforce_eager True`.

## Замечания

- Инференс только на CPU.
- Скачивание весов вынесено из entrypoint основного контейнера в initContainer `download-model`. Образ `safeboard-model-downloader:0.1` собирается отдельным `Dockerfile.downloader` и не содержит vLLM.
- Оба контейнера непривилегированные. У пода `runAsUser`, `runAsGroup` и `fsGroup` равны 1000, `runAsNonRoot: true`. Тот же uid/gid у легковесного образа. InitContainer получает `MODEL_ID`, `MODEL_DIR` и `HF_TOKEN`. `API_KEY` в него не передаётся.
- Лог пишет факт обращения (id запроса, число токенов, `finish_reason`). Текст промпта и сгенерированный ответ в stdout/stderr не попадают.
- Реплика одна, стратегия `Recreate` — том `ReadWriteOnce`.
- `/dev/shm` — `emptyDir` в памяти, лимит 1 Gi (дефолтных 64 MiB воркерам vLLM мало).

## Ожидаемые фрагменты

Первый старт, контейнер `download-model`:

```text
downloading Qwen/Qwen2.5-0.5B-Instruct into /models/Qwen2.5-0.5B-Instruct
download complete: /models/Qwen2.5-0.5B-Instruct
```

Дальше в логе контейнера `vllm`: `serving local weights at /models/Qwen2.5-0.5B-Instruct`, затем `Application startup complete` и `enforce_eager True`.

Старт после удаления пода, контейнер `download-model`:

```text
model weights present at /models/Qwen2.5-0.5B-Instruct; skipping download
```

`GET /v1/models` с верным Bearer — HTTP 200. Тело такого вида (поля сокращены):

```json
{
  "object": "list",
  "data": [
    {
      "id": "Qwen/Qwen2.5-0.5B-Instruct",
      "object": "model",
      "owned_by": "vllm",
      "max_model_len": 512
    }
  ]
}
```

Без Bearer или с неверным ключом — HTTP 401.

## Нагрузочное тестирование

Скрипт `scripts/load_test.py` (только стандартная библиотека Python 3) шлёт `POST /v1/chat/completions` со `stream: true`. Ключ берётся из переменной `API_KEY`. Базовый URL по умолчанию — `http://127.0.0.1:8000`, модель — `Qwen/Qwen2.5-0.5B-Instruct`.

Короткий промпт — одна фраза (`Reply with one short sentence: what is 2+2?`). Длинный — просьба суммировать текст и `--long-repeats` повторов предложения `The quick brown fox jumps over the lazy dog.` (по умолчанию 20). `max_tokens` по умолчанию 32 у обоих профилей: отличается длина входа. Вместе с шаблоном чата промпт должен уложиться в `MAX_MODEL_LEN` (сейчас 512). Ответ 400 из-за лимита контекста — уменьшить `--long-repeats` или `--max-tokens`.

Определения, те же, что печатает скрипт:

- **TTFT** — от момента перед HTTP-вызовом до первого SSE-события, в котором `choices[].delta.content` непустая строка. Чанк только с `role` первым токеном не считается. В интервал входят установка соединения и очередь на сервере.
- **Latency** — от того же момента до конца тела (`data: [DONE]` или EOF).
- Ожидание свободного воркера в клиентском пуле в TTFT и Latency не входит.
- **Throughput, req/s** — успешные запросы сценария, делённые на время от старта пула до конца последнего запроса. Неуспешные в числитель не входят, их время остаётся в знаменателе.
- **Throughput, output tok/s** — сумма выходных токенов успешных запросов за то же время. Берётся `usage.completion_tokens` (`stream_options.include_usage: true`). Если usage в потоке нет, это число SSE-событий с непустым `delta.content`.
- **Throughput, total tok/s** — сумма `usage.prompt_tokens` и `usage.completion_tokens` за то же время. Без usage ячейка пустая: длину промпта скрипт по символам не оценивает.

`port-forward` занимает терминал. Во втором, когда в логе контейнера `vllm` есть `Application startup complete`:

```bash
kubectl port-forward svc/safeboard-vllm 8000:8000
```

```bash
API_KEY="$(kubectl get secret safeboard-vllm -o jsonpath='{.data.API_KEY}' | base64 -d)"
python3 scripts/load_test.py \
  --concurrency 2 \
  --requests 8 \
  --output load-test-results.md
```

Файл `load-test-results.md` в git не входит. В README переносится таблица из stdout. Ненулевой код возврата значит, что warmup или часть запросов завершились ошибкой; таблица при этом всё равно печатается.

Только длинный профиль, меньше повторов:

```bash
python3 scripts/load_test.py \
  --scenarios long \
  --concurrency 1 \
  --requests 4 \
  --long-repeats 12
```

Проверка самого скрипта без кластера (локальная SSE-заглушка, не эндпоинт vLLM):

```bash
python3 scripts/test_load_test.py
```

### Таблица метрик

**Таблица-заглушка.** Прогона против Minikube в этой среде не было. Заполнить после прогона на Minikube: подставить сюда таблицу из stdout скрипта. Оценочных чисел в ячейках нет.

| Сценарий | TTFT p50, мс | TTFT среднее, мс | Latency p50, мс | Latency среднее, мс | Throughput, req/s | Throughput, output tok/s | Throughput, total tok/s |
|---|---:|---:|---:|---:|---:|---:|---:|
| короткий | — | — | — | — | — | — | — |
| длинный | — | — | — | — | — | — | — |

| Сценарий | Символов в промпте | max_tokens | N | Параллельность | Успешно | Prompt tokens, среднее | Output tokens, среднее |
|---|---:|---:|---:|---:|---:|---:|---:|
| короткий | — | — | — | — | — | — | — |
| длинный | — | — | — | — | — | — | — |

### Аналитика: что менять ради Throughput

Это инженерное рассуждение по документации vLLM и по параметрам этого CPU-стенда. Замером на поде оно не подтверждено.

Стенд: vLLM 0.31.0, CPU, `Qwen/Qwen2.5-0.5B-Instruct`, `MAX_MODEL_LEN=512`, `VLLM_CPU_KVCACHE_SPACE=1` (гибибайты), `GPU_MEMORY_UTILIZATION=0.5`, Minikube около 4 CPU и 8 GiB, одна реплика. Пока `VLLM_CPU_KVCACHE_SPACE` задан, бюджет KV задаёт эта переменная; `GPU_MEMORY_UTILIZATION` на размер CPU KV почти не влияет. Доля `0.92` на раннем прогоне этого стенда приводила к OOM.

Источники: [CPU — vLLM](https://docs.vllm.ai/en/stable/getting_started/installation/cpu/) (`VLLM_CPU_KVCACHE_SPACE`, привязка OpenMP, раздел performance tuning) и [Optimization and Tuning](https://docs.vllm.ai/en/stable/configuration/optimization/) (preemption, chunked prefill). В актуальной CPU-документации online-дефолт `--max-num-seqs` — `128 * world_size`, `--max-num-batched-tokens` — `2048 * world_size`. На этом поде `world_size` равен 1. Образ закреплён на 0.31.0: если `vllm serve --help` внутри образа покажет другие дефолты, верны они. Там же `VLLM_CPU_KVCACHE_SPACE` помечен как legacy в пользу `--kv-cache-memory-bytes`. На этом образе бюджет по-прежнему задаёт переменная из ConfigMap. Переносить его во флаг стоит только после проверки `--help` в `safeboard-vllm:0.31`, и источник размера должен остаться один.

`--max-num-seqs`, `--max-num-batched-tokens`, `--enable-chunked-prefill` и `--enable-prefix-caching` в `scripts/serve_model.sh` сейчас не передаются. Их добавление — правка скрипта и новая сборка `safeboard-vllm:0.31`. `MAX_MODEL_LEN`, `VLLM_CPU_KVCACHE_SPACE` и новые переменные окружения лежат в ConfigMap: правка и рестарт пода, образ тот же.

Оценка ёмкости KV — арифметика по опубликованному `config.json` `Qwen/Qwen2.5-0.5B-Instruct`. Это не дамп кэша с пода. `use_sliding_window` выключен, размер головы 896/14 = 64. На токен при 2 байтах на элемент: 2 (K и V) × 24 слоя × 2 KV-головы × 64 × 2 байта = 12288 байт. 1 ГиБ / 12288 ≈ 8,7×10^4 токенов до округления блоками vLLM.

**А. Много запросов, мало токенов.** Узкое место — сколько коротких последовательностей попадает в один decode-шаг.

- Поднять `--max-num-seqs`. В CPU-документации этот предел числа последовательностей в батче сильнее всего влияет на скорость выходных токенов. Короткий запрос занимает мало KV: при длине в десятки токенов в 1 ГиБ помещаются сотни таких последовательностей. Потолок раньше оказывается на дефолте 128 или на 4 ядрах. Документация советует крутить размер батча от дефолта: больший батч обычно поднимает throughput, меньший снижает latency. На 4 vCPU батч в сотни последовательностей удлиняет шаг. Поднимать предел, пока output tok/s растёт, и остановиться, когда растёт уже latency.
- `--max-num-batched-tokens` оставить около online-дефолта 2048. Документация относит его в первую очередь к первому токену. 2048 уже больше `MAX_MODEL_LEN=512`, короткий промпт и так целиком входит в шаг. Сильно больший бюджет на CPU отдаёт шаг prefill и задерживает decode, которым этот профиль набирает выходные токены.
- `VLLM_CPU_KVCACHE_SPACE` оставить 1, пока в логе нет preemption из-за нехватки KV. Документация: больший объём позволяет больше параллельных запросов и более длинный контекст, но сумма весов шарда и этого бюджета должна помещаться в память узла, иначе воркер убивается (exit 9). Для коротких запросов гибибайт не первый ограничитель, а запас 8 GiB VM маленький.
- Долю `GPU_MEMORY_UTILIZATION` оставить 0.5. На GPU та же страница предлагает поднять её, чтобы отдать больше памяти под KV и убрать preemption. В этом поде размер пула задаёт `VLLM_CPU_KVCACHE_SPACE`.
- До перебора батча проверить привязку потоков, как просит CPU-документация. `VLLM_CPU_OMP_THREADS_BIND` по умолчанию `auto`. Для online serving там рекомендуют оставить одно ядро фронту (`VLLM_CPU_NUM_OF_RESERVED_CPU`), чтобы OpenMP не делил все ядра с API-процессом. На Minikube с 4 CPU это около трёх потоков на инференс. В контейнере `auto` может видеть ядра хоста вне cgroup; если по `htop` это так, поставить `nobind` и `OMP_NUM_THREADS` по лимиту пода. Это переменные окружения, их можно добавить в ConfigMap. `securityContext` пода ради этого не менять.
- Если у коротких запросов общий префикс, `--enable-prefix-caching` убирает повторный prefill. На уникальных фразах выигрыш маленький.

**Б. Запросов меньше, токенов в каждом очень много.** Throughput набирается prefill длинного промпта, и этот промпт должен поместиться в KV.

- Поднять `MAX_MODEL_LEN` до длины, которую реально будут слать и чей KV помещается в память этой VM. Сейчас 512, и более длинный контекст сервер отклонит до планировщика. У модели `max_position_embeddings` 32768. На Minikube с 8 GiB такой контекст не помещается. Ключ уже в ConfigMap.
- Поднять `VLLM_CPU_KVCACHE_SPACE` так, чтобы один целевой контекст и немного выходных токенов сидели в пуле. Документация связывает больший бюджет с более длинным контекстом. Preemption с пересчётом страница оптимизации называет вредным для latency и для фактического throughput. Пример `40` из CPU-документации к этой VM не относится: веса 0.5B при 2 байтах на параметр — около 1 ГиБ, рядом рантайм, `/dev/shm` на 1 ГиБ и Minikube в 8 ГиБ. Шаг — с 1 ГиБ вверх, пока под не уходит в OOM. На GPU тот же смысл у `gpu-memory-utilization`; здесь его менять бесполезно, пока бюджет задаёт переменная.
- Снизить `--max-num-seqs` до числа одновременно живых длинных запросов. При нехватке кэша документация советует уменьшить `max_num_seqs`: меньше последовательностей в батче, меньше спрос на KV. Дефолт 128 при длинном контексте примет больше запросов, чем пул удерживает, и планировщик начнёт вытеснять их с пересчётом.
- Поднять `--max-num-batched-tokens` к размеру куска промпта, который ядра считают за один шаг. По описанию chunked prefill больший бюджет обрабатывает больше токенов prefill за итерацию и улучшает TTFT и throughput; меньший оставляет шаг decode. Запросов мало, шаг почти весь prefill. Если chunked prefill выключен, тот же раздел предупреждает: бюджет должен быть больше `max_model_len`, иначе сервер может не стартовать. Online-дефолт CPU — 2048. Когда `max-model-len` станет больше 2048, бюджет поднимают вместе с ним. Рекомендация «больше 8192» в том разделе дана для маленькой модели на большой GPU. На 4 vCPU кусок в тысячи токенов и так занимает ядра на время шага.
- Включить chunked prefill, если лог процесса 0.31 на CPU показывает, что он выключен (`--enable-chunked-prefill`). В руководстве по V1 он включён по умолчанию там, где платформа позволяет, и режет длинный промпт по `max_num_batched_tokens`. Пока флаг выключен, держать `max_num_batched_tokens` больше `max_model_len`.

После смены параметра и рестарта пода тот же скрипт показывает, вырос ли throughput. До этого прогона текст выше — направление настройки.

Детали реализации — см. [NOTES.md](NOTES.md).
