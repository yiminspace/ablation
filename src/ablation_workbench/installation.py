"""Coordinate package upgrades with activity across local workspaces."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sys


def installation_lock_path() -> Path:
    identity = f"{Path(sys.prefix).resolve()}\n{Path(__file__).resolve().parent}"
    name = hashlib.sha256(identity.encode()).hexdigest()[:32]
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache").expanduser()
    return cache / "ablation" / "installation-locks" / f"{name}.lock"


class InstallationLease:
    def __init__(self, *, exclusive: bool):
        if os.name != "posix":
            raise RuntimeError("Installation activity locking currently requires macOS or Linux")
        import fcntl

        path = installation_lock_path()
        if not path.parent.is_dir():
            path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        if not path.exists():
            descriptor = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(descriptor)
        # A replay subprocess can join its parent's shared lease without cache writes.
        self._stream = path.open("rb")
        try:
            mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
            fcntl.flock(self._stream, mode | fcntl.LOCK_NB)
        except BlockingIOError:
            self._stream.close()
            message = ("This installation has active jobs or replays in another workspace; wait before upgrading"
                       if exclusive else "This installation is being upgraded; wait and restart before starting a job")
            raise RuntimeError(message) from None
        except BaseException:
            self._stream.close()
            raise

    def close(self):
        self._stream.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()


def installation_activity() -> InstallationLease:
    return InstallationLease(exclusive=False)


def installation_upgrade() -> InstallationLease:
    return InstallationLease(exclusive=True)
