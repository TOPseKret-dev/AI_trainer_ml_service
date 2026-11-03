# Интеграция с сервисами InterSim

## Проверенные источники

Конфигурация сверена 03.11.2026 с приватными репозиториями организации:

- [intersim-docs](https://github.com/intersim-org/intersim-docs/tree/6d97477244fe87d7f3aab8f2500e651dbc8f4eb6) — `main`, `6d974772`.
- [intersim-backend](https://github.com/intersim-org/intersim-backend/tree/1c1863407c2aa3f155330792e2e46f54b1a3935e) — `dev`, `1c186340`.
- [intersim-frontend](https://github.com/intersim-org/intersim-frontend/tree/57d55c6aa47db3920c3d5f2a717f1b0c670e739d) — `main`, `57d55c6a`.

В production Compose Backend использует сервис `backend` на порту 5000 и сеть
с логическим ключом `intersim_internal`. ML подключается к той же сети как
`ml-service:8001`. Сервису нужен исходящий HTTPS к OpenRouter; сеть не должна
запрещать этот выход через `internal: true` без отдельного egress-маршрута.

## Подключение к существующему Core-стеку

Сначала запустите существующий production Compose Backend с его штатными
PostgreSQL и Redis. Узнайте фактическое имя сети:

```bash
docker inspect intersim_backend --format '{{json .NetworkSettings.Networks}}'
```

Compose обычно добавляет префикс имени проекта. Например, при `-p intersim`
имя сети будет `intersim_intersim_internal`; на другом сервере оно может отличаться.
В `.env` ML задайте найденное значение `INTERSIM_NETWORK`, а также ключ OpenRouter.
Далее из каталога ML:

```bash
docker compose -f compose.yaml -f compose.intersim.yaml config --quiet
docker compose -f compose.yaml -f compose.intersim.yaml up -d --build --wait --wait-timeout 120
python3 scripts/smoke.py
```

Override помечает сеть как external: ML не создаёт её и не удаляет при остановке.
Те же два `-f` нужны для `logs`, `stop`, `down` и дальнейших `up`.
Не запускайте этот же проект в одном случае с override, а в другом без него.

Проверка DNS и HTTP из временного соседнего контейнера, без LLM-вызова:

```bash
docker run --rm --network <фактическое-имя-сети> intersim-ml:local \
  python -c "import urllib.request; print(urllib.request.urlopen('http://ml-service:8001/health/ready', timeout=5).read().decode())"
```

Если образ называется иначе, замените `intersim-ml:local` на значение `ML_IMAGE`.
Связь между Compose-проектами работает на одном Docker host. Для нескольких
серверов понадобится отдельная маршрутизация или оркестратор.

## Адрес и HTTP-клиент Core Backend

В контейнере Core base URL должен быть `http://ml-service:8001/`, а не `localhost`:

| Вызов | Маршрут относительно base URL |
| --- | --- |
| Readiness | `health/ready` |
| Уточнение | `api/v1/interviewer` |
| Оценка | `api/v1/evaluator` |
| Транскрибация, пока stub | `api/v1/transcriptions` |
| Генерация черновиков, пока stub | `api/v1/question-bank/generate` |

В проверенной ветке Backend нет регистрации ML HttpClient и вызовов Interviewer/
Evaluator: поднятая сеть обеспечивает транспортную связь, а бизнес-интеграция
требует отдельного изменения Backend. Следующий пример показывает настройку
клиента для этого изменения; одни переменные environment ещё не добавят его в Core:

```csharp
builder.Services.AddHttpClient("ML", client =>
{
    client.BaseAddress = new Uri(
        builder.Configuration["ML_SERVICE_URL"] ?? "http://ml-service:8001/");
    client.Timeout = TimeSpan.FromSeconds(60);
});
```

`ML_SERVICE_URL=http://ml-service:8001/` можно добавить в environment Core после
внедрения такого клиента. JSON-поля ML имеют `snake_case`; отправляйте DTO с
нужными JSON-именами согласно [рабочему контракту](api.md) или `/openapi.json`.
В Evaluation передавайте anchors и reference_answer текущего грейда, без weight.

Вызовы модели могут занять весь бюджет 55 секунд, включая один repair. Таймаут
Core должен быть больше этого бюджета; старое значение 30 секунд из общего
`.env.example` для этой конфигурации недостаточно. Backend сохраняет ответ
пользователя до вызова AI и управляет повтором/идемпотентностью на своём уровне.

## Последовательность работы Core → ML

1. Core создаёт тренировку, выбирает опубликованные версии вопросов и фиксирует
   их в своей БД. ML не выбирает вопросы из банка и не создаёт сессию.
2. После ответа пользователя Core сохраняет реплику и передаёт Interviewer текст
   текущего основного вопроса, описания его применимых критериев, всю цепочку
   диалога по этому вопросу и счётчики уточнений.
3. При `follow_up` Core сохраняет уточнение, увеличивает счётчик и возвращает его
   фронтенду. После следующего ответа передаёт расширенную цепочку. При `complete`
   закрывает только текущий вопрос и управляет переходом к следующему.
4. Для оценки Core собирает Evaluator request по грейду, зафиксированному для
   тренировки: эталон из `question_grade_configs`, критерии и anchors из
   `criterion_grade_configs` / `criterion_grade_anchors` выбранной версии.
   Критерии без конфигурации этого грейда не включаются. Эталон другого грейда
   не подставляется. В JSON критерия передаются только `criterion_id`,
   `description`, `anchors`; веса и `max_score` остаются в Core.
5. Core сохраняет результаты по `criterion_id`, комментарии, массивы evidence,
   summary и recommendations. Итоговые взвешенные баллы и пользовательскую
   историю формирует Core. Evaluator возвращает разбор одного основного вопроса,
   а отдельного вызова для общего LLM-резюме всей тренировки сейчас нет.

Оба LLM-маршрута возвращают результат в ответе на тот же HTTP-запрос; ML не
выдаёт task ID и не предоставляет polling/retry API. Фоновые задачи, статус
обработки для фронтенда, повторы и `X-Idempotency-Key` реализуются в Core.
Повтор POST к ML может означать новый платный вызов модели. ML не сохраняет
присланный диалог между запросами, поэтому Core передаёт его целиком для каждого
хода и оценки текущего вопроса.

## Границы публичного API

Frontend обращается к Core Backend через существующий Nginx. ML работает во
внутренней сети и не требует CORS. Host port привязан только к `127.0.0.1`,
для диагностики и smoke на сервере. У ML нет пользовательской авторизации;
доступ к нему даётся доверенным участникам Docker-сети. Публичный proxy-маршрут
к ML в Nginx здесь не добавляется.

## Расхождения общих документов

`intersim-docs/docker-compose.yml` содержит ранний шаблон с Python/asyncpg Backend
и `localhost` в ML_SERVICE_URL. Для интеграции использован фактический .NET Compose
из `intersim-backend/docker-compose.prod.yml`.

В `api_spec.md` остались примеры `/interviewer`, `/evaluator` с camelCase-полями;
фактические маршруты ML — `/api/v1/interviewer` и `/api/v1/evaluator`, поля —
snake_case по JSON-схемам. API-клиент должен использовать рабочий контракт ML.
Изменения STT MI-003 от 03.11.2026 в общем документе помечены предложением:
они не становятся реализованным API при добавлении Docker.

Контракт генерации пока provisional и требует настройки каждого критерия для
всех грейдов вопроса; схема БЗ допускает отсутствие критерия на отдельном грейде.
Согласование генератора, реальный ASR, сохранение LLM request/response и клиент
Core остаются отдельными задачами. Существующие пользовательские данные,
PostgreSQL и Redis ML Compose не изменяет.
