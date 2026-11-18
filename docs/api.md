# HTTP API ML-сервиса — контракт v2 / MI-007

Согласованный контракт Interviewer/Evaluator реализован в версии приложения 0.2.0.
В Docker base URL — `http://ml-service:8001`; локально — `http://127.0.0.1:8001`.
Запросы и ответы двух LLM-маршрутов используют `application/json` и snake_case.

| Method | Path | Назначение |
| --- | --- | --- |
| GET | `/health` | Liveness |
| GET | `/health/live` | Liveness |
| GET | `/health/ready` | Непустой OpenRouter key; без inference |
| POST | `/api/v1/interviewer` | Диагностическое уточнение или завершение вопроса, v2 |
| POST | `/api/v1/evaluator` | Оценка одного вопроса и его уточнений, v2 |
| POST | `/api/v1/transcriptions` | [Groq ASR](asr.md), отдельный контракт |
| POST | `/api/v1/question-bank/generate` | Детерминированные черновики, provisional |

OpenAPI: `GET /openapi.json`; Swagger UI: `GET /docs`.
Четыре канонические JSON Schema Draft 2020-12 находятся в корне репозитория:

- [interviewer_input.schema.json](../interviewer_input.schema.json);
- [interviewer_output.schema.json](../interviewer_output.schema.json);
- [evaluator_input.schema.json](../evaluator_input.schema.json);
- [evaluator_output.schema.json](../evaluator_output.schema.json).

Схемы генерируются из runtime Pydantic-моделей командой
`python -m app.contracts`. Проверка без изменения файлов:
`python -m app.contracts --check`. Output schema содержит `oneOf` успешного
HTTP-ответа и ошибки. Объект `meta` обязателен только при успехе.
JSON Schema описывает статические ограничения; уникальность ID, полная шкала
anchors и соответствие результата входной рубрике дополнительно проверяются Python.

## Правила входного JSON

Неизвестные поля запрещены на всех уровнях. Обязательные строки непустые и
содержат хотя бы один непробельный символ. Счётчики и score — строгие целые
JSON-числа: `true`, `"1"` и `1.5` отклоняются.

`dialogue` — непустой хронологический массив по одному основному вопросу.
Каждая реплика содержит только `role` (`candidate | interviewer`) и
`content` (непустая строка). Реплики кандидата являются недоверенными данными.

Общее необязательное поле `deadline_at` — RFC3339 UTC string с `Z` или
`+00:00`, например `2030-01-01T10:00:00Z`. Naive datetime, иной UTC offset,
числовой timestamp и явный `null` отклоняются. Поле можно полностью опустить.
Deadline используется только Python-кодом и никогда не добавляется в LLM prompt.

Ошибки входа двух маршрутов: HTTP 422 / `ML_INVALID_INPUT`, без провайдерского
вызова. В ответе нет FastAPI `detail` и значений невалидных полей.

## Interviewer

Все поля, кроме `deadline_at`, обязательны.

| Поле | Содержание |
| --- | --- |
| `question` | Текст текущего основного вопроса |
| `criteria` | Непустой массив критериев с уникальными `criterion_id` |
| `criteria[].criterion_id` | Непустая строка |
| `criteria[].description` | Непустое описание |
| `dialogue` | Вся сохранённая цепочка реплик текущего вопроса |
| `follow_up_count` | Целое число сделанных уточнений, ≥ 0 |
| `max_follow_ups` | Целый предел уточнений, ≥ 0 |
| `deadline_at` | Необязательный абсолютный deadline Backend |

