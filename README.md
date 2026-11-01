# SafeBoard — инференс vLLM на CPU в Minikube

Автор: Салитов М.

Локальный OpenAI-совместимый инференс `Qwen/Qwen2.5-0.5B-Instruct` на CPU в Minikube. Образ `safeboard-vllm:0.31` собран из `vllm/vllm-openai-cpu:v0.31.0-arm64` (vLLM 0.31.0). Веса качаются с Hugging Face на PVC при первом старте пода и дальше читаются с тома.

Проверено 1 ноября 2026 (MSK) на Mac M4: `GET /v1/models` с Bearer отвечает 200; после `kubectl delete pod` лог содержит `model weights present … skipping download`, сервер снова доходит до `Application startup complete`, в аргументах движка `enforce_eager True`.

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
| `Dockerfile` | Rootless-образ: пользователь `safeboard` (uid 1000), патч логов, скрипт старта |
| `requirements.txt` | Зафиксированные версии окружения базового образа (`vllm==0.31.0+cpu`) |
| `logging/` | Подмена логгера vLLM: факт запроса есть, текст промпта и ответа нет |
| `scripts/download_model.sh` | Скачивание весов на PVC и запуск `vllm serve` |
| `k8s/` | PVC, Secret, ConfigMap, Deployment, Service |

## Секреты

В git у `k8s/secret.yaml` только заглушки: `API_KEY` равен `REPLACE_ME`, `HF_TOKEN` пустой. Настоящий ключ API и токен Hugging Face в репозиторий не коммитятся.

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
```

Веса в образ не входят. Тег должен совпасть с `image` в Deployment: у контейнера `imagePullPolicy: Never`.

## Загрузка образа в Minikube

Образ должен оказаться в демоне Minikube. Прямой `minikube image load safeboard-vllm:0.31` на этой связке может зависнуть. Рабочий путь:

```bash
docker save safeboard-vllm:0.31 | minikube image load -
```

## Деплой

Первому старту нужен исходящий доступ пода к Hugging Face.

```bash
kubectl apply -f k8s/
kubectl rollout status deployment/safeboard-vllm
```

PVC `safeboard-models` — 5 Gi, `ReadWriteOnce`. Сервис `safeboard-vllm` — ClusterIP, порт 8000.

## Проверка

```bash
kubectl logs -f deployment/safeboard-vllm
```

Дождитесь `Application startup complete`. В другом терминале:

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
kubectl logs -f deployment/safeboard-vllm
```

В логе — `model weights present … skipping download`. Дальше `vllm serve` читает локальный каталог (`HF_HUB_OFFLINE=1`).

## Параметры ConfigMap

Ключи `k8s/configmap.yaml` попадают в под через `envFrom`. После правки образ пересобирать не нужно:

```bash
kubectl apply -f k8s/configmap.yaml
kubectl rollout restart deployment/safeboard-vllm
```

| Ключ | Сейчас | Куда попадает |
|---|---|---|
| `MODEL_ID` | `Qwen/Qwen2.5-0.5B-Instruct` | Репозиторий Hugging Face для первой загрузки |
| `MODEL_DIR` | `/models/Qwen2.5-0.5B-Instruct` | Каталог весов на PVC |
| `SERVED_MODEL_NAME` | `Qwen/Qwen2.5-0.5B-Instruct` | Имя модели в API |
| `DTYPE` | `float16` | `--dtype` |
| `MAX_MODEL_LEN` | `512` | `--max-model-len` |
| `GPU_MEMORY_UTILIZATION` | `0.5` | `--gpu-memory-utilization` (на CPU — доля RAM под KV) |
| `VLLM_CPU_KVCACHE_SPACE` | `1` | Переменная окружения vLLM: бюджет KV-cache CPU, в гибибайтах |

Другая модель — новые `MODEL_ID` и `MODEL_DIR` на том же PVC. Старый каталог на томе остаётся. Если в `MODEL_DIR` уже лежат `config.json` и веса, скрипт загрузку пропускает, даже когда `MODEL_ID` сменился.

`--enforce-eager` задаёт `scripts/download_model.sh`. В ConfigMap этого ключа нет: скрипт копируется в образ при сборке. На прогоне в аргументах движка было `enforce_eager True`.

## Замечания

- Инференс только на CPU.
- Контейнер непривилегированный: uid/gid 1000, в Deployment `runAsNonRoot: true`.
- Лог пишет факт обращения (id запроса, число токенов, `finish_reason`). Текст промпта и сгенерированный ответ в stdout/stderr не попадают.
- Реплика одна, стратегия `Recreate` — том `ReadWriteOnce`.
- `/dev/shm` — `emptyDir` в памяти, лимит 1 Gi (дефолтных 64 MiB воркерам vLLM мало).

## Ожидаемые фрагменты

Первый старт:

```text
downloading Qwen/Qwen2.5-0.5B-Instruct into /models/Qwen2.5-0.5B-Instruct
download complete: /models/Qwen2.5-0.5B-Instruct
```

Дальше в логе vLLM: `Application startup complete` и `enforce_eager True`.

Старт после удаления пода:

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
