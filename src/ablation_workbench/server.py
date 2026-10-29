from __future__ import annotations

from datetime import datetime, timezone
import errno
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from urllib.parse import parse_qs, urlsplit
import uuid
import webbrowser

from .cli import read_config
from . import __version__
from .demo import create_demo
from .events import UpdateEvents
from .installation import installation_activity
from .runner import AgentRunner, workspace_lock

ACTIVE = {"queued", "running", "cancelling"}
MAX_BODY = 32 * 1024 * 1024
WEB = Path(__file__).parent / "web"
DEFAULT_PORT = 8766
BASE_PATH = "/ablation"


def route_path(path):
    if path == BASE_PATH or path.startswith(BASE_PATH + "/"):
        return path[len(BASE_PATH):] or "/"
    return path


def now():
    return datetime.now(timezone.utc).isoformat()


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)


class ReplayJobs:
    def __init__(self, workbench):
        self.workbench = workbench
        self.directory = Path(workbench.workspace) / "jobs"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.events = {}
        self.lock = threading.RLock()
        self.list()

    @staticmethod
    def alive(pid):
        if not isinstance(pid, int): return False
        try: os.kill(pid, 0); return True
        except OSError: return False

    def save(self, job):
        with self.lock: save_json(self.directory / (job["id"] + ".json"), job)

    def list(self):
        jobs = []
        for path in sorted(self.directory.glob("*.json")):
            try:
                job = json.loads(path.read_text())
                if job.get("kind") == "replay":
                    if job["status"] in ACTIVE and not self.alive(job.get("pid")):
                        job.update(status="interrupted", error="The process ended before this replay finished.", finished_at=now())
                        self.save(job)
                    jobs.append(job)
            except (OSError, ValueError): continue
        return jobs

    def start(self, experiment_id, options):
        self.workbench.get(experiment_id)
        allowed = {"candidates", "scenarios", "repeats", "profile"}
        if set(options) - allowed: raise ValueError("Unsupported run option")
        if not options.get("candidates") or not options.get("scenarios") or not options.get("profile"):
            raise ValueError("Select candidates, scenarios and an inference profile")
        repeats = options.get("repeats", 1)
        if not isinstance(repeats, int) or isinstance(repeats, bool) or not 1 <= repeats <= 10:
            raise ValueError("repeats must be 1–10")
        if len(options["candidates"]) * len(options["scenarios"]) * repeats > 30:
            raise ValueError("A batch may contain at most 30 requests")
        job = {"id": "replay_" + uuid.uuid4().hex[:16], "kind": "replay", "experiment_id": experiment_id,
               "status": "queued", "created_at": now(), "pid": os.getpid(), "options": options}
        lease = installation_activity()
        try:
            with workspace_lock(Path(self.workbench.workspace)):
                if (Path(self.workbench.workspace) / ".upgrade-running").exists(): raise RuntimeError("Upgrade is running")
                for path in self.directory.glob("*.json"):
                    other = json.loads(path.read_text())
                    if other.get("experiment_id") == experiment_id and other.get("status") in ACTIVE:
                        raise RuntimeError("This experiment already has an active job")
                self.save(job)
            event = threading.Event()
            with self.lock: self.events[job["id"]] = event
            threading.Thread(target=self._execute, args=(job, event, lease), daemon=True, name=job["id"]).start()
        except BaseException:
            lease.close()
            raise
        return job

    def _execute(self, job, event, lease):
        job = dict(job)
        try:
            job.update(status="running", started_at=now()); self.save(job)
            previous_ids = {run["id"] for run in self.workbench.get(job["experiment_id"])["runs"]}
            result = self.workbench.run(job["experiment_id"], cancel_event=event, **job["options"])
            runs = [run for run in result["runs"] if run["id"] not in previous_ids]
            errors = [run for run in runs if run["status"] in {"error", "interrupted"}]
            job.update(status="cancelled" if event.is_set() else "failed" if errors else "completed",
                       revision=result["revision"], run_ids=[run["id"] for run in runs],
                       summary={"requests": len(runs), "errors": len(errors),
                                "assertions_failed": sum(run.get("passed") is False for run in runs)})
            if errors:
                job["error"] = f"{len(errors)} replay request(s) failed; inspect their run records."
        except Exception as exc:
            job.update(status="cancelled" if event.is_set() else "failed", error=str(exc))
        finally:
            try:
                job["finished_at"] = now(); self.save(job)
            finally:
                with self.lock: self.events.pop(job["id"], None)
                lease.close()

    def cancel(self, job_id):
        with self.lock:
            event = self.events.get(job_id)
            if event is None: raise ValueError("No active replay with this ID in this service")
            event.set()
            job = json.loads((self.directory / (job_id + ".json")).read_text())
            if job["status"] in ACTIVE:
                job["status"] = "cancelling"; self.save(job)
            return job


