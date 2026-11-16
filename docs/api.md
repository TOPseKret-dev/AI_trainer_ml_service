# HTTP API ML-сервиса

Рабочий контракт текущей реализации. Base URL в Docker: `http://ml-service:8001`;
для команд на сервере: `http://127.0.0.1:8001`. Все бизнес-маршруты имеют префикс `/api/v1`.


| Method | Path | Назначение |
| --- | --- | --- |
| GET | `/health` | Проверка работоспособности |
| GET | `/health/live` | Liveness, аналог `/health` |
| GET | `/health/ready` | Готовность локальной конфигурации LLM, без inference |
| POST | `/api/v1/transcriptions` | Groq ASR; see [ASR contract](asr.md) |
| POST | `/api/v1/interviewer` | Решение об уточнении или завершении |
| POST | `/api/v1/evaluator` | LLM-оценка каждого входного критерия |
| POST | `/api/v1/question-bank/generate` | Генерация черновиков, provisional |

Полные схемы текущей версии: `GET /openapi.json`; интерактивная документация:
`GET /docs`. При изменении реализации сверяйте примеры с этими схемами.

### Общие правила JSON

- Для JSON-маршрутов нужен `Content-Type: application/json`. Неизвестные поля
  запрещены во всех вложенных JSON-объектах и дают 422.
- Обязательные строки непустые и содержат хотя бы один непробельный символ.
- `dialogue` — непустой массив реплик в порядке разговора по **одному основному
  вопросу**, включая его уточнения. Каждая реплика содержит только `role`
  (`candidate` или `interviewer`) и `content` (непустая строка).
- `target_level` и `min_grade` принимают только `junior`, `middle`, `senior`.
- Счётчики уточнений, баллы anchors и `count` генерации — целые JSON-числа;
  строки `"1"`, дробные числа и boolean вместо них не принимаются.

ML не создаёт сессии, не сохраняет ответы и не обрабатывает
`X-Idempotency-Key`. Повторный POST к LLM-маршруту может вызвать модель заново.
Управление состоянием и повторной обработкой находится в Core Backend.

### Health

```bash
curl http://127.0.0.1:8001/health
```

Ответ: `{"status":"ok","service":"ml-service"}`.

Readiness: `GET /health/ready` → `{"status":"ready","service":"ml-service"}`.
Без ключа возвращает 503. Это проверка конфигурации, а не доступности OpenRouter.

### LLM: бюджет времени и ошибки

Общий бюджет Interviewer/Evaluator — `LLM_REQUEST_TIMEOUT_SECONDS` (55 секунд),
включая один repair при нарушении structured/domain контракта. По истечении
бюджета текущий запрос отменяется, ответ API — 504. Отдельные HTTP-операции
дополнительно ограничены `OPENROUTER_TIMEOUT_SECONDS` (55 секунд).
Сетевые и provider retries отсутствуют; максимум два платных calls на API-запрос.

| Код | Значение |
| --- | --- |
| 400 | Пустой аудиофайл или non-audio Content-Type |
| 422 | Ошибка входной схемы; стандартный FastAPI validation response |
| 502 | Невалидный ответ провайдера или неуспешный repair |
| 503 | Нет ключа, provider/auth/rate-limit/transport error; возможен также лимит Uvicorn |
| 504 | Истёк общий LLM-бюджет или transport timeout |
| 500 | Необработанная серверная ошибка, без stack trace в HTTP-ответе |

Обработанные ошибки сервиса имеют вид `{"detail":"фиксированное сообщение"}`.
Upstream response наружу не передаётся. Собственные ошибки Uvicorn могут иметь
текстовое тело; Core должен проверять HTTP status до разбора JSON.

### Transcription

See [ASR API, configuration and limitations](asr.md). This endpoint uses the
`error.code/message/details` envelope; the legacy `detail` format above applies
to the other ML endpoints.

### Interviewer

| Поле | Обязательное содержимое |
| --- | --- |
| `question` | Текст текущего основного вопроса |
| `criteria` | Непустой массив объектов `criterion_id`, `description` |
| `dialogue` | Вся сохранённая цепочка реплик текущего вопроса |
| `follow_up_count` | Целое число уже сделанных уточнений, ≥ 0 |
| `max_follow_ups` | Целый предел уточнений, ≥ 0 |

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

`follow_up` означает, что Core сохраняет `message` как уточнение и ожидает
следующий ответ пользователя. `complete` завершает только текущий основной
вопрос: следующий вопрос или завершение всей тренировки выбирает Core.
При `complete` поле `message` обязательно присутствует и равно `null`.
Interviewer не получает эталонный ответ и не возвращает оценку кандидата.

### Evaluator

| Поле | Обязательное содержимое |
| --- | --- |
| `question` | Текст одного основного вопроса |
| `target_level` | Грейд сотрудника: `junior`, `middle` или `senior` |
| `reference_answer` | Эталон именно для этого грейда |
| `criteria` | Непустой массив применимых к грейду критериев |
| `dialogue` | Вся цепочка реплик этого вопроса, включая ответы на уточнения |

Объект критерия содержит только `criterion_id`, `description`, `anchors`;
объект anchor — только `score`, `description`. Максимальный балл определяется
по anchors. Поля `weight`, `max_score`, `expected_level`, `sessionId` и другие
поля из БД или старых примеров API сюда не добавляются: неизвестные поля дают 422.

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
Core сопоставляет результаты по `criterion_id`; порядок критериев в ответе
не обязан совпадать с запросом. `evidence` — массив строк, включая пустой массив
для `absent`, а не одна строка. `summary` и `recommendations` относятся к этому
основному вопросу с его уточнениями. Общий взвешенный балл рассчитывает Core;
отдельного API общего LLM-резюме всей тренировки здесь нет.

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
curl -X POST http://127.0.0.1:8001/api/v1/question-bank/generate \
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
