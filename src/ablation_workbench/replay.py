from __future__ import annotations

import copy
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .models import MAX_BYTES, canonical, pointer_get, validate_checks
from .sources import NoRedirect, env_value, redact, validate_url


def response_text(response: Any) -> str:
    if not isinstance(response, dict):
        return ""
    pieces = []
    if isinstance(response.get("output_text"), str):
        pieces.append(response["output_text"])
    for item in response.get("output", []) or []:
        if isinstance(item, dict) and item.get("type") == "message":
            for part in item.get("content", []) or []:
                if isinstance(part, dict) and part.get("type") in {"output_text", "text"}:
                    pieces.append(str(part.get("text", "")))
    for choice in response.get("choices", []) or []:
        content = (choice.get("message") or {}).get("content") if isinstance(choice, dict) else None
        if isinstance(content, str):
            pieces.append(content)
        elif isinstance(content, list):
            pieces.extend(part.get("text", "") for part in content if isinstance(part, dict) and isinstance(part.get("text"), str))
    return "\n".join(pieces)


def called_tools(response: Any) -> set[str]:
    names = set()
    if not isinstance(response, dict):
        return names
    for item in response.get("output", []) or []:
        if isinstance(item, dict) and item.get("type") == "function_call" and isinstance(item.get("name"), str):
            names.add(item["name"])
    for choice in response.get("choices", []) or []:
        message = choice.get("message", {}) if isinstance(choice, dict) else {}
        for item in message.get("tool_calls", []) or []:
            name = (item.get("function") or {}).get("name") if isinstance(item, dict) else None
            if isinstance(name, str):
                names.add(name)
        legacy = message.get("function_call") or {}
        if isinstance(legacy.get("name"), str):
            names.add(legacy["name"])
    return names


def tool_arguments(response: dict, name: str) -> list[Any]:
    arguments = []
    for item in response.get("output", []) or []:
        if isinstance(item, dict) and item.get("type") == "function_call" and item.get("name") == name:
            arguments.append(item.get("arguments"))
    for choice in response.get("choices", []) or []:
        message = choice.get("message", {}) if isinstance(choice, dict) else {}
        functions = [item.get("function", {}) for item in message.get("tool_calls", []) or [] if isinstance(item, dict)]
        if message.get("function_call"):
            functions.append(message["function_call"])
        for function in functions:
            if isinstance(function, dict) and function.get("name") == name:
                arguments.append(function.get("arguments"))
    return arguments


def evaluate_checks(response: dict, checks: list) -> list[dict]:
    results = []
    for check in validate_checks(checks):
        result = copy.deepcopy(check)
        kind = check["kind"]
        if kind == "json_equals":
            try:
                actual = pointer_get(response, check["pointer"])
                passed = canonical(actual) == canonical(check["value"])
                result["actual"] = actual
            except ValueError:
                passed = False
                result["error"] = "JSON Pointer was absent from the response"
        elif kind == "text_contains":
            actual = response_text(response)
            passed = check["value"] in actual
            result["actual"] = actual
        elif kind == "tool_argument_equals":
            actual, errors = [], []
            for arguments in tool_arguments(response, check["name"]):
                try:
                    parsed = json.loads(arguments) if isinstance(arguments, str) else arguments
                    actual.append(pointer_get(parsed, check["pointer"]))
                except (ValueError, TypeError):
                    errors.append("Tool arguments were not valid JSON or the pointer was absent")
            passed = any(canonical(value) == canonical(check["value"]) for value in actual)
            result["actual"] = actual
            if errors:
                result["errors"] = errors
        else:
            actual = sorted(called_tools(response))
            passed = check["name"] in actual
            result["actual"] = actual
        result["passed"] = passed
        results.append(result)
    return results