def public_config(workspace):
    config = read_config(workspace)
    profiles = {}
    for name, raw in config.get("profiles", {}).items():
        profiles[name] = {key: raw[key] for key in ("base_url", "source_base_url", "source_path", "source_request_pointer", "source_response_pointer", "source_fields", "api_key_env", "token_env", "source_user_env", "source_password_env") if key in raw}
    agent = config.get("agent", {})
    return {"profiles": profiles, "agent": {"configured": bool(agent.get("backend")), "backend": agent.get("backend")}, "workspace": str(workspace)}


def make_server(workbench, port=0):
    agent = AgentRunner(workbench)
    jobs = ReplayJobs(workbench)
    owned_agent_jobs = set()
    updates = UpdateEvents()

    class Handler(BaseHTTPRequestHandler):
        server_version = "Ablation"

        def log_message(self, fmt, *args):
            pass

        def valid_local(self, mutation=False):
            host = self.headers.get("Host", "")
            parsed = urlsplit("//" + host)
            try:
                if parsed.hostname not in ("localhost", "127.0.0.1", "::1") or parsed.port != self.server.server_port:
                    return False
            except ValueError: return False
            origin = self.headers.get("Origin")
            if origin:
                url = urlsplit(origin)
                if url.scheme != "http" or url.netloc != host or url.path not in ("", "/"):
                    return False
            return not mutation or self.headers.get("X-Ablation-Client") == "local"

        def send_bytes(self, content, code=200, content_type="application/json; charset=utf-8", extra=None):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'")
            for key, value in (extra or {}).items(): self.send_header(key, value)
            self.end_headers()
            self.wfile.write(content)

        def send_json(self, value, code=200, extra=None):
            self.send_bytes(json.dumps(value, ensure_ascii=False, allow_nan=False).encode(), code, extra=extra)

        def error(self, exc, status=None):
            code = status or (404 if isinstance(exc, FileNotFoundError) else 409 if isinstance(exc, RuntimeError) else 400)
            self.send_json({"error": {"code": "not_found" if code == 404 else "request_failed", "message": str(exc)}}, code)

        def read_body(self):
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > MAX_BODY: raise ValueError("JSON upload exceeds 32 MiB")
            content_type = self.headers.get("Content-Type", "").split(";", 1)[0]
            if content_type != "application/json": raise ValueError("Content-Type must be application/json")
            value = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(value, dict): raise ValueError("Request body must be a JSON object")
            return value

        def send_events(self):
            revision = updates.revision()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.close_connection = True
            try:
                self.wfile.write(b"retry: 1000\n\n")
                self.wfile.flush()
                while (latest := updates.wait(revision)) is not None:
                    frame = b'data: {"type":"update_available"}\n\n' if latest != revision else b": heartbeat\n\n"
                    self.wfile.write(frame)
                    self.wfile.flush()
                    revision = latest
            except OSError:
                pass

        def do_GET(self):
            try:
                if not self.valid_local(): self.error(ValueError("Local origin required"), 403); return
                parsed = urlsplit(self.path)
                if parsed.path in ("/", "/index.html", BASE_PATH):
                    target = BASE_PATH + "/" + ("?" + parsed.query if parsed.query else "")
                    self.send_bytes(b"", 302, extra={"Location": target})
                    return
                path = route_path(parsed.path)
                if path == "/api/events":
                    self.send_events()
                    return
                if path in ("/", "/index.html", "/favicon.svg", "/app.js", "/styles.css", "/theme.js", "/voyage.css", "/voyage-toolbar.js", "/messages.js", "/messages.css", "/run-results.js", "/bifrost-LICENSE.txt", "/bifrost-NOTICE.txt"):
                    asset = WEB / ("index.html" if path == "/" else path[1:])
                    self.send_bytes(asset.read_bytes(), content_type=(mimetypes.guess_type(asset)[0] or "text/plain") + "; charset=utf-8")
                    return
                query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
                if path == "/api/experiments": result = {"experiments": workbench.list_experiments()}
                elif path == "/api/config": result = public_config(workbench.workspace)
                elif path == "/api/jobs": result = {"jobs": jobs.list() + agent.list_jobs()}
                elif path == "/api/version": result = {"version": __version__}
                elif path == "/api/update":
                    from .updates import gui_update_info
                    result = gui_update_info(workbench.workspace)
                elif path == "/api/changelog":
                    from .updates import changelog
                    result = {"text": changelog()}
                elif match := re.fullmatch(r"/api/experiments/([A-Za-z0-9_-]+)(?:/(.*))?", path):
                    eid, action = match.groups()
                    if not action: result = workbench.get(eid)
                    elif action == "diff":
                        result = workbench.compare(eid, **{k: v for k, v in query.items() if k in ("base", "candidate", "scenario", "artifact", "format")})
                    elif action == "export":
                        with tempfile.TemporaryDirectory() as directory:
                            target = Path(directory) / "experiment.json"
                            workbench.export_bundle(eid, target)
                            self.send_bytes(target.read_bytes(), extra={"Content-Disposition": f'attachment; filename="{eid}.json"'})
                            return
                    elif match := re.fullmatch(r"reviews/([A-Za-z0-9_-]+)/text", action):
                        result = {"text": workbench.review_text(eid, match[1])}
                    else: raise FileNotFoundError("Unknown experiment route")
                else: raise FileNotFoundError("Unknown route")
                self.send_json(result)
            except (ValueError, OSError, RuntimeError, KeyError, TypeError) as exc: self.error(exc)

        def do_POST(self):
            try:
                if not self.valid_local(mutation=True): self.error(ValueError("Local origin and client header required"), 403); return
                path = route_path(urlsplit(self.path).path)
                body = self.read_body()
                if path == "/api/import":
                    source = body.get("source", {})
                    if not isinstance(source, dict) or set(source) - {"kind", "data", "url", "ref", "profile", "response"}:
                        raise ValueError("GUI imports accept JSON content or source URL; local paths are CLI-only")
                    result = workbench.import_source(source, title=body.get("title", ""))
                elif path == "/api/import-bundle":
                    with tempfile.TemporaryDirectory() as directory:
                        target = Path(directory) / "bundle.json"; target.write_text(json.dumps(body))
                        result = workbench.import_bundle(target)
                elif path == "/api/demo": result = create_demo(workbench)
                elif path == "/api/upgrade":
                    from .updates import perform_upgrade
                    result = perform_upgrade(workbench.workspace)
                elif match := re.fullmatch(r"/api/jobs/([A-Za-z0-9_-]+)/cancel", path):
                    result = jobs.cancel(match[1]) if match[1].startswith("replay_") else agent.cancel(match[1])
                elif match := re.fullmatch(r"/api/experiments/([A-Za-z0-9_-]+)/(.*)", path):
                    eid, action = match.groups()
                    if action == "candidates": result = workbench.add_candidate(eid, **body)
                    elif action == "change-groups": result = workbench.set_change_groups(eid, **body)
                    elif action == "scenarios": result = workbench.add_scenario(eid, **body)
                    elif action == "reviews": result = workbench.submit_review(eid, body)
                    elif action == "run": result = jobs.start(eid, body)
                    elif match := re.fullmatch(r"reviews/([A-Za-z0-9_-]+)/(replies|dispatch)", action):
                        rid, verb = match.groups()
                        if verb == "dispatch":
                            with workspace_lock(Path(workbench.workspace)):
                                existing_ids = {job["id"] for job in agent.list_jobs()}
                                result = agent.start(eid, rid)
                                if result["id"] not in existing_ids:
                                    owned_agent_jobs.add(result["id"])
                        else:
                            result = workbench.reply(eid, rid, **body)
                    else: raise FileNotFoundError("Unknown experiment route")
                else: raise FileNotFoundError("Unknown route")
                self.send_json(result)
            except (ValueError, OSError, RuntimeError, KeyError, TypeError) as exc: self.error(exc)

    # Bind directly so another process cannot claim the port between a probe and bind.
    selected_port = DEFAULT_PORT if port is None else port
    while True:
        try:
            server = ThreadingHTTPServer(("127.0.0.1", selected_port), Handler)
            break
        except OSError as exc:
            if port is not None or exc.errno != errno.EADDRINUSE or selected_port >= 65535:
                raise
            selected_port += 1
    server.daemon_threads = True
    server.agent_runner = agent
    server.replay_jobs = jobs
    server.owned_agent_jobs = owned_agent_jobs
    server.update_events = updates
    return server


