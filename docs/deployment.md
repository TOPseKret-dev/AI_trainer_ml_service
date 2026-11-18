# Развёртывание ML-сервиса

## Что нужно на сервере

Linux с Docker Engine и Docker Compose >= 2.20; доступ к Docker Hub/PyPI при сборке
и исходящий HTTPS к OpenRouter при работе. PostgreSQL, Redis, GPU и постоянные
тома этому сервису не нужны. Docker для Ubuntu устанавливается по
[официальной инструкции](https://docs.docker.com/engine/install/ubuntu/).

Python 3.12 и зависимости находятся внутри образа. Базовый образ закреплён digest,
runtime-зависимости — версиями и SHA256 в `requirements.lock`.
Сборка использует готовые wheels; компиляторы в образ не устанавливаются.

## Первый запуск

В новом клоне репозитория:

```bash
cp .env.example .env
chmod 600 .env
```

Заполните `OPENROUTER_API_KEY` в `.env`. Существующий `.env` при обновлении
сохраняйте; сравнивайте его с новым шаблоном, добавляя новые настройки вручную.
Ключ передаётся контейнеру при запуске. В Dockerfile, build args и Git его нет.
Администратор Docker может читать environment контейнера.

```bash
docker compose config --quiet
docker compose up -d --build --wait --wait-timeout 120
docker compose ps
curl --fail http://127.0.0.1:8001/health/ready
python3 scripts/smoke.py
```

`config --quiet` проверяет Compose, не печатая развёрнутый environment с ключом.
Для существующей сети Backend используйте [инструкцию интеграции](integration.md)
и оба Compose-файла во всех командах жизненного цикла.

## Настройки

Compose читает `.env` для подстановок, но передаёт приложению только явно
перечисленные переменные. Environment оболочки имеет приоритет над `.env`.
После изменения настроек выполните `docker compose up -d --force-recreate --wait`:
обычный `restart` environment не обновляет.

| Переменная | По умолчанию | Назначение |
| --- | --- | --- |
| `OPENROUTER_API_KEY` | обязательно для Compose | Ключ OpenRouter |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | Адрес API провайдера |
| `OPENROUTER_MODEL` | `qwen/qwen3.5-27b` | Единственная разрешённая сейчас модель |
| `OPENROUTER_PROVIDER` | `alibaba` | Закреплённый провайдер, fallback отключён |
| `OPENROUTER_TIMEOUT_SECONDS` | `55` | Таймаут отдельных HTTP-операций |
| `LLM_REQUEST_TIMEOUT_SECONDS` | `55` | Общий бюджет LLM, включая один repair |
| `ML_IMAGE` | `intersim-ml:local` | Имя образа для сборки или запуска |
| `ML_HOST_PORT` | `8001` | Порт на loopback сервера; внутри всегда `8001` |
| `ML_WORKERS` | `1` | Число Uvicorn workers |
| `ML_CONCURRENCY` | `32` | Uvicorn limit-concurrency на worker; избыток → 503 |
| `ML_LOG_LEVEL` | `info` | Уровень Uvicorn; приложение пишет INFO в stdout/stderr |
| `ML_CPUS` | `1.0` | Ограничение CPU контейнера |
| `ML_MEMORY_LIMIT` | `512m` | Ограничение памяти контейнера |
| `ML_TMPFS_SIZE` | `128m` | Вместимость временного `/tmp`, включая spool аудио |
| `ML_SHUTDOWN_TIMEOUT_SECONDS` | `60` | Ожидание запросов Uvicorn после SIGTERM |
| `ML_STOP_GRACE_PERIOD` | `75s` | Время Docker до принудительного завершения |
| `ML_NETWORK_NAME` | `intersim-ml` | Создаваемая сеть отдельного запуска |
| `INTERSIM_NETWORK` | задаётся для интеграции | Настоящее имя существующей сети Backend |

CPU, память, concurrency и `/tmp` — стартовые эксплуатационные настройки,
которые нужно подобрать по нагрузке. `ML_CONCURRENCY` не является отдельным
глобальным лимитом LLM и не создаёт очередь. При нескольких workers у каждого
свой HTTP-клиент и свой лимит. `/tmp` учитывается в памяти контейнера; его размер
не заменяет HTTP-проверку размера файла. Настройки Groq и ограничения ASR
описаны в [отдельной инструкции](asr.md); для ASR нужен ASR_GROQ_API_KEY.

При изменении общего таймаута соблюдайте порядок:
`LLM_REQUEST_TIMEOUT_SECONDS < timeout HTTP-клиента Backend`,
`LLM_REQUEST_TIMEOUT_SECONDS < ML_SHUTDOWN_TIMEOUT_SECONDS < ML_STOP_GRACE_PERIOD`.
Стартовая конфигурация: ML 55 секунд, клиент Core 60 секунд, shutdown 60/75 секунд.
Транспортный timeout отдельно ограничивает зависшие connect/read/write/pool операции.
В контракте v2 локальный лимит ML нельзя увеличить выше 55 секунд.
`deadline_at` от Core может уменьшить бюджет; сервис оставляет 5 секунд резерва.
Для Interviewer/Evaluator ошибки возвращаются как `error.code/message/details`
с best-effort meta; полный каталог находится в [HTTP API](api.md).

## Здоровье и диагностика

| Маршрут | Значение |
| --- | --- |
| `/health`, `/health/live` | Приложение отвечает; ключ может отсутствовать |
| `/health/ready` | Настройки загружены и ключ непустой; без ключа → 503 |

Docker healthcheck использует readiness каждые 15 секунд. Проверка не вызывает
OpenRouter, не тратит inference и не подтверждает валидность ключа, баланс или
доступность выбранного провайдера. `unhealthy` само по себе не вызывает Docker
restart; политика `unless-stopped` перезапускает завершившийся процесс.

```bash
docker compose ps
docker compose logs --tail 100 ml-service
docker compose logs --follow ml-service
docker stats --no-stream
```

Приложение логирует маршруты, статусы, длительность и метаданные попыток LLM.
Тексты диалогов и секреты в application logs не записываются; access log Uvicorn
в Docker отключён. Docker ротирует логи: 10 МБ × 3 файла на контейнер.

| Проблема | Что проверить |
| --- | --- |
| `Set OPENROUTER_API_KEY` при чтении Compose | Ключ в выбранном `--env-file`/`.env`, переменные оболочки |
| Контейнер `unhealthy` | Логи запуска, непустой ключ, `/health/ready` |
| HTTP 503 от LLM-маршрута | Конфигурация, provider/auth/rate limit, доступ по HTTPS или concurrency |
| HTTP 504 | Общий LLM-бюджет либо транспортный timeout |
| HTTP 502 | Невалидный upstream envelope/результат или исчерпан repair |
| Core не видит `ml-service` | Оба контейнера в одной сети; внутри нельзя использовать `localhost` |
| Нет external network | Взять имя из `docker inspect intersim_backend`, запустить Core-стек первым |
| Ошибка записи большого аудио | Лимит памяти, `/tmp` и ограничения из [ASR](asr.md) |

## Обновление и откат

Для обновления из исходников используйте проверенный commit/tag и уникальное имя
образа, например `ML_IMAGE=intersim-ml:release-2026-11-03` в `.env`:

```bash
git fetch origin
git switch --detach <проверенный-коммит-или-тег>
docker compose build --pull ml-service
docker compose up -d --no-build --wait --wait-timeout 120
python3 scripts/smoke.py
```

Перед обновлением сохраните имя предыдущего образа. Для отката укажите его в
`ML_IMAGE` и выполните `docker compose up -d --no-build --wait`. Повторная сборка
для отката не нужна. Сервис stateless; миграций БД у него нет. При одном экземпляре
пересоздание контейнера создаёт краткий перерыв обслуживания.

Для доставки готового образа можно использовать приватный registry:

```bash
docker login ghcr.io
docker compose pull ml-service
docker compose up -d --no-build --wait
```

Здесь `ML_IMAGE` должен указывать на реально опубликованный version tag или digest,
например `ghcr.io/intersim-org/intersim-ml:<версия>`. Этот репозиторий не публикует
образ и не меняет сервер автоматически: CI выполняет проверки и сборку.

Базовый Python digest обновляйте отдельным изменением Dockerfile с повторной
сборкой и smoke. `--pull` не меняет уже закреплённый digest.

## Остановка

```bash
docker compose stop
docker compose down
```

При интеграции добавляйте `-f compose.yaml -f compose.intersim.yaml`.
Общая external network принадлежит Core-стеку и при `down` ML-сервиса сохраняется.
Постоянных данных и томов в этом Compose нет. Контейнер работает под UID/GID 1000,
с read-only root filesystem, отдельным `/tmp`, без Linux capabilities и без
возможности повысить привилегии.
