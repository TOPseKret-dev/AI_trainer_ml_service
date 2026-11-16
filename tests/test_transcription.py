import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from app.api.routes.transcription import get_asr_service
from app.main import app
from app.services import transcription as module
from app.services.transcription import AsrError, AsrService, AsrSettings

URL = "/api/v1/transcriptions"


def fields():
    return dict(
        attempt_id=str(uuid4()),
        specialization_id=str(uuid4()),
        question_text="SQL",
        deadline_at=(datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
    )


@pytest.fixture
def asr(monkeypatch):
    monkeypatch.setattr(module, "validate_audio", AsyncMock(return_value=1200))
    service = AsrService(
        AsrSettings(asr_groq_api_key="unused", groq_api_key="test"),
        httpx.MockTransport(lambda r: httpx.Response(200, json={"text": "SQL"})),
    )
    app.dependency_overrides[get_asr_service] = lambda: service
    yield service
    app.dependency_overrides.pop(get_asr_service)


def test_success(client, asr):
    data = fields()
    response = client.post(URL, data=data, files={"file": ("a.webm", b"audio", "audio/webm")})
    assert response.status_code == 200
    assert response.json()["attempt_id"] == data["attempt_id"]
    assert response.json()["recognized_text"] == "SQL"
    assert response.json()["audio_duration_ms"] == 1200
    assert asr.admitted == 0


@pytest.mark.parametrize(
    "audio,mime,status,code",
    [
        (b"", "audio/webm", 422, "AUDIO_EMPTY"),
        (b"audio", "audio/wav", 415, "AUDIO_FORMAT_UNSUPPORTED"),
        (b"x" * 25000001, "audio/webm", 413, "PAYLOAD_TOO_LARGE"),
    ],
)
def test_invalid_upload(client, asr, audio, mime, status, code):
    response = client.post(URL, data=fields(), files={"file": ("a", audio, mime)})
    assert response.status_code == status
    assert response.json()["error"]["code"] == code


def test_expired(client, asr):
    data = fields()
    data["deadline_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    response = client.post(URL, data=data, files={"file": ("a", b"a", "audio/webm")})
    assert response.json()["error"]["code"] == "ASR_TIMEOUT"
    assert asr.admitted == 0


@pytest.mark.parametrize(
    "status,code",
    [
        (401, "ASR_PROVIDER_AUTH_ERROR"),
        (429, "ASR_UNAVAILABLE"),
        (500, "ASR_UNAVAILABLE"),
        (400, "ASR_PROVIDER_CONFIG_ERROR"),
    ],
)
def test_provider_errors(asr, status, code):
    asr.transport = httpx.MockTransport(lambda r: httpx.Response(status, text="secret upstream"))
    with pytest.raises(AsrError) as error:
        asyncio.run(asr.recognize(b"a", "SQL"))
    assert error.value.code == code


def test_no_speech(asr):
    asr.transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"text": " "}))
    with pytest.raises(AsrError, match="ASR_NO_SPEECH"):
        asyncio.run(asr.recognize(b"a", "SQL"))


def test_full_queue(client, asr):
    asr.admitted = 14
    response = client.post(URL, data=fields(), files={"file": ("a", b"a", "audio/webm")})
    assert response.json()["error"]["code"] == "ASR_QUEUE_FULL"
    assert asr.admitted == 14


def test_real_decoder_rejects_junk():
    with pytest.raises(AsrError):
        asyncio.run(module.validate_audio(b"not webm"))


def test_missing_fields_use_asr_envelope(client, asr):
    response = client.post(URL, files={"file": ("a", b"a", "audio/webm")})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "ASR_INVALID_INPUT"


def test_cancellation_releases_slot(asr, monkeypatch):
    async def scenario():
        entered = asyncio.Event()

        async def slow(data):
            entered.set()
            await asyncio.sleep(100)

        monkeypatch.setattr(module, "validate_audio", slow)
        task = asyncio.create_task(
            asr.transcribe(b"a", uuid4(), "SQL", datetime.now(UTC) + timedelta(seconds=30))
        )
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert asr.admitted == 0
        assert asr.slots._value == 4

    asyncio.run(scenario())


def test_real_webm_silence(tmp_path):
    import subprocess

    path = tmp_path / "silence.webm"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=16000:cl=mono",
            "-t",
            "0.1",
            "-c:a",
            "libopus",
            str(path),
        ],
        check=True,
    )
    with pytest.raises(AsrError, match="ASR_NO_SPEECH"):
        asyncio.run(module.validate_audio(path.read_bytes()))


