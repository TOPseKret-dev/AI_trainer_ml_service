# ASR API (MI-004)

Implemented route: `POST /api/v1/transcriptions`, internal Core to ML only.
Provider: Groq `whisper-large-v3-turbo`, Russian, temperature 0. One provider
request per operation; no automatic retry or model fallback.

## Configuration

Set `ASR_GROQ_API_KEY` in `.env` or the deployment environment. Local settings
read `.env`; Compose forwards the key to ML. Never commit the file.
An absent key disables ASR with `ASR_NOT_CONFIGURED`; other endpoints still run.
Local runs require `ffmpeg` and `ffprobe` on PATH; the Docker image installs them.
Keep `ML_WORKERS=1` and one service replica: the semaphore is process-local.
The endpoint must remain behind Core in the private network, as in compose.yaml.
It does not authenticate end users; Core owns authorization and rate limiting.

## Request and response

Required multipart fields:

- `file`: audio/webm, actual WebM/Opus, one audio stream, at most 25,000,000 bytes.
- `attempt_id`: UUID assigned by Core.
- `question_text`: saved current interviewer question, 1-10,000 characters.
- `specialization_id`: UUID; currently validated but no glossary is selected.
- `deadline_at`: timezone-aware timestamp; Core should send UTC.

Success returns `attempt_id`, `recognized_text`, `audio_duration_ms`, `language`,
`provider`, `model`, `ml_received_at`, `queue_ms`, `processing_ms`,
`glossary_version` (currently null). Duration comes from decoded 16 kHz mono PCM,
not from client metadata or a provider usage estimate. No fabricated confidence
or token counts are returned. The provisional stub DTO is replaced; Core must
regenerate its ASR contract before integration. Interviewer/evaluator are unchanged.

Errors use `{ "error": { "code": "...", "message": "...", "details":
{ "attempt_id": "...", "retryable": false } } }`.
Request validation errors omit attempt_id if it cannot be validated. Raw provider
bodies, file names, audio, question text and recognized text are not logged.

| Status | Codes |
|---|---|
| 413 | PAYLOAD_TOO_LARGE |
| 415 | AUDIO_FORMAT_UNSUPPORTED |
| 422 | ASR_INVALID_INPUT, AUDIO_EMPTY, AUDIO_INVALID, AUDIO_DURATION_EXCEEDED, ASR_NO_SPEECH |
| 502 | ASR_INVALID_RESPONSE |
| 503 | ASR_NOT_CONFIGURED, ASR_PROVIDER_AUTH_ERROR, ASR_PROVIDER_CONFIG_ERROR, ASR_UNAVAILABLE, ASR_QUEUE_FULL |
| 504 | ASR_TIMEOUT, ASR_QUEUE_TIMEOUT |

HTTP 408/504 and transport timeouts are retryable ASR_TIMEOUT.
Other transient transport failures, 429 and upstream 5xx are retryable ASR_UNAVAILABLE.
Auth/config errors are not retryable. Retry is always a new request initiated by
Core under its own policy; ML does not persist or deduplicate attempts.

## Execution and limits

At most fourteen requests are admitted before multipart parsing; four operations
run concurrently and queue wait is at most ten seconds. A shared 32 MiB upload
budget is held until requests finish; large recordings can reach this budget
before the count limit, returning retryable ASR_QUEUE_FULL. Each multipart body
is limited to 25,000,000 bytes plus 64 KiB of fields/framing, with a separate
30-second upload timeout. File contents retain the 25,000,000-byte limit.
Upload spools are closed as soon as the audio bytes have been read. This bounds
ASR temporary storage below the default 128 MiB tmpfs; local storage errors
return retryable ASR_UNAVAILABLE. The total
budget is min(Core deadline, 30 seconds from endpoint receipt), including queue,
decoding and provider call. Cancellation terminates decoder subprocesses and
releases the slot. Audio is decoded only up to 181 seconds to detect the
180-second limit with bounded PCM output. Temporary copies use random private
directories and are removed on normal completion, errors and cancellation.

## Differences and remaining integration work

This implementation follows the proposed internal MI-003 contract. The following
limitations are explicit and must be considered before pilot acceptance:

- Queue limits are per process, not shared across workers. Use exactly one worker
  and replica; distributed admission requires a separate implementation.
- Speech checking uses local WebRTC VAD (mode 3, 16 kHz mono PCM16, 30 ms
  frames) before calling Groq. Normally four consecutive voiced frames are
  required; recordings shorter than 120 ms use half their frames, rounded up.
  This rejects short noise-onset triggers without a text blacklist. Whisper
  segments all reporting no_speech_prob >= 0.6 and avg_logprob < -1 are also
  rejected. VAD is probabilistic: varied microphones, quiet speech and background
  noise still require a representative acceptance dataset. No model download
  or separate VAD service is needed.
  Reference: https://github.com/daanzu/py-webrtcvad-wheels
