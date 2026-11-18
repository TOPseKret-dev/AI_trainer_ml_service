# Интеграция Core Backend → ML: контракт v2 / MI-007

Согласованный контракт v2 реализован в ML Service 0.2.0.
Рабочие DTO, meta, каталог 17 ошибок и политика времени находятся в
[HTTP API](api.md); четыре канонические JSON Schema — в корне репозитория.
Общие старые примеры из intersim-docs с camelCase и маршрутами
/interviewer или /evaluator не являются wire-контрактом этой реализации.

## Подключение к Docker-сети

Для одного Docker host ML подключается к существующей сети Backend:

```bash
docker inspect intersim_backend --format '{{json .NetworkSettings.Networks}}'
```

В ML .env задайте OPENROUTER_API_KEY и фактическое имя сети в INTERSIM_NETWORK,
например intersim_intersim_internal. Во всех командах используйте оба файла:

```bash
docker compose -f compose.yaml -f compose.intersim.yaml config --quiet
docker compose -f compose.yaml -f compose.intersim.yaml up -d --build --wait --wait-timeout 120
python3 scripts/smoke.py
```

Override объявляет сеть external: ML не создаёт и не удаляет её.
Исходящий HTTPS к OpenRouter должен быть разрешён. PostgreSQL/Redis принадлежат
Core; ML не подключается к БД и не выполняет миграции.
Для нескольких Docker hosts потребуется маршрутизация или оркестратор.

Core использует base URL `http://ml-service:8001/`:

| Операция | Путь относительно base URL |
| --- | --- |
| Readiness | health/ready |
| Interviewer v2 | api/v1/interviewer |
| Evaluator v2 | api/v1/evaluator |
| Groq ASR, отдельный контракт | api/v1/transcriptions |
| Черновики банка, provisional | api/v1/question-bank/generate |

Frontend обращается к Core. ML доступен доверенным участникам внутренней сети;
host port привязан к 127.0.0.1. Публичный API Core и CORS фронтенда сюда не входят.

## HTTP-клиент Backend

Зарегистрируйте сервисный HttpClient с base URL из конфигурации. Например:

```csharp
builder.Services.AddHttpClient("ML", client =>
{
    client.BaseAddress = new Uri(
        builder.Configuration["ML_SERVICE_URL"] ?? "http://ml-service:8001/");
    client.Timeout = TimeSpan.FromSeconds(60);
});
```

ML_SERVICE_URL в Docker не должен указывать на localhost.
Backend timeout по MI-007 — 60 с по умолчанию, настраиваемый диапазон 30..120 с.
Для каждой HTTP-попытки Core передаёт deadline_at в UTC от начала своего запроса.
ML оставляет 5 с резерва и ограничивает весь вызов максимумом 55 с.
Таким образом 30/60/120 с на Core дают не более 25/55/55 с генерации.

Все предметные JSON-имена — snake_case. DTO Backend должен явно сериализовать
criterion_id, reference_answer, target_level, follow_up_count, max_follow_ups,
deadline_at и другие имена по схемам. Интерпретируйте HTTP status до чтения
предметного success DTO; при ошибке читайте error.code/message/details и optional meta.
Различайте null (неизвестно) и 0 (достоверный ноль).

## Последовательность обработки

1. Core создаёт TrainingSession и фиксирует выбранные опубликованные
   QuestionVersion, грейд и рубрику.
2. Сохраняет Answer/DialogueMessage до ML. В Interviewer передаёт вопрос,
   применимые критерии, полную цепочку диалога этого вопроса и счётчики уточнений.
3. При follow_up сохраняет message и увеличивает счётчик. При следующем ответе
   передаёт расширенный диалог. При complete закрывает текущий вопрос и управляет
   переходом к следующему. Повтор не должен создавать второе одинаковое уточнение.
4. Для Evaluator выбирает reference_answer фактического грейда из
   question_grade_configs выбранной версии. Передаёт только применимые критерии
   с description и полной шкалой anchors из criterion_grade_configs /
   criterion_grade_anchors. Weight, max_score и expected_level в JSON не передаются.
5. Сохраняет QuestionFeedback и CriterionResult по criterion_id. Evidence хранится
   как массив строк/jsonb. Summary/recommendations описывают один основной вопрос.
   Взвешенный результат вычисляется кодом Core по сохранённой рубрике.
6. Для каждого HTTP-вызова сохраняет одну запись LlmCall с агрегированными
   prompt_tokens, completion_tokens и duration_ms из meta; provider_attempts
   показывает 0..2 физических OpenRouter-вызова. Не добавляет reasoning_tokens
   второй раз к completion и не дублирует расход repair отдельным учётом.

При отсутствии candidate-реплики Evaluator возвращает ML_NOTHING_TO_EVALUATE.
Неполная рубрика даёт ML_INVALID_INPUT до вызова провайдера.
LLM генерирует только предметный JSON; meta и error формирует Python.
Отдельного ML API общего LLM-резюме всей тренировки пока нет.

## Ошибки, идемпотентность и повтор

ML выполняет максимум один внутренний repair только для EMPTY_RESPONSE,
OUTPUT_TRUNCATED и SCHEMA_ERROR. Сетевые retry, fallback и очередь LLM отсутствуют.
Вызовы синхронные, без task ID/polling/retry endpoints ML.
Все 17 кодов и точные лимиты Backend перечислены в [HTTP API](api.md).

Backend определяет профиль по error.code, учитывает Retry-After и хранит бюджет
попыток отдельно от ML provider_attempts. Interviewer не повторяется автоматически.
Evaluator может автоматически повторяться только при временной ошибке L.
Для 502 после разрешённого repair допустим только ручной повтор.
Auth/quota/config ошибки провайдера возвращаются как 503 с отдельными кодами,
а не как 401 пользователя InterSim и не как временный rate limit.

Ответы уже завершённой/устаревшей операции не применяются к новой версии состояния.
Повтор HTTP не создаёт новый Answer и не перезаписывает готовый QuestionFeedback.
Эти правила, persistence и фоновые циклы реализуются в Core; ML хранит только
контекст текущего HTTP-запроса в памяти.

## Проверка совместимости

```bash
python -m app.contracts --check
python -m pytest
python3 scripts/smoke.py
```

Unit/contract tests заменяют только HTTP transport OpenRouter, проверяют весь
путь FastAPI → validation → service → client → result/error и не тратят inference.
Smoke проверяет живой процесс и детерминированный hard limit с meta, без LLM.
При необходимости импортируйте четыре схемы из корня в контрактные тесты Core.
Старые архивные схемы из docs/reference не заменяют контракт v2.

Groq ASR имеет собственные поля, deadline и error codes:
[ASR](asr.md). Контракт генератора банка остаётся provisional.
Смена модели, провайдера и промптов для миграции v2 не выполнялась.
