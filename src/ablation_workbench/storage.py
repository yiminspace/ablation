from __future__ import annotations

import contextlib
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Iterator

from .models import MAX_BYTES, SCHEMA_VERSION, canonical, validate_id

_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


def load_json(path: str | Path) -> Any:
    path = Path(path)
    if path.stat().st_size > MAX_BYTES:
        raise ValueError("JSON file exceeds 32 MiB")
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_json(path: Path, value: Any) -> None:
    encoded = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode()
    if len(encoded) > MAX_BYTES:
        raise ValueError("JSON document exceeds 32 MiB")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".ablation-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Store:
    def __init__(self, workspace: str | Path):
        self.workspace = Path(workspace).expanduser().resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.root = self.workspace / "experiments"
        self.root.mkdir(exist_ok=True)
        if self.root.is_symlink():
            raise ValueError("Experiment storage cannot be a symbolic link")

    def experiment_path(self, experiment_id: str) -> Path:
        path = self.root / validate_id(experiment_id) / "experiment.json"
        if not path.resolve().is_relative_to(self.root.resolve()) or path.parent.is_symlink() or path.is_symlink():
            raise ValueError("Experiment path escapes workspace")
        return path

    @contextlib.contextmanager
    def lock(self, experiment_id: str, *, purpose: str = "write", blocking: bool = True) -> Iterator[None]:
        path = self.experiment_path(experiment_id)
        path.parent.mkdir(exist_ok=True)
        lock_path = path.parent / f".{purpose}.lock"
        if lock_path.is_symlink():
            raise ValueError("Lock path cannot be a symbolic link")
        with _locks_guard:
            thread_lock = _locks.setdefault(str(lock_path), threading.RLock())
        acquired = thread_lock.acquire(blocking=blocking)
        if not acquired:
            raise RuntimeError("An operation is already active for this experiment")
        try:
            with lock_path.open("a+b") as stream:
                if os.name == "nt":
                    import msvcrt
                    if stream.tell() == 0:
                        stream.write(b"0")
                        stream.flush()
                    stream.seek(0)
                    mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
                    try:
                        msvcrt.locking(stream.fileno(), mode, 1)
                    except OSError as error:
                        raise RuntimeError("An operation is already active for this experiment") from error
                    try:
                        yield
                    finally:
                        stream.seek(0)
                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    try:
                        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
                    except BlockingIOError as error:
                        raise RuntimeError("An operation is already active for this experiment") from error
                    try:
                        yield
                    finally:
                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            thread_lock.release()

    def load(self, experiment_id: str) -> dict:
        data = load_json(self.experiment_path(experiment_id))
        if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("Unsupported experiment schema version")
        return data

    def save(self, experiment: dict, *, bump: bool = True) -> None:
        if bump:
            experiment["revision"] += 1
        atomic_json(self.experiment_path(experiment["id"]), experiment)

    def config(self) -> dict:
        path = self.workspace / "config.json"
        if not path.exists():
            return {"schema_version": SCHEMA_VERSION, "profiles": {}}
        data = load_json(path)
        if not isinstance(data, dict) or data.get("schema_version", 1) != SCHEMA_VERSION:
            raise ValueError("Unsupported configuration schema version")
        profiles = data.get("profiles", {})
        if not isinstance(profiles, dict):
            raise ValueError("Configuration profiles must be an object")
        for profile in profiles.values():
            if not isinstance(profile, dict):
                raise ValueError("Each profile must be an object")
            if any(key in profile for key in ("api_key", "password", "token", "authorization", "headers", "source_user", "source_password", "access_token", "refresh_token")):
                raise ValueError("Profiles must reference credential environment variables, not secret values")
            from .sources import validate_source_profile
            validate_source_profile(profile)
        canonical(data)
        return data
