"""Private atomic checkpoints and nonblocking process locks for a single Linux host."""

import fcntl
import hashlib
import json
import os
import tempfile
from pathlib import Path


class JobBusy(RuntimeError):
    """The same job is already running; backend should return 409 or its job status."""


class IdempotencyConflict(ValueError):
    """An existing request_id was reused with a different request/configuration."""


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


class Journal:
    def __init__(self, root, job_id, identity):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / (job_id + ".json")
        self.lock_path = self.root / (job_id + ".lock")
        self.identity = identity
        self.lock = None

    def __enter__(self):
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        self.lock = os.fdopen(fd, "w")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            raise JobBusy("generation already running") from None
        try:
            self.data = (
                json.loads(self.path.read_text())
                if self.path.exists()
                else {
                    "identity": self.identity,
                    "calls": {},
                    "slots": {},
                    "result": None,
                }
            )
            if self.data["identity"] != self.identity:
                raise IdempotencyConflict("request_id reused with different request or config")
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def save(self):
        fd, name = tempfile.mkstemp(prefix=".checkpoint-", dir=self.root)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(self.data, stream, ensure_ascii=False, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
            directory = os.open(self.root, os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def __exit__(self, *args):
        if self.lock:
            self.lock.close()
