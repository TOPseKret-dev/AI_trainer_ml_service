# InterSim ML Service

Внутренний HTTP-сервис InterSim на Python 3.12, FastAPI и Pydantic v2.
Core Backend передаёт контекст вопроса и диалог; ML возвращает уточнение или оценку.
Interviewer/Evaluator реализуют согласованный контракт v2 / MI-007: обязательная
meta на успехе, 17 кодов ошибок, deadline_at и максимум один semantic repair.
OpenRouter использует JSON object mode с обязательной валидацией результата
в Python; особенности совместимости Alibaba описаны в [документации](docs/development.md).

| Функция | Состояние |
| --- | --- |
| Interviewer | OpenRouter, решение `follow_up / complete` |
| Evaluator | OpenRouter, оценка по anchors и проверка evidence |
| Transcription | Groq Whisper large-v3-turbo, WebM/Opus |
| Question bank generation | Детерминированные черновики `[STUB]` |

Историю, авторизацию, публикацию вопросов и итоговый взвешенный score ведёт Core Backend.
Evaluator возвращает разбор одного основного вопроса с его уточнениями.
Отдельного API для общего LLM-резюме всей тренировки в этой версии нет.

## Запуск в Docker

Нужны Docker Engine и плагин Docker Compose >= 2.20. Из нового клона:

```bash
cp .env.example .env
chmod 600 .env
```

Заполните `OPENROUTER_API_KEY` в `.env`, затем:

```bash
docker compose config --quiet
docker compose up -d --build --wait --wait-timeout 120
curl --fail http://127.0.0.1:8001/health/ready
python3 scripts/smoke.py
```

Сервис доступен на сервере по `127.0.0.1:8001`; порт не публикуется на внешнем
интерфейсе. Swagger: `http://127.0.0.1:8001/docs`.
Образ не содержит `.env`, тесты, Git, IDE-файлы или исходные документы.

## Подключение к InterSim

Контейнерное имя сервиса и DNS: `ml-service`, внутренний порт: `8001`.
Для подключения к уже работающей сети Core Backend задайте её настоящее имя
в `INTERSIM_NETWORK` и используйте оба Compose-файла:

```bash
docker compose -f compose.yaml -f compose.intersim.yaml up -d --build --wait
```

[Интеграция с Core Backend](docs/integration.md) содержит определение имени сети,
проверку DNS, рабочие маршруты и пример настройки HTTP-клиента.
Core должен использовать DTO, ошибки и метаданные согласованного контракта v2.

## Документация

- [Развёртывание, настройки, обновление и откат](docs/deployment.md).
- [HTTP API и примеры запросов](docs/api.md).
- Канонические JSON Schema v2: `interviewer_input.schema.json`,
  `interviewer_output.schema.json`, `evaluator_input.schema.json`, `evaluator_output.schema.json`.
- [Связь с другими сервисами и расхождения общих документов](docs/integration.md).
- [Разработка, тесты и обновление lock-файлов](docs/development.md).
- [Исходные материалы и их происхождение](docs/reference/README.md).

## Проверки

```bash
.venv/bin/python -m pytest
.venv/bin/python -m app.contracts --check
.venv/bin/ruff check .
.venv/bin/ruff format --check .
```

CI повторяет проверки из lock-файла, собирает образ, запускает Compose и выполняет
smoke без платных LLM-вызовов. Реальные integration-тесты запускаются отдельно:
`.venv/bin/python -m pytest -m integration`.

## ASR

Groq transcription setup and contract: [docs/asr.md](docs/asr.md).
