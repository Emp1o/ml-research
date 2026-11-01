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

Детали реализации — см. [NOTES.md](NOTES.md).
