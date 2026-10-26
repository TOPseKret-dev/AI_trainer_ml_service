from unittest.mock import AsyncMock

import pytest

from app.schemas.transcription import TranscriptionResponse
from app.services import transcription

URL = "/api/v1/transcriptions"


def test_audio_upload(client):
    response = client.post(URL, files={"file": ("sample.webm", b"audio bytes", "audio/webm")})
    assert response.status_code == 200
    assert response.json() == {
        "text": "stub transcription",
        "language": "ru",
        "duration_seconds": None,
        "confidence": None,
    }


def test_custom_language_and_arbitrary_audio_codec(client):
    response = client.post(
        URL,
        files={"file": ("sample.unknown", b"audio bytes", "audio/x-custom-codec")},
        data={"language": "en"},
    )
    assert response.status_code == 200
    assert response.json()["language"] == "en"


def test_empty_file(client):
    response = client.post(URL, files={"file": ("empty.wav", b"", "audio/wav")})
    assert response.status_code == 400


@pytest.mark.parametrize("content_type", ["text/plain", "application/octet-stream", "video/webm"])
def test_non_audio_content_type(client, content_type):
    response = client.post(URL, files={"file": ("sample.wav", b"data", content_type)})
    assert response.status_code == 400


def test_missing_file(client):
    assert client.post(URL, data={"language": "ru"}).status_code == 422


def test_absent_file_content_type_is_accepted(client):
    body = (
        b'--boundary\r\nContent-Disposition: form-data; name="file"; filename="audio"\r\n'
        b"\r\naudio bytes\r\n--boundary--\r\n"
    )
    response = client.post(
        URL, content=body, headers={"Content-Type": "multipart/form-data; boundary=boundary"}
    )
    assert response.status_code == 200


def test_spooled_file_has_no_artificial_byte_limit(client):
    response = client.post(
        URL, files={"file": ("large.wav", b"x" * (2 * 1024 * 1024), "audio/wav")}
    )
    assert response.status_code == 200


def test_endpoint_awaits_transcription_service(client, monkeypatch):
    service = AsyncMock(
        return_value=TranscriptionResponse(
            text="service called", language="ru", duration_seconds=None, confidence=None
        )
    )
    monkeypatch.setattr(transcription, "transcribe_audio", service)
    response = client.post(URL, files={"file": ("sample.wav", b"audio", "audio/wav")})
    assert response.status_code == 200
    assert response.json()["text"] == "service called"
    service.assert_awaited_once()
