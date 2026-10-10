from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlparse

from . import __version__
from .engine import Workbench


def default_workspace():
    return Path(os.environ.get("ABLATION_WORKSPACE", Path.home() / ".local/share/ablation"))


def read_json(path):
    return json.loads(sys.stdin.read() if path == "-" else Path(path).expanduser().read_text(encoding="utf-8"))


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))


def config_path(workspace):
    return Path(workspace) / "config.json"


def read_config(workspace):
    path = config_path(workspace)
    return json.loads(path.read_text()) if path.exists() else {"schema_version": 1, "profiles": {}}


def save_config(workspace, data):
    from .runner import workspace_lock
    path = config_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    with workspace_lock(Path(workspace)):
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        tmp.chmod(0o600)
        tmp.replace(path)


def configure_profile(args, workspace):
    profile = {}
    for key in ("base_url", "source_base_url", "source_path", "source_request_pointer", "source_response_pointer", "api_key_env", "token_env", "source_user_env", "source_password_env"):
        value = getattr(args, key, None)
        if value is None:
            continue
        if key.endswith("_env") and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
            raise ValueError(f"{key} must be an environment variable name, not a secret value")
        if key.endswith("url"):
            url = urlparse(value)
            if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password or url.query or url.fragment:
                raise ValueError(f"{key} must be an HTTP(S) URL without credentials, query or fragment")
        profile[key] = value
    if args.source_fields_json is not None:
        profile["source_fields"] = json.loads(args.source_fields_json)
    config = read_config(workspace)
    config.setdefault("profiles", {}).setdefault(args.name, {}).update(profile)
    from .sources import validate_source_profile
    validate_source_profile(config["profiles"][args.name])
    save_config(workspace, config)
    return {"profile": args.name, "config": str(config_path(workspace)), "settings": config["profiles"][args.name]}