def test_real_webm_duration(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "has_speech", lambda pcm: True)
    import subprocess

    path = tmp_path / "tone.webm"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=16000",
            "-t",
            "0.2",
            "-c:a",
            "libopus",
            str(path),
        ],
        check=True,
    )
    duration = asyncio.run(module.validate_audio(path.read_bytes()))
    assert 190 <= duration <= 210


def test_settings_load_dotenv(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ASR_GROQ_API_KEY", raising=False)
    (tmp_path / ".env").write_text("ASR_GROQ_API_KEY=local-test\n")
    assert AsrSettings().groq_api_key.get_secret_value() == "local-test"


@pytest.mark.parametrize("status", [408, 504])
def test_provider_http_timeout(asr, status):
    asr.transport = httpx.MockTransport(lambda r: httpx.Response(status))
    with pytest.raises(AsrError) as error:
        asyncio.run(asr.recognize(b"a", "SQL"))
    assert (error.value.code, error.value.status, error.value.retryable) == (
        "ASR_TIMEOUT",
        504,
        True,
    )


def test_real_decoder_rejects_truncation(tmp_path):
    import subprocess

    path = tmp_path / "tone.webm"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=16000",
            "-t",
            "2",
            "-c:a",
            "libopus",
            str(path),
        ],
        check=True,
    )
    data = path.read_bytes()
    with pytest.raises(AsrError, match="AUDIO_INVALID"):
        asyncio.run(module.validate_audio(data[: len(data) // 2]))


def test_temp_storage_failure_is_retryable(monkeypatch):
    import errno
    from pathlib import Path

    def full_disk(*args):
        raise OSError(errno.ENOSPC, "No space left")

    monkeypatch.setattr(Path, "write_bytes", full_disk)
    with pytest.raises(AsrError) as error:
        asyncio.run(module.validate_audio(b"a"))
    assert error.value.code == "ASR_UNAVAILABLE"
    assert error.value.retryable


def test_provider_deadline_releases_capacity(asr, monkeypatch):
    async def slow(request):
        await asyncio.sleep(1)
        return httpx.Response(200, json={"text": "SQL"})

    asr.transport = httpx.MockTransport(slow)
    with pytest.raises(AsrError, match="ASR_TIMEOUT"):
        asyncio.run(
            asr.transcribe(b"a", uuid4(), "SQL", datetime.now(UTC) + timedelta(seconds=0.02))
        )
    assert asr.admitted == 0
    assert asr.slots._value == 4


def test_upload_budget_and_concurrency_are_released():
    from app.asr_admission import AsrAdmission

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def endpoint(scope, receive, send):
            while (await receive()).get("more_body"):
                pass
            entered.set()
            await release.wait()
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        middleware = AsrAdmission(endpoint, max_requests=2, max_bytes=10)
        scope = {"type": "http", "path": URL}

        async def request(body):
            sent = []

            async def receive():
                return {"type": "http.request", "body": body, "more_body": False}

            async def send(message):
                sent.append(message)

            await middleware(scope, receive, send)
            return sent

        first = asyncio.create_task(request(b"12345678"))
        await entered.wait()
        rejected = await request(b"123")
        assert rejected[0]["status"] == 503
        assert middleware.bytes == 8
        assert middleware.active == 1
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert middleware.bytes == middleware.active == 0
        release.set()
        assert (await request(b"123"))[0]["status"] == 200
        assert middleware.bytes == middleware.active == 0

    asyncio.run(scenario())


def test_oversized_body_rejected_before_multipart(client, asr):
    response = client.post(URL, content=b"x" * (25000000 + 65537))
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"
    assert asr.admitted == 0


def test_vad_rejects_stationary_noise():
    import random
    import struct

    rng = random.Random(10)
    pcm = b"".join(struct.pack("<h", rng.randint(-160, 160)) for _ in range(16000 * 3))
    assert not module.has_speech(pcm)
    assert not module.has_speech(bytes(16000 * 2))


def test_retry_after_is_forwarded(client, asr):
    asr.transport = httpx.MockTransport(
        lambda r: httpx.Response(429, headers={"Retry-After": "17"})
    )
    r = client.post(URL, data=fields(), files={"file": ("a.webm", b"a", "audio/webm")})
    assert r.status_code == 503
    assert r.headers["Retry-After"] == "17"
    assert r.json()["error"]["details"]["retryable"]


@pytest.mark.parametrize("value,expected", [("3", 3), ("bad", None), (None, None)])
def test_retry_after_validation(value, expected):
    assert module.retry_after_seconds(value) == expected