def parse_response(raw: bytes, content_type: str, kind: str) -> tuple[dict, list]:
    text = raw.decode("utf-8")
    if "text/event-stream" not in content_type and not text.lstrip().startswith(("data:", "event:", ":")):
        try:
            response = json.loads(text)
        except json.JSONDecodeError as error:
            raise ValueError("Model endpoint returned invalid JSON") from error
        if not isinstance(response, dict):
            raise ValueError("Model endpoint response must be an object")
        return response, []
    events, done = [], False
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    for block in normalized.split("\n\n"):
        payload = "\n".join(line[5:].lstrip(" ") for line in block.splitlines() if line.startswith("data:"))
        if not payload:
            continue
        if payload == "[DONE]":
            done = True
            continue
        try:
            event = json.loads(payload)
        except json.JSONDecodeError as error:
            raise ValueError("Invalid JSON in model SSE event") from error
        if not isinstance(event, dict):
            raise ValueError("Invalid SSE event object")
        if event.get("error") or event.get("type") in {"error", "response.failed"}:
            raise ValueError("Model stream reported an error")
        events.append(event)
    if kind == "responses":
        for event in reversed(events):
            if event.get("type") == "response.completed" and isinstance(event.get("response"), dict):
                return event["response"], events
        raise ValueError("Responses stream ended without response.completed; output is unconfirmed")
    choices: dict[int, dict] = {}
    result: dict = {"object": "chat.completion", "choices": []}
    for event in events:
        for key in ("id", "model", "created", "usage", "system_fingerprint"):
            if key in event:
                result[key] = event[key]
        for item in event.get("choices", []) or []:
            index = item.get("index", 0)
            choice = choices.setdefault(index, {"index": index, "message": {"role": "assistant", "content": ""}, "finish_reason": None})
            delta = item.get("delta") or {}
            message = choice["message"]
            if isinstance(delta.get("content"), str):
                message["content"] += delta["content"]
            if delta.get("role"):
                message["role"] = delta["role"]
            if delta.get("refusal"):
                message["refusal"] = message.get("refusal", "") + delta["refusal"]
            for tool in delta.get("tool_calls", []) or []:
                tools = message.setdefault("_tools", {})
                target = tools.setdefault(tool.get("index", 0), {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                if tool.get("id"):
                    target["id"] = tool["id"]
                function = tool.get("function") or {}
                for key in ("name", "arguments"):
                    if isinstance(function.get(key), str):
                        target["function"][key] += function[key]
            if item.get("finish_reason") is not None:
                choice["finish_reason"] = item["finish_reason"]
    if not choices or not (done or all(choice["finish_reason"] is not None for choice in choices.values())):
        raise ValueError("Chat stream ended before completion; output is unconfirmed")
    for index in sorted(choices):
        choice = choices[index]
        tools = choice["message"].pop("_tools", {})
        if tools:
            choice["message"]["tool_calls"] = [tools[key] for key in sorted(tools)]
        result["choices"].append(choice)
    return result, events


def endpoint_for(profile: dict, kind: str) -> str:
    base = validate_url(profile.get("base_url", ""))
    suffix = "/responses" if kind == "responses" else "/chat/completions"
    if base.endswith(("/responses", "/chat/completions")):
        if not base.endswith(suffix):
            raise ValueError("Replay endpoint and request API family do not match")
        return base
    return base + suffix if base.endswith("/v1") else base + "/v1" + suffix


def replay_request(request: dict, profile: dict, kind: str, secrets: list[str] | None = None) -> dict:
    url = endpoint_for(profile, kind)
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if profile.get("api_key_env"):
        headers["Authorization"] = f"Bearer {env_value(profile['api_key_env'])}"
    headers.update(profile.get("_tracking_headers", {}))
    observed_headers = {}
    timeout = profile.get("timeout_seconds", 120)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= timeout <= 300:
        raise ValueError("Replay timeout_seconds must be between 1 and 300")
    started = time.monotonic()
    result: dict = {"status": "error", "response": None, "checks": [], "passed": None, "target": url}
    payload = canonical(request).encode()
    try:
        http_request = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        with urllib.request.build_opener(NoRedirect).open(http_request, timeout=timeout) as http_response:
            observed_headers = {name.lower(): value for name, value in http_response.headers.items()}
            body = http_response.read(MAX_BYTES + 1)
            if len(body) > MAX_BYTES:
                raise ValueError("Model response exceeds 32 MiB")
            try:
                response, events = parse_response(body, http_response.headers.get("Content-Type", ""), kind)
            except (ValueError, UnicodeError, TypeError, KeyError):
                result["response"] = {"unparsed_body": redact(body.decode("utf-8", errors="replace"), secrets)}
                raise
            response = redact(response, secrets)
            result["response"] = response
            if events:
                result["stream_events"] = redact(events, secrets)
            if response.get("error") or response.get("status") in {"failed", "incomplete", "cancelled", "in_progress", "queued"}:
                result["error"] = "Provider did not confirm a completed response"
            elif not (isinstance(response.get("choices"), list) if kind == "chat" else isinstance(response.get("output"), list)):
                result["error"] = "Response did not contain expected model output"
            else:
                result["status"] = "completed"
    except urllib.error.HTTPError as error:
        observed_headers = {name.lower(): value for name, value in error.headers.items()}
        result["error"] = f"Model endpoint returned HTTP {error.code}"
        raw = error.read(65536)
        try:
            result["response"] = redact(json.loads(raw), secrets)
        except (ValueError, UnicodeError):
            result["response"] = {"body": redact(raw.decode("utf-8", errors="replace"), secrets)}
    except (urllib.error.URLError, TimeoutError, OSError):
        result["error"] = "Model endpoint could not be reached or request timed out"
    except (ValueError, UnicodeError, TypeError, KeyError) as error:
        result["error"] = redact(str(error), secrets)
    result["elapsed_ms"] = round((time.monotonic() - started) * 1000)
    from .observation import collect_evidence
    observers = profile.get("observers", [])
    allowed = {"x-request-id", "request-id"} | {observer.get(key, "").lower() for observer in observers for key in ("request_id_header", "record_id_header", "url_header")}
    result["response_headers"] = redact({k: v for k, v in observed_headers.items() if k in allowed}, secrets)
    result["external_evidence"] = collect_evidence(observers, profile.get("_tracking_context", {}), result["response"], observed_headers, secrets or [])
    return result
