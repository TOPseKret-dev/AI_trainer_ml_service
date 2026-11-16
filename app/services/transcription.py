import asyncio
import json
import logging
import tempfile
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from math import ceil
from pathlib import Path
from time import monotonic

import httpx
import webrtcvad
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class AsrSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ASR_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )
    groq_api_key: SecretStr = SecretStr("")


class AsrError(Exception):
    def __init__(
        self, code: str, status: int, retryable: bool = False, retry_after: int | None = None
    ):
        super().__init__(code)
        self.code, self.status, self.retryable = code, status, retryable
        self.retry_after = retry_after


def retry_after_seconds(value: str | None) -> int | None:
    if not value:
        return None
    try:
        seconds = (
            int(value)
            if value.isdecimal()
            else ceil((parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds())
        )
        return max(0, min(seconds, 86400))
    except (ValueError, TypeError, OverflowError):
        return None


async def run_process(*args: str) -> bytes:
    process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        stdout, stderr = await process.communicate()
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    if process.returncode or b"File ended prematurely" in stderr:
        raise AsrError("AUDIO_INVALID", 422)
    return stdout


def has_speech(pcm: bytes) -> bool:
    # 16 kHz mono PCM16, 30 ms frames. Require sustained speech rather than
    # the first few frames that a stationary noise onset can trigger.
    vad = webrtcvad.Vad(3)
    frame_size = 960
    frames = (len(pcm) + frame_size - 1) // frame_size
    required = min(4, max(1, (frames + 1) // 2))
    consecutive = 0
    for offset in range(0, len(pcm), frame_size):
        frame = pcm[offset : offset + frame_size].ljust(frame_size, b"\x00")
        consecutive = consecutive + 1 if vad.is_speech(frame, 16000) else 0
        if consecutive >= required:
            return True
    return False


async def validate_audio(data: bytes) -> int:
    try:
        return await decode_audio(data)
    except OSError as exc:
        raise AsrError("ASR_UNAVAILABLE", 503, True) from exc


async def decode_audio(data: bytes) -> int:
    with tempfile.TemporaryDirectory(prefix="intersim-asr-") as directory:
        source = Path(directory) / "input.webm"
        source.write_bytes(data)
        try:
            probe = json.loads(
                await run_process(
                    "ffprobe",
                    "-v",
                    "error",
                    "-protocol_whitelist",
                    "file",
                    "-show_streams",
                    "-show_format",
                    "-of",
                    "json",
                    str(source),
                )
            )
            streams = probe.get("streams", [])
            if (
                len(streams) != 1
                or streams[0].get("codec_name") != "opus"
                or "webm" not in probe.get("format", {}).get("format_name", "")
            ):
                raise AsrError("AUDIO_FORMAT_UNSUPPORTED", 415)
            pcm = await run_process(
                "ffmpeg",
                "-v",
                "error",
                "-nostdin",
                "-xerror",
                "-protocol_whitelist",
                "file",
                "-i",
                str(source),
                "-t",
                "181",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-f",
                "s16le",
                "pipe:1",
            )
        except FileNotFoundError as exc:
            raise AsrError("ASR_NOT_CONFIGURED", 503) from exc
        except (ValueError, KeyError, TypeError) as exc:
            raise AsrError("AUDIO_INVALID", 422) from exc
        duration = len(pcm) / 32
        if duration > 180000:
            raise AsrError("AUDIO_DURATION_EXCEEDED", 422)
        if not pcm:
            raise AsrError("AUDIO_INVALID", 422)
        if not has_speech(pcm):
            raise AsrError("ASR_NO_SPEECH", 422)
        return round(duration)


class AsrService:
    def __init__(self, settings=None, transport=None):
        self.settings = settings or AsrSettings()
        self.transport = transport
        self.slots = asyncio.Semaphore(4)
        self.admitted = 0

    async def transcribe(self, data, attempt_id, question_text, deadline_at):
        received = datetime.now(UTC)
        remaining = min(30, (deadline_at - received).total_seconds())
        if remaining <= 0:
            raise AsrError("ASR_TIMEOUT", 504, True)
        if self.admitted >= 14:
            raise AsrError("ASR_QUEUE_FULL", 503, True)
        self.admitted += 1
        started = monotonic()
        acquired = False
        try:
            async with asyncio.timeout(remaining):
                try:
                    await asyncio.wait_for(self.slots.acquire(), min(10, remaining))
                    acquired = True
                except TimeoutError as exc:
                    raise AsrError("ASR_QUEUE_TIMEOUT", 504, True) from exc
                queue_ms = round((monotonic() - started) * 1000)
                processing = monotonic()
                if not self.settings.groq_api_key.get_secret_value():
                    raise AsrError("ASR_NOT_CONFIGURED", 503)
                duration = await validate_audio(data)
                text = await self.recognize(data, question_text)
                return dict(
                    attempt_id=str(attempt_id),
                    recognized_text=text,
                    audio_duration_ms=duration,
                    language="ru",
                    provider="groq",
                    model="whisper-large-v3-turbo",
                    ml_received_at=received,
                    queue_ms=queue_ms,
                    processing_ms=round((monotonic() - processing) * 1000),
                    glossary_version=None,
                )
        except TimeoutError as exc:
            raise AsrError("ASR_TIMEOUT", 504, True) from exc
        finally:
            if acquired:
                self.slots.release()
            self.admitted -= 1

    async def recognize(self, data, question_text):
        try:
            async with httpx.AsyncClient(transport=self.transport, timeout=30) as client:
                response = await client.post(
                    "https://api.groq.com/openai/v1/audio/transcriptions",
                    headers={
                        "Authorization": "Bearer " + self.settings.groq_api_key.get_secret_value()
                    },
                    files={"file": ("audio.webm", data, "audio/webm")},
                    data={
                        "model": "whisper-large-v3-turbo",
                        "language": "ru",
                        "temperature": "0",
                        "response_format": "verbose_json",
                        "prompt": question_text[:200],
                    },
                )
        except httpx.TimeoutException as exc:
            raise AsrError("ASR_TIMEOUT", 504, True) from exc
        except httpx.RequestError as exc:
            raise AsrError("ASR_UNAVAILABLE", 503, True) from exc
        logger.info("function=asr_provider status=%d", response.status_code)
        if response.status_code in (408, 504):
            raise AsrError("ASR_TIMEOUT", 504, True)
        if response.status_code in (401, 403):
            raise AsrError("ASR_PROVIDER_AUTH_ERROR", 503)
        if response.status_code == 429 or response.status_code >= 500:
            raise AsrError(
                "ASR_UNAVAILABLE",
                503,
                True,
                retry_after_seconds(response.headers.get("Retry-After")),
            )
        if response.status_code != 200:
            raise AsrError("ASR_PROVIDER_CONFIG_ERROR", 503)
        try:
            payload = response.json()
            text = payload["text"]
            if not isinstance(text, str) or len(text) > 100000:
                raise ValueError()
            if not text.strip():
                raise AsrError("ASR_NO_SPEECH", 422)
            segments = payload.get("segments", [])
            if segments and all(
                isinstance(segment, dict)
                and isinstance(segment.get("no_speech_prob"), (int, float))
                and segment["no_speech_prob"] >= 0.6
                and isinstance(segment.get("avg_logprob"), (int, float))
                and segment["avg_logprob"] < -1
                for segment in segments
            ):
                raise AsrError("ASR_NO_SPEECH", 422)
            return text.strip()
        except (ValueError, KeyError, TypeError) as exc:
            raise AsrError("ASR_INVALID_RESPONSE", 502, True) from exc
