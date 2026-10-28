"""Bounded update discovery and explicit package-manager upgrades."""

from __future__ import annotations

from importlib import metadata, resources
import json
import math
import os
from pathlib import Path
import re
import shutil
import shlex
import threading
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from . import __version__
from .installation import installation_upgrade
from .runner import ACTIVE, _alive, _write, workspace_lock

PACKAGE = "ablation-workbench"
CACHE_SECONDS = 24 * 60 * 60
_CHECK_LOCK = threading.Lock()


def _distribution():
    try:
        return metadata.distribution(PACKAGE)
    except metadata.PackageNotFoundError:
        return None


def _editable():
    distribution = _distribution()
    if distribution is None:
        return True
    try:
        data = json.loads(distribution.read_text("direct_url.json") or "{}")
        return bool(data.get("dir_info", {}).get("editable"))
    except (ValueError, OSError):
        return False


def _local_source():
    distribution = _distribution()
    if distribution is None:
        return False
    try:
        data = json.loads(distribution.read_text("direct_url.json") or "{}")
        return str(data.get("url", "")).startswith("file:")
    except (ValueError, OSError):
        return False


def _version_tuple(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d+\.\d+\.\d+", value):
        return None
    return tuple(int(part) for part in value.split("."))


def cached_update_info(workspace):
    base = {"schema_version": 1, "current": __version__, "latest": None, "available": False}
    if os.environ.get("ABLATION_UPDATE_CHECK") == "0":
        return {**base, "status": "disabled", "reason": "Disabled by ABLATION_UPDATE_CHECK=0"}
    if _editable():
        return {**base, "status": "disabled", "reason": "Source or editable installation"}
    try:
        cached = json.loads((Path(workspace) / ".update-cache.json").read_text())
    except (OSError, ValueError):
        cached = {}
    if not isinstance(cached, dict):
        cached = {}
    latest = cached.get("latest")
    if _version_tuple(latest) is not None:
        base.update(latest=latest, available=_version_tuple(latest) > _version_tuple(__version__))
    status = cached.get("status")
    base["status"] = status if status in ("checked", "unavailable") else "unknown"
    if status == "checked" and _version_tuple(latest) is None:
        base["status"] = "unknown"
    checked_at = cached.get("checked_at")
    if base["status"] != "unknown" and isinstance(checked_at, (int, float)) and not isinstance(checked_at, bool):
        try:
            timestamp = float(checked_at)
            if math.isfinite(timestamp):
                base["checked_at"] = timestamp
        except OverflowError:
            pass
    if isinstance(cached.get("reason"), str):
        base["reason"] = cached["reason"]
    return base


def update_info(workspace, *, force=False):
    with _CHECK_LOCK:
        cached = cached_update_info(workspace)
        if cached["status"] == "disabled":
            return cached
        checked_at = cached.get("checked_at")
        if not force and checked_at is not None and 0 <= time.time() - checked_at < CACHE_SECONDS:
            return cached
        result = {**cached, "checked_at": time.time()}
        result.pop("reason", None)
        try:
            with urlopen(f"https://pypi.org/pypi/{PACKAGE}/json", timeout=3) as response:
                raw = response.read(2_000_001)
                if len(raw) > 2_000_000:
                    raise ValueError("Registry response is too large")
                data = json.loads(raw)
            latest = data["info"]["version"]
            latest_version = _version_tuple(latest)
            if latest_version is None:
                raise ValueError("Registry did not provide a stable version")
            result.update(status="checked", latest=latest,
                          available=latest_version > _version_tuple(__version__))
        except HTTPError as error:
            result.update(status="unavailable", reason="Package is not published" if error.code == 404 else "Registry unavailable")
        except (URLError, OSError, ValueError, KeyError, TypeError):
            result.update(status="unavailable", reason="Update information could not be retrieved")
        with workspace_lock(workspace):
            _write(Path(workspace) / ".update-cache.json", result)
        return result


def gui_update_info(workspace):
    result = cached_update_info(workspace)
    plan = upgrade_plan()
    latest = result.get("latest")
    return {
        **result,
        "upgrade_command": shlex.join(plan["command"]) if plan.get("command") else "",
        "release_url": f"https://github.com/yiminspace/ablation/releases/tag/v{latest}" if _version_tuple(latest) else None,
    }


def _update_loop(workspace, stop, on_update=None):
    announced = None
    while not stop.is_set():
        try:
            result = update_info(workspace)
            if result.get("available") and result.get("latest") != announced:
                if on_update is not None:
                    on_update()
                announced = result["latest"]
        except (OSError, ValueError, RuntimeError):
            pass
        if stop.wait(3600):
            return


def start_update_checker(workspace, stop, on_update=None):
    thread = threading.Thread(target=_update_loop, args=(workspace, stop, on_update), name="ablation-update-checker", daemon=True)
    thread.start()
    return thread


def upgrade_plan():
    base = {"schema_version": 1, "package": PACKAGE, "current": __version__, "supported": False,
            "command": None, "restart_required": True}
    if _editable():
        return {**base, "manager": "source", "reason": "Update the source checkout; automatic upgrade is disabled"}
    prefix = Path(sys.prefix)
    if (prefix / "pipx_metadata.json").exists() or "pipx" in prefix.parts:
        executable = shutil.which("pipx")
        return {**base, "manager": "pipx", "supported": bool(executable),
                "command": ([executable, "install", "--force", PACKAGE] if _local_source() else [executable, "upgrade", PACKAGE]) if executable else None,
                "reason": "" if executable else "pipx executable not found"}
    if (prefix / "uv-receipt.toml").exists() or ("uv" in prefix.parts and "tools" in prefix.parts):
        executable = shutil.which("uv")
        return {**base, "manager": "uv", "supported": bool(executable),
                "command": ([executable, "tool", "install", "--upgrade", "--force", PACKAGE] if _local_source() else [executable, "tool", "upgrade", PACKAGE]) if executable else None,
                "reason": "" if executable else "uv executable not found"}
    return {**base, "manager": "pip", "supported": True,
            "command": [sys.executable, "-m", "pip", "install", "--upgrade", PACKAGE]}


def perform_upgrade(workspace):
    with installation_upgrade():
        return _perform_upgrade(workspace)


def _perform_upgrade(workspace):
    workspace = Path(workspace)
    marker = workspace / ".upgrade-running"
    with workspace_lock(workspace):
        for path in (workspace / "jobs").glob("*.json"):
            job = json.loads(path.read_text())
            if job.get("status") in ACTIVE:
                raise ValueError("Wait for active jobs to finish or stop them before upgrading")
        for path in (workspace / "experiments").glob("*/experiment.json"):
            from .storage import Store
            try:
                with Store(workspace).lock(path.parent.name, purpose="replay", blocking=False):
                    pass
            except RuntimeError:
                raise ValueError("Wait for active replays to finish before upgrading") from None
            experiment = json.loads(path.read_text())
            if any(run.get("status") in ACTIVE for run in experiment.get("runs", [])):
                raise ValueError("Wait for active replays to finish before upgrading")
        if marker.exists():
            try:
                running = json.loads(marker.read_text())
            except (OSError, ValueError):
                raise ValueError("An upgrade marker exists; inspect it before trying again") from None
            if _alive(running.get("pid")):
                raise ValueError("An upgrade is already running")
            marker.unlink()
        plan = upgrade_plan()
        if not plan["supported"]:
            raise ValueError(plan.get("reason", "Unsupported installation"))
        _write(marker, {"pid": os.getpid(), "started_at": time.time()})
    try:
        process = subprocess.run(plan["command"], capture_output=True, text=True, timeout=180, check=False)
        return {**plan, "status": "completed" if process.returncode == 0 else "failed",
                "exit_code": process.returncode, "restart_required": process.returncode == 0,
                "message": "Package manager completed; restart ablation to use the installed version" if process.returncode == 0
                else "Package manager failed. Run the displayed command in a terminal to inspect its diagnostics."}
    except subprocess.TimeoutExpired:
        return {**plan, "status": "failed", "restart_required": False,
                "message": "Upgrade exceeded its time limit; inspect the installation before retrying"}
    finally:
        with workspace_lock(workspace):
            marker.unlink(missing_ok=True)


def changelog():
    installed = resources.files("ablation_workbench").joinpath("CHANGELOG.md")
    if installed.is_file():
        return installed.read_text(encoding="utf-8")
    source = Path(__file__).resolve().parents[2] / "CHANGELOG.md"
    return source.read_text(encoding="utf-8") if source.exists() else "Release notes are unavailable."
