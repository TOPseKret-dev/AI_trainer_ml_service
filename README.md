# InterSim ML service

Stateless HTTP service на Python 3.11+ (рекомендуется 3.12), FastAPI, Uvicorn и Pydantic v2.
Все четыре ML-функции — **детерминированные STUB**, без реальных LLM, ASR,
внешних API, БД, авторизации и хранения истории. Endpoint генерации возвращает
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
Переменные окружения и API keys не нужны; `.env.example` служит пояснением,
автоматической загрузки `.env` нет. Версии зависимостей ограничены в `pyproject.toml`.
Команды ниже выполняются после активации `.venv`.

## Run

```bash
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Эквивалентный запуск: `python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000`.

## Tests

```bash
pytest
```

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
| POST | `/api/v1/evaluator` | Stub-оценка каждого входного критерия |
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
`{"action":"complete","message":null}`. Stub не анализирует ответ и не даёт подсказок.
Оба счётчика обязательны и неотрицательны.

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
      "score": 0,
      "status": "absent",
      "comment": "Stub evaluator: real evaluation is not implemented yet.",
      "evidence": []
    }
  ],
  "summary": "Stub evaluation: real LLM evaluation is not implemented yet.",
  "recommendations": []
}
```

Это заглушка, а не реальная оценка качества ответа. Возвращаются все входные
критерии в исходном порядке, с теми же ID. Выходное поле — **`comment`**.
ID критериев и баллы anchors уникальны; обязательны anchor 0 и положительный
anchor. Разреженные шкалы допустимы: evaluator может вернуть только балл,
явно заданный в anchors. `partial` требует `0 < score < max(anchors)`,
`correct` — максимальный anchor; `absent` — 0 и пустой evidence,
`incorrect` — 0. Эти правила проверяет `validate_evaluation` в service layer.

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
- `app/services/`: отдельные async stub-функции. Реальный inference впоследствии
  подключается здесь с сохранением сигнатур, маршрутов и HTTP-контрактов.
- `tests/`: HTTP-контракты, ошибки входа, структурные и доменные инварианты.

Источники interviewer/evaluator: `json_schemas_v1.zip`, затем актуальные
`Схема БЗ.md` и backlog. Исходные локальные материалы не включены в поставку.
Сервис не считает overall score и не хранит пользователей, сессии или диалоги.

В логах — функция/endpoint, success/failure, HTTP status и время HTTP-запроса.
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
3. Заменить четыре stub-реализации реальным inference отдельной задачей;
   текущая поставка не выполняет реальную оценку, генерацию или транскрибацию.