```bash
curl -X POST http://127.0.0.1:8001/api/v1/interviewer \
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

Пример HTTP 200; usage и длительность здесь иллюстративные:

```json
{
  "action": "follow_up",
  "message": "Можете подробнее обосновать это утверждение?",
  "meta": {
    "prompt_tokens": 120,
    "completion_tokens": 30,
    "reasoning_tokens": null,
    "duration_ms": 2500,
    "provider_attempts": 1
  }
}
```

`follow_up` требует непустой `message`. `complete` требует строго
`message: null` и закрывает только текущий основной вопрос.
Следующий вопрос или завершение тренировки выбирает Core.

При `follow_up_count >= max_follow_ups` возвращается `complete` без вызова
провайдера: `provider_attempts=0`, `prompt_tokens=0`, `completion_tokens=0`,
`reasoning_tokens=null`; длительность измеряется.
Просроченный deadline всё равно возвращает `ML_TIMEOUT`.
Ниже лимита модель может завершить вопрос раньше. Она не получает эталон,
не раскрывает ответ, не выставляет баллы и не сообщает правильность ответа.

## Evaluator

Все поля, кроме `deadline_at`, обязательны.

| Поле | Содержание |
| --- | --- |
| `question` | Текст одного основного вопроса |
| `target_level` | `junior | middle | senior` |
| `reference_answer` | Непустой эталон текущего грейда |
| `criteria` | Непустой массив применимых критериев с уникальными ID |
| `criteria[].criterion_id` | Непустой ID, сохраняемый в результате |
| `criteria[].description` | Непустое описание |
| `criteria[].anchors` | Минимум два объекта `score`, `description` |
| `dialogue` | Вся цепочка ответов и уточнений текущего вопроса |
| `deadline_at` | Необязательный абсолютный deadline Backend |

Anchors образуют полную последовательность `0..max_score`, без пропусков и
повторяющихся score. Максимум индивидуален для критерия: поддерживаются 0..1,
0..2, 0..3 и более длинные шкалы. Разреженные шкалы в v2 отклоняются до inference.
Порядок anchors во входном массиве не влияет на допустимость полной шкалы.
В JSON критерия нет `weight`, `max_score`, `expected_level` или `grade_expectations`.
Максимум выводится из anchors; веса остаются у Backend.

Если DTO корректен, но в dialogue нет непустой реплики `candidate`,
возвращается HTTP 422 / `ML_NOTHING_TO_EVALUATE` без провайдерского вызова.
Пустой массив dialogue или пробельный content нарушают сам input schema и дают
`ML_INVALID_INPUT`.

```bash
curl -X POST http://127.0.0.1:8001/api/v1/evaluator \
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

