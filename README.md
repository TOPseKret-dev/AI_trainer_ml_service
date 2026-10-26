# InterSim ML service

Stateless HTTP service на Python 3.11+ (рекомендуется 3.12), FastAPI, Uvicorn и Pydantic v2.
Interviewer и Evaluator используют реальный OpenRouter. Transcription и
question-bank generation остаются **детерминированными STUB**.
Сервис не использует БД, авторизацию и хранение истории. Endpoint генерации возвращает
только черновики. Orchestration, persistence, публикация вопросов и итоговый
пользовательский score остаются ответственностью backend.

## Setup

Из корня репозитория:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e ".[dev]"
```

Если Python 3.12 отсутствует, используйте `python3 -m venv .venv` с Python >=3.11.
Для Interviewer/Evaluator настройте OpenRouter, как описано ниже.
Версии зависимостей ограничены в `pyproject.toml`.
Команды ниже выполняются после активации `.venv`.

## OpenRouter configuration

Скопируйте `.env.example` в `.env` и задайте ключ локально либо через environment.
Environment имеет приоритет; Pydantic Settings автоматически загружает `.env`.
`.env` игнорируется Git. Пример без ключа:

```env
OPENROUTER_API_KEY=
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
OPENROUTER_MODEL=qwen/qwen3.5-27b
OPENROUTER_PROVIDER=alibaba
OPENROUTER_TIMEOUT_SECONDS=55
```

Модель закреплена: `qwen/qwen3.5-27b`. Alibaba Cloud International закреплён
через `provider.order=["alibaba"]`, `allow_fallbacks=false`, `require_parameters=true`.
Slug `alibaba` и поддержка `reasoning`, `response_format`, `structured_outputs`
проверены по [публичному endpoints API](https://openrouter.ai/api/v1/models/qwen/qwen3.5-27b/endpoints).
Модель и провайдер в settings ограничены этими значениями: смена требует проверки возможностей.
[Provider routing](https://openrouter.ai/docs/guides/routing/provider-selection).

Interviewer: temperature **0.2**. Evaluator: temperature **0.0**.
Reasoning явно отключён через `reasoning={"enabled": false}`; это отключение,
а не скрытие reasoning-текста. Параметр поддерживается выбранным endpoint,
reasoning у модели не является обязательным.
[Reasoning API](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens).
Usage автоматически приходит в том же response, без дополнительного inference
и без устаревшего `usage.include`:
[Usage accounting](https://openrouter.ai/docs/cookbook/administration/usage-accounting).

Общий `httpx.AsyncClient` создаётся на lifespan FastAPI и закрывается при остановке.
Timeout **55 секунд** применяется к HTTP connect/read/write/pool операциям каждого attempt;
это не общий deadline для двух attempts. При нарушении structured/domain контракта
разрешён ровно один semantic repair (не более двух платных calls на endpoint request).
Сетевые/provider retries отсутствуют. Ошибки конфигурации, подключения, auth,
rate limit и недоступности → 503; timeout → 504; invalid upstream output → 502.
Полный upstream response наружу не возвращается.

Без ключа приложение запускается, `/health`, Swagger и stub endpoints работают.
Interviewer/Evaluator при необходимости LLM возвращают 503 с понятной ошибкой
конфигурации. Hard max Interviewer работает и без ключа.

- transcription — still stub;
- question-bank generation — still stub;
- interviewer — real OpenRouter;
- evaluator — real OpenRouter.

## Run

```bash
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Эквивалентный запуск: `python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000`.

## Tests

```bash
.venv/bin/python -m pytest
```

Обычный запуск исключает marker `integration`, даже если ключ настроен.
Unit tests подменяют HTTP transport через dependency injection и не выполняют inference.
Опциональный реальный smoke (два HTTP-запроса, максимум четыре inference calls с repair):

```bash
.venv/bin/python -m pytest -m integration
```

Без `OPENROUTER_API_KEY` из environment/`.env` эти два теста пропускаются.

## Lint

```bash
ruff check .
```

Дополнительные проверки:

```bash
ruff format --check .
python -m compileall app
```

## Swagger

- Swagger UI: http://127.0.0.1:8000/docs
- ReDoc: http://127.0.0.1:8000/redoc
- OpenAPI: http://127.0.0.1:8000/openapi.json

## PyCharm

Project interpreter: **`.venv/bin/python`** в корне проекта.

Откройте Settings → Project → Python Interpreter → Add Interpreter → Existing
и выберите этот файл. Создайте Run → Edit Configurations → Python:

- Target: **Module name** `uvicorn`.
- Parameters: `app.main:app --reload --host 127.0.0.1 --port 8000`.
- Working directory: корень проекта (`$PROJECT_DIR$`).
- Python interpreter: **Project Default**, привязанный к `.venv`.

Локальные настройки `.idea` и окружение `.venv` игнорируются Git.

## Endpoints

| Method | Path | Назначение |
| --- | --- | --- |
| GET | `/health` | Проверка работоспособности |
| POST | `/api/v1/transcriptions` | Audio → stub text, provisional |
| POST | `/api/v1/interviewer` | Решение об уточнении или завершении |
| POST | `/api/v1/evaluator` | LLM-оценка каждого входного критерия |
| POST | `/api/v1/question-bank/generate` | Генерация черновиков, provisional |

### Health

```bash
curl http://127.0.0.1:8000/health
```

Ответ: `{"status":"ok","service":"ml-service"}`.

### Transcription — PROVISIONAL CONTRACT

```bash
curl -X POST \
  -F "file=@sample.webm;type=audio/webm" \
  -F "language=ru" \
  http://127.0.0.1:8000/api/v1/transcriptions
```

`file` обязателен; `language` по умолчанию `ru`. Если Content-Type части файла
указан, он должен начинаться с `audio/`. Указание `type=audio/webm` в curl
исключает автоматическую отправку `application/octet-stream`.
Кодек не проверяется, ограничения размера в байтах нет. Продуктовый лимит
длительности и инфраструктурные ограничения требуют отдельного согласования.
Отсутствующий файл → 422; пустой файл или non-audio Content-Type → 400.

```json
{
  "text": "stub transcription",
  "language": "ru",
  "duration_seconds": null,
  "confidence": null
}
```

### Interviewer

```bash
curl -X POST http://127.0.0.1:8000/api/v1/interviewer \
  -H 'Content-Type: application/json' \
  --data-binary @- <<'JSON'
{
  "question": "Что делает GROUP BY в SQL?",
  "criteria": [
    {"criterion_id": "grouping", "description": "Объясняет группировку строк"}
  ],
  "dialogue": [
    {"role": "candidate", "content": "Группирует строки по указанным полям."}
  ],
  "follow_up_count": 0,
  "max_follow_ups": 3
}
JSON
```

```json
{
  "action": "follow_up",
  "message": "Можете подробнее обосновать это утверждение?"
}
```

При `follow_up_count >= max_follow_ups` ответ:
`{"action":"complete","message":null}` без вызова OpenRouter. Ниже лимита LLM
самостоятельно выбирает `follow_up` или ранний `complete`: лимит — верхняя граница,
а не цель. Уточнение должно давать новую диагностическую информацию без подсказок.
Оба счётчика обязательны и неотрицательны. Пример ответа выше иллюстративный.

### Evaluator

```bash
curl -X POST http://127.0.0.1:8000/api/v1/evaluator \
  -H 'Content-Type: application/json' \
  --data-binary @- <<'JSON'
{
  "question": "Что делает GROUP BY в SQL?",
  "target_level": "junior",
  "reference_answer": "GROUP BY объединяет строки по ключам для вычисления агрегатов.",
  "criteria": [
    {
      "criterion_id": "grouping",
      "description": "Объясняет группировку строк",
      "anchors": [
        {"score": 0, "description": "Объяснения нет или оно неверно"},
        {"score": 1, "description": "Группировка объяснена корректно"}
      ]
    }
  ],
  "dialogue": [
    {"role": "candidate", "content": "Группирует строки по указанным полям."}
  ]
}
JSON
```

```json
{
  "criteria": [
    {
      "criterion_id": "grouping",
      "score": 1,
      "status": "correct",
      "comment": "Описана группировка строк по полям.",
      "evidence": ["Группирует строки по указанным полям."]
    }
  ],
  "summary": "Назначение группировки раскрыто.",
  "recommendations": []
}
```

Пример иллюстрирует структуру, реальный результат зависит от LLM и rubric.
Возвращаются все входные критерии ровно один раз, с теми же ID. Выходное поле — **`comment`**.
ID критериев и баллы anchors уникальны; обязательны anchor 0 и положительный
anchor. Разреженные шкалы допустимы: evaluator может вернуть только балл,
явно заданный в anchors. `partial` требует `0 < score < max(anchors)`,
`correct` — максимальный anchor; `absent` — 0 и пустой evidence,
`incorrect` — 0. Для каждого non-absent результата требуется evidence.
Каждая цитата проверяется как дословный substring хотя бы одной реплики candidate
из всей цепочки; interviewer/reference/rubric не используются как источник evidence.
Эти правила проверяет `validate_evaluation` в service layer. Проверка substring
подтверждает наличие цитаты, а её релевантность критерию определяет LLM.

