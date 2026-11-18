# Разработка и проверки

## Локальное окружение

Для совпадения с Docker и CI используйте Linux и Python 3.12. Lock-файлы
сформированы для этого окружения. Исходный проект допускает Python >= 3.11;
на других платформах при необходимости разрешайте зависимости из pyproject.toml.

Для полного набора существующих ASR-тестов нужен системный FFmpeg. На Ubuntu/Debian:

```bash
sudo apt-get update
sudo apt-get install -y --no-install-recommends ffmpeg
```

В Docker FFmpeg уже включён в runtime-образ; CI устанавливает его перед pytest.

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements-dev.lock
cp .env.example .env
.venv/bin/python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8001
```

При существующем `.env` сохраняйте его значения. Без OpenRouter-ключа приложение
стартует: liveness, документация и заглушки работают; readiness и LLM-вызовы
возвращают 503. Interviewer hard limit работает без ключа.
В Docker Compose ключ обязателен уже на этапе проверки конфигурации.

PyCharm: interpreter `.venv/bin/python`, target Module `uvicorn`, parameters
`app.main:app --reload --host 127.0.0.1 --port 8001`, working directory — корень
проекта. Локальные `.idea`, `.vscode`, venv и `.env` исключены из Git и Docker.

## Проверки

```bash
.venv/bin/python -m pytest
.venv/bin/python -m app.contracts --check
.venv/bin/ruff check .
.venv/bin/ruff format --check .
```

Обычный pytest исключает marker `integration`, даже если ключ есть в environment
или `.env`. HTTP transport заменяется MockTransport; реального inference нет.
Два платных smoke-теста запускаются только явно:

```bash
.venv/bin/python -m pytest -m integration
```

Они требуют ключа и выполняют до четырёх inference calls с учётом repair.
Структурная валидация и mock-тесты не измеряют качество реальной модели,
нейтральность follow-up или устойчивость к prompt injection. Для этих свойств
нужен отдельный контрольный набор и реальные прогоны.

После запуска контейнера `python3 scripts/smoke.py` проверяет liveness,
readiness, OpenAPI и жёсткий лимит Interviewer. Этот smoke не вызывает модель.
Для другого адреса используйте `--base-url http://host:port`.

## Обновление зависимостей

`pyproject.toml` — источник допустимых зависимостей. `requirements.lock` содержит
только runtime, `requirements-dev.lock` — тот же runtime и инструменты разработки.
Оба файла содержат версии и хеши, коммитятся вместе с изменением зависимостей.
Не редактируйте отдельные хеши вручную.

В окружении Python 3.12 с установленным dev lock:

```bash
.venv/bin/python -m piptools compile --generate-hashes --strip-extras --no-annotate \
  --no-build-isolation --no-emit-index-url --no-emit-trusted-host \
  --output-file requirements.lock pyproject.toml
.venv/bin/python -m piptools compile --generate-hashes --allow-unsafe --strip-extras \
  --no-annotate --no-build-isolation --no-emit-index-url --no-emit-trusted-host \
  --constraint requirements.lock --extra dev --output-file requirements-dev.lock pyproject.toml
```

Без `--upgrade` сохраняются подходящие существующие pins. Для запланированного
обновления добавьте `--upgrade` или `--upgrade-package <package>` к первой команде,
затем пересоберите dev lock. Флаг `--allow-unsafe` нужен pip-tools для включения
его зависимостей pip/setuptools в dev lock с обязательными хешами.

Проверьте установку в чистом venv, pytest, lint, Docker build и smoke.
Базовый Docker digest обновляется отдельно с теми же проверками.

## Устройство кода

- `app/api` — маршруты, загрузка аудио и dependency injection.
- `app/schemas` — Pydantic v2, запрет неизвестных полей, проверки связанных значений.
- `app/services` — orchestration, общий deadline, repair и доменная валидация.
- `app/clients/openrouter.py` — транспорт, provider routing, strict JSON Schema, usage.
- `app/prompts` — версии и полные тексты инструкций Interviewer/Evaluator.
- `app/settings.py` — environment и локальный `.env`; ключ представлен `SecretStr`.

Общий httpx.AsyncClient создаётся в lifespan и закрывается при остановке.
Два system messages содержат инструкции и trusted configuration. Dialogue
сохраняет порядок: candidate → user, interviewer → assistant. Interviewer не
получает reference_answer. При repair в trusted message добавляются только
сгенерированные кодом нарушения контракта, без недоверенного результата модели.

В v2 `InterviewerLLMResult` / `EvaluatorLLMResult` содержат только предметный
результат. `InterviewerResponse` / `EvaluatorResponse` добавляют обязательную meta.
`app/generation.py` хранит один монотонный deadline и UsageAccumulator;
`app/http_contract.py` измеряет HTTP wall-clock, отменяет работу при disconnect
и формирует безопасные ошибки только для двух LLM endpoints.
Deadline, метрики и error envelope не включаются в trusted configuration.
OpenRouter получает прежний структурный набор JSON Schema keywords; условные
связи полей из полных HTTP-схем проверяются Python до ответа Backend.
Эта проекция не добавляет в provider schema поля meta/error.

Четыре схемы в корне генерируются `python -m app.contracts`; флаг `--check`
проверяет отсутствие расхождений. Снимки схем коммитятся вместе с моделями.
JSON Schema validator установлен только в dev lock. Каждый HTTP-ответ двух
маршрутов в обычных тестах проверяется по опубликованной output schema.

Evaluator проверяет anchors, точный набор criterion_id, согласованность статуса
и score, а также дословное наличие evidence в реплике candidate. Релевантность
цитат и содержательную оценку определяет модель. Никаких общих числовых оценок
пользователя, БД или хранения сессий в сервисе нет.

Модель `qwen/qwen3.5-27b` и провайдер `alibaba` закреплены в settings. Provider
fallback отключён; reasoning отключён. Температуры Interviewer/Evaluator — 0.2/0.0.
Смена модели или провайдера требует проверки поддержки передаваемых параметров,
structured output и контрольного набора, а не только изменения `.env`.

## Что проверяет CI

Workflow `.github/workflows/ci.yml` устанавливает dev lock, запускает lint,
форматирование и unit tests, собирает production-образ, поднимает Compose,
проверяет HTTP, Docker DNS, UID и read-only filesystem. Ключ для CI — фиктивный;
модель не вызывается. Compose-проект CI изолирован и удаляется в конце job.
Автоматического деплоя на сервер или публикации образа workflow не выполняет.