def parser():
    p = argparse.ArgumentParser(prog="ablation", description="Local prompt experiments, replay, diffs and review.")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--workspace", type=Path, default=default_workspace())
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="Create a local workspace with credential references only")
    imp = sub.add_parser("import", help="Import request, log or capture JSON from a file or configured source")
    imp.add_argument("source", help="JSON path, '-' for stdin, same-origin JSON URL, or configured source reference")
    imp.add_argument("--format", choices=["request", "log", "capture"], default="request")
    imp.add_argument("--ref", action="store_true", help="Treat source as an identifier expanded through the profile's source_path")
    imp.add_argument("--profile", default="")
    imp.add_argument("--title", default="")
    imp.add_argument("--response", help="Optional original response JSON")
    sub.add_parser("list")
    show = sub.add_parser("show"); show.add_argument("experiment")
    demo = sub.add_parser("demo", help="Create a synthetic walkthrough, with no model requests")
    demo.add_argument("--open", action="store_true")
    gui = sub.add_parser("gui", help="Serve the local review workspace")
    gui.add_argument("experiment", nargs="?")
    gui.add_argument("--port", type=int, default=0)
    gui.add_argument("--no-open", action="store_true")
    candidate = sub.add_parser("candidate", help="Create an immutable candidate")
    candidate.add_argument("experiment"); candidate.add_argument("--label", required=True)
    candidate.add_argument("--patch", required=True, help="JSON Patch file or '-' for stdin")
    candidate.add_argument("--rationale", required=True); candidate.add_argument("--parent", default="baseline")
    scenario = sub.add_parser("scenario", help="Create an immutable test input and explicit checks")
    scenario.add_argument("experiment"); scenario.add_argument("--label", required=True)
    scenario.add_argument("--patch", required=True); scenario.add_argument("--description", required=True)
    scenario.add_argument("--origin", choices=["synthetic", "source", "real", "derived"], default="synthetic")
    scenario.add_argument("--checks", help="JSON assertions file")
    run = sub.add_parser("run", help="Replay explicit candidate/scenario pairs; returned tools are not executed")
    run.add_argument("experiment"); run.add_argument("--candidate", action="append", dest="candidates", required=True)
    run.add_argument("--scenario", action="append", dest="scenarios", default=None)
    run.add_argument("--repeats", type=int, default=1); run.add_argument("--profile", required=True)
    diff = sub.add_parser("diff")
    diff.add_argument("experiment"); diff.add_argument("--base", default="baseline")
    diff.add_argument("--candidate", default="baseline"); diff.add_argument("--scenario", default="original")
    diff.add_argument("--artifact", choices=["prompt", "input", "request", "response", "checks", "source"], default="request")
    review = sub.add_parser("review"); rs = review.add_subparsers(dest="review_action", required=True)
    submit = rs.add_parser("submit"); submit.add_argument("experiment"); submit.add_argument("--file", required=True)
    text = rs.add_parser("text"); text.add_argument("experiment"); text.add_argument("review")
    reply = rs.add_parser("reply"); reply.add_argument("experiment"); reply.add_argument("review")
    reply.add_argument("--text"); reply.add_argument("--file"); reply.add_argument("--comment-index", type=int)
    reply.add_argument("--author", default="agent")
    dispatch = rs.add_parser("dispatch", help="Run configured Agent and wait for completion")
    dispatch.add_argument("experiment"); dispatch.add_argument("review")
    export = sub.add_parser("export"); export.add_argument("experiment"); export.add_argument("--out", required=True)
    bundle = sub.add_parser("import-bundle"); bundle.add_argument("path")
    config = sub.add_parser("config"); cs = config.add_subparsers(dest="config_action", required=True)
    cs.add_parser("show")
    profile = cs.add_parser("profile"); profile.add_argument("name")
    for flag in ("base-url", "source-base-url", "source-path", "source-request-pointer", "source-response-pointer", "api-key-env", "token-env", "source-user-env", "source-password-env"):
        profile.add_argument("--" + flag)
    profile.add_argument("--source-fields-json", help="JSON object mapping top-level request field names to source JSON Pointers")
    agent = sub.add_parser("agent"); ats = agent.add_subparsers(dest="agent_action", required=True)
    ac = ats.add_parser("configure"); ac.add_argument("--backend", choices=["codex", "command"], default="codex")
    ac.add_argument("--cwd", type=Path); ac.add_argument("--command-json", help="JSON argv array for custom command; reads task from stdin")
    ac.add_argument("--timeout", type=int, default=600)
    status = ats.add_parser("status"); status.add_argument("job", nargs="?")
    cancel = ats.add_parser("cancel"); cancel.add_argument("job")
    check = sub.add_parser("update-check"); check.add_argument("--force", action="store_true")
    upgrade = sub.add_parser("upgrade"); upgrade.add_argument("--yes", action="store_true", help="Execute the displayed upgrade plan")
    sub.add_parser("changelog")
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        workbench = Workbench(args.workspace)
        exit_code = 0
        cmd = args.command
        if cmd == "init":
            if not config_path(args.workspace).exists():
                save_config(args.workspace, {"schema_version": 1, "profiles": {}})
            result = {"workspace": str(args.workspace.resolve()), "config": str(config_path(args.workspace).resolve())}
        elif cmd == "import":
            source = {"kind": args.format, "profile": args.profile}
            if args.ref:
                source["ref"] = args.source
            elif args.source.startswith(("http://", "https://")):
                source["url"] = args.source
            elif args.source == "-" or Path(args.source).expanduser().is_file():
                source["data"] = read_json(args.source)
            else:
                from .sources import profile_for
                if not args.profile:
                    raise ValueError("Source file was not found; choose --profile with source_path and --ref for an identifier")
                _, configured = profile_for(workbench.store.config(), args.profile)
                if "{ref}" not in configured.get("source_path", ""):
                    raise ValueError("Source references require profile.source_path containing {ref}")
                if any(character in args.source for character in ("/", "\\", ":")) or args.source.startswith((".", "~")) or args.source.lower().endswith(".json"):
                    raise ValueError("Source may be a missing file path; use --ref to explicitly treat it as an identifier")
                source["ref"] = args.source
            if args.response:
                source["response"] = read_json(args.response)
            result = workbench.import_source(source, title=args.title)
        elif cmd == "list": result = workbench.list_experiments()
        elif cmd == "show": result = workbench.get(args.experiment)
        elif cmd in ("demo", "gui"):
            from .server import serve
            if cmd == "demo":
                from .demo import create_demo
                result = create_demo(workbench)
                if args.open:
                    serve(workbench, experiment_id=result["id"])
                    return 0
            else:
                serve(workbench, experiment_id=args.experiment, port=args.port, open_browser=not args.no_open)
                return 0
        elif cmd == "candidate":
            result = workbench.add_candidate(args.experiment, label=args.label, patch=read_json(args.patch), rationale=args.rationale, parent=args.parent)
        elif cmd == "scenario":
            result = workbench.add_scenario(args.experiment, label=args.label, patch=read_json(args.patch), description=args.description, origin=args.origin, checks=read_json(args.checks) if args.checks else None)
        elif cmd == "run":
            previous_runs = len(workbench.get(args.experiment)["runs"])
            result = workbench.run(args.experiment, candidates=args.candidates, scenarios=args.scenarios or ["original"], repeats=args.repeats, profile=args.profile)
            produced = result["runs"][previous_runs:]
            if any(run["status"] in ("error", "failed") for run in produced): exit_code = 4
            elif any(run.get("passed") is False for run in produced): exit_code = 3
        elif cmd == "diff":
            result = workbench.compare(args.experiment, base=args.base, candidate=args.candidate, scenario=args.scenario, artifact=args.artifact)
        elif cmd == "review":
            if args.review_action == "submit": result = workbench.submit_review(args.experiment, read_json(args.file))
            elif args.review_action == "text":
                print(workbench.review_text(args.experiment, args.review)); return 0
            elif args.review_action == "reply":
                if bool(args.text) == bool(args.file): raise ValueError("Choose exactly one of --text or --file")
                text = args.text if args.text else (sys.stdin.read() if args.file == "-" else Path(args.file).read_text())
                result = workbench.reply(args.experiment, args.review, text=text, comment_index=args.comment_index, author=args.author)
            else:
                from .runner import AgentRunner
                runner = AgentRunner(workbench); result = runner.start(args.experiment, args.review)
                try:
                    while result["status"] in ("queued", "running", "cancelling"):
                        time.sleep(0.2); result = runner.status(result["id"])
                except KeyboardInterrupt:
                    runner.cancel(result["id"])
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline and runner.status(result["id"])["status"] in ("queued", "running", "cancelling"):
                        time.sleep(0.1)
                    raise
                emit(result); return 0 if result["status"] == "completed" else 4
        elif cmd == "export": result = {"path": str(workbench.export_bundle(args.experiment, args.out))}
        elif cmd == "import-bundle": result = workbench.import_bundle(args.path)
        elif cmd == "config": result = read_config(args.workspace) if args.config_action == "show" else configure_profile(args, args.workspace)
        elif cmd == "agent":
            from .runner import AgentRunner
            if args.agent_action == "configure":
                if not 1 <= args.timeout <= 7200: raise ValueError("timeout must be 1–7200 seconds")
                agent = {"backend": args.backend, "timeout_seconds": args.timeout}
                if args.cwd:
                    if not args.cwd.is_dir(): raise ValueError("Agent cwd must exist")
                    agent["cwd"] = str(args.cwd.resolve())
                if args.backend == "command":
                    command = json.loads(args.command_json or "null")
                    if not isinstance(command, list) or not command or not all(isinstance(x, str) and x for x in command):
                        raise ValueError("Custom backend requires --command-json '[\"executable\", ...]'")
                    agent["command"] = command
                config = read_config(args.workspace); config["agent"] = agent; save_config(args.workspace, config)
                result = {"agent": agent}
            else:
                runner = AgentRunner(workbench)
                result = runner.cancel(args.job) if args.agent_action == "cancel" else (runner.status(args.job) if args.job else runner.list_jobs())
        elif cmd == "update-check":
            from .updates import update_info
            result = update_info(args.workspace, force=args.force)
        elif cmd == "upgrade":
            from .updates import perform_upgrade, upgrade_plan
            result = perform_upgrade(args.workspace) if args.yes else upgrade_plan()
            if result.get("status") == "failed": exit_code = 4
        else:
            from .updates import changelog
            print(changelog()); return 0
        emit(result)
        return exit_code
    except KeyboardInterrupt:
        print("Cancelled. Completed experiment records are retained.", file=sys.stderr)
        return 130
    except (ValueError, FileNotFoundError, RuntimeError, OSError, KeyError) as exc:
        print(json.dumps({"error": {"code": "operation_failed", "message": str(exc)}}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