### Question bank — PROVISIONAL CONTRACT

```bash
curl -X POST http://127.0.0.1:8000/api/v1/question-bank/generate \
  -H 'Content-Type: application/json' \
  --data-binary @- <<'JSON'
{
  "specialization": "Аналитика",
  "topic": "SQL",
  "min_grade": "middle",
  "difficulty": "medium",
  "count": 2
}
JSON
```

Ответ содержит `questions` ровно из `count` черновиков (по умолчанию 10,
диапазон 1..50). Первый `question_text`: `[STUB 1] Технический вопрос по теме SQL`.
Каждый черновик содержит `reference_answers` по применимым грейдам и `criteria`
с `grade_configs`: `grade`, `weight=1.0`, `max_score=3`, `expected_level` и anchors
0, 1, 2, 3. Все тексты явно помечены `[STUB]`, ID детерминированы.

Грейды идут в порядке `junior < middle < senior`: junior включает все три,
middle — middle/senior, senior — только senior. `difficulty` — непустая
свободная строка, допустимая шкала пока не определена. В заглушке specialization
и difficulty принимаются как контекст, но не влияют на placeholder-содержание.
Ничего не сохраняется и не публикуется.

## Architecture and contracts

- `app/api/`: HTTP, upload validation, routing; `/api/v1` для четырёх функций.
- `app/schemas/`: строгие Pydantic v2 модели, `extra="forbid"`, ограничения
  строк/чисел и проверки связанных полей. Строки из одних пробелов отклоняются.
- `app/services/`: async orchestration и domain validation; `structured.py` ограничивает repair.
- `app/clients/openrouter.py`: auth, provider pinning, timeout, native strict JSON Schema
  из существующих response models, разбор completion/usage и безопасные ошибки.
- `app/settings.py`: environment/`.env`; секрет хранится как `SecretStr`.
- `app/prompts/`: полные versioned prompts: `interviewer_v2_structured_1`,
  `evaluator_v2_anchors_1`. Версии логируются при каждом attempt.
- Два system messages содержат prompt и trusted configuration; dialogue сохраняет
  порядок и роли `candidate → user`, `interviewer → assistant`. Interviewer не получает
  reference answer. Repair добавляет только нарушения ограничений в trusted message,
  без вставки недоверенного результата модели или candidate content.
- `tests/`: HTTP-контракты, ошибки входа, структурные и доменные инварианты.

Источники interviewer/evaluator: `json_schemas_v1.zip`, затем актуальные
`Схема БЗ.md` и backlog. Исходные локальные материалы не включены в поставку.
Сервис не считает overall score и не хранит пользователей, сессии или диалоги.

В логах — функция/endpoint, success/failure, HTTP status и время HTTP-запроса.
Каждый LLM attempt логирует service, model/provider, prompt version, attempt number,
latency, HTTP/upstream status, finish reason и input/output/reasoning tokens, если доступны.
Strict JSON Schema дополняется повторной Pydantic и domain validation;
невалидабельный результат не исправляется эвристиками.
Тела запросов, аудио, имена файлов, reference answers, candidate answers,
query strings и секреты не логируются приложением. Uvicorn пишет свой обычный
access log; не передавайте секреты в URL. Для отключения access log можно
передать Uvicorn `--no-access-log`. Ошибки схемы → 422, ошибки загрузки → 400;
необработанная серверная ошибка → 500 без stack trace в HTTP-ответе.

Технические справочники: [FastAPI UploadFile](https://fastapi.tiangolo.com/tutorial/request-files/),
[Pydantic validators](https://docs.pydantic.dev/latest/concepts/validators/).

## TODO: согласовать с backend

1. **Transcription — PROVISIONAL CONTRACT**: поля multipart/response, язык,
   политика метаданных duration/confidence, инфраструктурный byte limit,
   ответственность за продуктовый лимит длительности. Сейчас лимитов не добавлено.
2. **Question bank — PROVISIONAL CONTRACT**: финальная структура черновика,
   справочник difficulty и генерационный контекст. После согласования заменить
   provisional-контракт, обновить тесты и документацию.
3. Заменить transcription и question-bank generation реальным inference отдельными задачами.
4. Выполнить opt-in OpenRouter smoke в окружении с настроенным ключом перед rollout.