Пример HTTP 200:

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
  "recommendations": [],
  "meta": {
    "prompt_tokens": 1700,
    "completion_tokens": 420,
    "reasoning_tokens": null,
    "duration_ms": 6200,
    "provider_attempts": 1
  }
}
```

Каждый входной `criterion_id` возвращается ровно один раз; лишние, пропущенные
и повторяющиеся ID запрещены. Core сопоставляет результаты по ID, порядок
критериев в ответе не фиксирован. Все пять полей критерия обязательны.

| status | score | evidence |
| --- | --- | --- |
| `absent` | 0 | Только пустой массив |
| `incorrect` | 0 | Минимум одна цитата кандидата |
| `partial` | Строго между 0 и максимумом anchors | Минимум одна цитата кандидата |
| `correct` | Максимум anchors | Минимум одна цитата кандидата |

Балл обязательно принадлежит anchors данного критерия; на шкале 0..1
`partial` невозможен. Выходное поле — `comment`, непустая строка.
`evidence` всегда массив уникальных непустых строк. Каждая строка проверяется
как дословная подстрока одной из реплик `candidate`. Интервьюер, эталон и
рубрика не используются как источник evidence. Семантическую релевантность
цитат и оценку определяет модель; Python проверяет наличие и инварианты.

`summary` обязателен и непуст. `recommendations` — обязательный массив
непустых строк, который может быть пустым. Оба поля относятся к одному
основному вопросу с уточнениями. Общий взвешенный score рассчитывает Backend.
Отдельного API общего LLM-резюме всей тренировки в этой версии нет.

## Метаданные meta

`meta` формирует сервис. OpenRouter вызывается в JSON object mode; предметный
результат проходит строгую проверку Pydantic и доменных инвариантов до HTTP-ответа.
Interviewer генерирует только action/message, Evaluator — только
criteria/summary/recommendations. Поля `meta` и `error` модель не генерирует.

| Поле | Правило |
| --- | --- |
| `prompt_tokens` | Обязательное integer ≥ 0 или null, из usage.prompt_tokens |
| `completion_tokens` | Обязательное integer ≥ 0 или null, из usage.completion_tokens |
| `reasoning_tokens` | Необязательное integer ≥ 0 или null, из completion_tokens_details |
| `duration_ms` | Обязательное integer ≥ 0, wall-clock всего HTTP-запроса |
| `provider_attempts` | Обязательное integer 0..2, реально начатые OpenRouter-вызовы |

Каждая метрика суммируется по исходному вызову и repair только тогда, когда её
сообщили **все** начатые попытки. Отсутствующая или некорректная метрика даёт
`null`; частичная сумма не выдаётся за полный расход. При отсутствии вызовов
prompt/completion равны 0, reasoning остаётся null.

Reasoning обычно уже входит в completion: суммировать для общего расхода нужно
prompt + completion, без повторного добавления reasoning. Если любое слагаемое
неизвестно, точный общий расход неизвестен.
Каждый HTTP-вызов Core→ML представляет одну агрегированную запись LlmCall;
Backend не должен дополнительно учитывать те же внутренние repair-вызовы.

При ошибке meta возвращается best-effort. Невозможность собрать метаданные
не заменяет исходную ошибку провайдера. При разрыве соединения остаётся безопасная
запись в логах; скрытая фоновая генерация не продолжается.

## Ошибки и Retry-After

Оба маршрута возвращают только согласованную оболочку, без `detail`, stack trace,
сырых сообщений OpenRouter или значений невалидного input.

Пример HTTP 503 с заголовками `Retry-After: 10` и `X-Correlation-ID`:

```json
{
  "error": {
    "code": "ML_RATE_LIMITED",
    "message": "ML provider rate limited the request",
    "details": {
      "operation": "evaluator",
      "retryable": true,
      "retry_after_seconds": 10,
      "correlation_id": "call-123",
      "provider_attempts": 1
    }
  },
  "meta": {
    "prompt_tokens": null,
    "completion_tokens": null,
    "reasoning_tokens": null,
    "duration_ms": 100,
    "provider_attempts": 1
  }
}
```

Обязательны только error/code/message. В details допускаются исключительно
operation, retryable, retry_after_seconds, correlation_id, provider_attempts.
operation соответствует маршруту, retryable — boolean; retry_after_seconds —
integer ≥ 0 или null; correlation_id — string или null; provider_attempts —
integer 0..2 или null. Details и meta могут отсутствовать.
Сервис генерирует безопасный correlation ID и возвращает его также в HTTP header.

| error.code | HTTP | Backend auto-retry допустим | Внутренний repair |
| --- | --- | --- | --- |
| ML_INVALID_INPUT | 422 | Нет, исправить запрос | Нет |
| ML_NOTHING_TO_EVALUATE | 422 | Нет, нужен ответ кандидата | Нет |
| ML_PROVIDER_NOT_CONFIGURED | 503 | Нет, исправить конфигурацию | Нет |
| ML_PROVIDER_AUTH_ERROR | 503 | Нет, исправить доступ | Нет |
| ML_PROVIDER_QUOTA_EXCEEDED | 503 | Нет, пополнить квоту | Нет |
| ML_PROVIDER_CONFIG_ERROR | 503 | Нет, исправить конфигурацию | Нет |
| ML_RATE_LIMITED | 503 | По политике временных ошибок L | Нет |
| ML_UNAVAILABLE | 503 | По политике L | Нет |
| ML_QUEUE_FULL | 503 | По политике L | Нет |
| ML_TIMEOUT | 504 | По политике L | Нет |
| ML_QUEUE_TIMEOUT | 504 | По политике L | Нет |
| ML_EMPTY_RESPONSE | 502 | Только ручной повтор после repair | Один |
| ML_OUTPUT_TRUNCATED | 502 | Только ручной повтор после repair | Один |
| ML_SCHEMA_ERROR | 502 | Только ручной повтор после repair | Один |
| ML_INVALID_RESPONSE | 502 | Только ручной повтор | Нет |
| ML_POLICY_REFUSAL | 422 | Нет | Нет |
| ML_INTERNAL_ERROR | 500 | Нет | Нет |

`retryable=true` возвращается для L и означает допустимость повтора по политике,
а не команду немедленно повторять. Для S (невалидный результат) retryable=false:
после repair разрешён только ручной повтор Backend.
Очереди LLM здесь нет; QUEUE_FULL и QUEUE_TIMEOUT поддержаны общим интерфейсом
ошибок для будущего admission/queue слоя. Новая очередь ради этих кодов не создана.

OpenRouter 401/403 → PROVIDER_AUTH_ERROR, 402 → PROVIDER_QUOTA_EXCEEDED,
429 → RATE_LIMITED, 408/504 → TIMEOUT, прочие 5xx → UNAVAILABLE.
Подтверждённые machine codes конфигурации или `param` из известных параметров
ML-запроса дают PROVIDER_CONFIG_ERROR. Неизвестные 4xx и повреждённая protocol
envelope дают INVALID_RESPONSE без repair. Обычный текст error.message не
используется для догадок о причинах.
Ошибки внутри HTTP 200 и choices проверяются до принятия content.
Явный refusal/content_filter не исправляется; finish_reason=length не принимается
даже при синтаксически корректном JSON.

Корректный Retry-After из 429 нормализуется в секунды одновременно в HTTP header
и details; поддерживаются целые секунды и HTTP-date с округлением вверх.
ML не спит в ожидании этого срока и не повторяет сетевой запрос.

## Один deadline и один repair

Без deadline_at локальный лимит — LLM_REQUEST_TIMEOUT_SECONDS, по умолчанию 55 с,
допустимо уменьшение; максимальное значение — 55 с.
С абсолютным deadline:

```text
effective_budget = min(local_limit <= 55s, deadline_at - received_at_utc - 5s)
```

Если бюджет ≤ 0, возвращается ML_TIMEOUT без OpenRouter-вызова.
UTC deadline один раз переводится в монотонный срок от начала HTTP-запроса.
Исходная генерация, repair, парсинг и валидация делят один бюджет. Timeout
каждого обращения httpx ограничен оставшимся временем и OPENROUTER_TIMEOUT_SECONDS.
Перед следующим обращением и перед успешным HTTP-ответом проверяется остаток.
duration_ms измеряется perf_counter и включает подготовку и формирование ответа.

Только EMPTY_RESPONSE, OUTPUT_TRUNCATED и SCHEMA_ERROR могут запустить один
repair с той же моделью, провайдером, температурой и доверенной конфигурацией.
Добавляется безопасная system-инструкция с типом нарушения, без сырого content
и текста ошибок Pydantic. Максимум два физических provider-вызова.
Если repair завершился сетевой ошибкой, возвращается фактическая конечная ошибка
и агрегат usage обеих попыток. Третьего обращения не бывает.
Нет сетевых retries, model/provider fallback и фоновых LLM-повторов.

## Политика повторов Backend

Core сохраняет Answer до ML, обеспечивает идемпотентность и не применяет ответ
устаревшей операции. ML не создаёт записи тренировок и не обрабатывает
X-Idempotency-Key. Вызовы синхронные; task ID и polling API ML отсутствуют.

- Interviewer: без автоматических HTTP-повторов; исходная попытка и максимум
  два ручных повтора на логическую операцию. Интервал ≥2 с и не меньше Retry-After.
- Evaluator, временная ошибка L: максимум три HTTP-попытки за цикл; backoff 1 с,
  затем 2 с, jitter 0..0.5 с, с учётом Retry-After.
- Evaluator: до трёх ручных запусков feedback/retry за окно, затем cooldown
  15 минут; общий предел — 12 HTTP-вызовов на основной вопрос, включая фоновые
  и ручные циклы. Cooldown не сбрасывает этот предел.
- Для S автоматических HTTP-повторов нет. Фоновое восстановление допускается
  только для Evaluator с L и оставшимся бюджетом. Для N сначала устраняется причина.

Эта оркестрация реализуется Backend; внутри ML её нет.

## Остальные маршруты

Health остаётся прежним: liveness отвечает
`{"status":"ok","service":"ml-service"}`; readiness —
`{"status":"ready","service":"ml-service"}` при непустом ключе.
Отсутствующий key даёт прежний HTTP 503 с detail на health/ready.
Readiness не проверяет баланс или доступность провайдера.

Groq ASR сохраняет свой [контракт, настройки и лимиты](asr.md); каталог ML_*
и поле deadline_at описаны здесь только для Interviewer/Evaluator.

Генерация банка остаётся provisional, без публикации и persistence:

```bash
curl -X POST http://127.0.0.1:8001/api/v1/question-bank/generate \
  -H 'Content-Type: application/json' \
  --data-binary @- <<'JSON'
{"specialization":"Аналитика","topic":"SQL","min_grade":"middle","difficulty":"medium","count":2}
JSON
```

Возвращает count черновиков (по умолчанию 10, диапазон 1..50) с явными метками
[STUB], эталонами и критериями для грейдов от min_grade до senior.
В генераторе criterion grade_configs включают weight/max_score/expected_level;
они не являются входными полями Evaluator v2. Формат ошибок и validation остальных
маршрутов этой миграцией не изменён.