def close_jobs(server):
    server.update_events.close()
    for job in server.agent_runner.list_jobs():
        if job["id"] in server.owned_agent_jobs and job["status"] in ACTIVE:
            server.agent_runner.cancel(job["id"])
    for job_id in list(server.replay_jobs.events):
        server.replay_jobs.cancel(job_id)
    deadline = time.monotonic() + 5
    while any(job["id"] in server.owned_agent_jobs and job["status"] in ACTIVE
              for job in server.agent_runner.list_jobs()) and time.monotonic() < deadline:
        time.sleep(0.1)


def serve(workbench, experiment_id=None, port=None, open_browser=True):
    if experiment_id: workbench.get(experiment_id)
    server = make_server(workbench, port)
    url = f"http://127.0.0.1:{server.server_port}{BASE_PATH}/" + (f"?experiment={experiment_id}" if experiment_id else "")
    print(json.dumps({"url": url, "workspace": str(workbench.workspace)}), flush=True)
    if open_browser: webbrowser.open(url)
    from .updates import start_update_checker
    update_stop = threading.Event()
    update_thread = start_update_checker(workbench.workspace, update_stop, server.update_events.publish)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally:
        update_stop.set()
        update_thread.join(timeout=4)
        close_jobs(server)
        server.server_close()