- The prompt contains only the first 200 characters of the trusted question.
  There is no glossary yet; specialization_id is reserved for it and
  glossary_version is null. No references, rubrics or candidate answers are used.
- MI-009 usage accounting is not implemented or represented as zero. The response
  reports measured audio duration; provider billing/token accounting needs its
  own agreed contract.
- The 180-second ceiling is checked in ML; a lower per-session limit (30-180 s)
  must be enforced by Core because that configuration is absent from the DTO.
- Admission and byte budgets are applied before multipart parsing. Core should
  still bound its own upload/delivery queue and enforce per-user rate limits.
- Abrupt process kill cannot run cleanup; /tmp is ephemeral in Compose. A hard
  ten-minute orphan TTL is not implemented. Core owns long-lived pilot originals.
- Valid Retry-After from Groq 429/5xx is forwarded as integer seconds (maximum
  24 hours). Core retains its bounded retry policy; ML never retries automatically.
  Capacity and 10-second latency targets need testing on the pilot VPS.
- Unit tests do not invoke paid models. Real provider checks are separate from
  the suite and do not establish deployment or end-to-end Core integration.

Provider reference: https://console.groq.com/docs/speech-to-text

## Verification

Run `python -m pytest` and `ruff check .` in the project environment. ASR tests
mock the provider transport and exercise validation, queue saturation, deadlines,
provider failures and cancellation. Decoder tests generate real WebM/Opus with
ffmpeg and check silence, duration and invalid input. Existing non-ASR tests run
with the same suite. Real speech recognition requires a configured key and an
approved test recording; keep recordings and secrets out of git.

## Deployment checks and verification record (2026-11-16)

Before deployment, retain the server's existing OPENROUTER_API_KEY and add
ASR_GROQ_API_KEY to its private `.env` (mode 0600). Both files and keys must stay
outside Git and the image. Do not replace the server configuration with the
local test configuration. Compose requires OPENROUTER_API_KEY for the existing
LLM routes and readiness; ASR itself uses only the Groq key.

Use one worker and one replica, the default 512 MiB memory and 128 MiB `/tmp`
limits. The 32 MiB shared admission budget deliberately rejects some batches of
large recordings before the fourteen-request ceiling. Core must treat
ASR_QUEUE_FULL as retryable and preserve its pilot audio original. Temporary
ASR files are not the pilot archive. Increasing workers or replicas changes the
aggregate limits and requires a new capacity check.

Build on the Linux server, or publish an image built for its architecture.
Validate Compose without printing interpolated secrets (`docker compose config
--quiet`). Start with the existing deployment Compose files and verify readiness,
then send a controlled WebM/Opus recording using the documented multipart fields.
A healthy LLM readiness endpoint alone does not prove Groq connectivity. The
ASR DTO replaces the old stub response; Core needs the updated required fields
and `recognized_text` response before a coordinated rollout.

Verification performed:

- 185 unit/contract tests passed both locally and inside the final Linux amd64
  image; two paid LLM integration tests excluded.
  Ruff, formatting, dependency consistency and diff whitespace checks passed.
- Linux amd64 production image built with hash-locked dependencies. Container
  ran as UID 1000, root filesystem read-only, 512 MiB memory and 128 MiB `/tmp`;
  healthcheck passed and `/app/.env` was absent.
- Live Groq recognized a synthesized Russian database-index explanation
  (6.963 seconds) in approximately 0.6-1.1 seconds, preserving its words.
  A 180-second repetitive synthetic recording returned success, but its output
  was incomplete and contained artifacts; this is not an accuracy benchmark.
- After adding VAD, digital silence and low stationary noise returned
  ASR_NO_SPEECH; 181-second input returned AUDIO_DURATION_EXCEEDED. The same
  validation cases passed over HTTP in the production container.
- Fourteen concurrent 5 MB uploads inside the constrained container, with only
  decoding/provider replaced for the capacity test: six succeeded, eight were
  rejected with 503, the next request succeeded, and no ASR temp files remained.
- Final live provider verification is blocked: Groq subsequently returned 404
  for both transcriptions and `/openai/v1/models`, also without credentials via
  curl. This does not establish a bad key: earlier authenticated inference
  succeeded. VPN was reported enabled; the cause of the access failure has not
  been established. No automatic retries were added to hide this condition.
- The server SSH check timed out before authentication. No server deployment or
  end-to-end browser/Core test was performed. Recheck provider connectivity from
  the deployment host before declaring the ASR rollout accepted.

The local Groq probe sent only newly synthesized neutral speech and artificial
noise. Existing project recordings were not uploaded by these verification runs.
