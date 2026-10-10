from __future__ import annotations

import base64
import copy
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .models import MAX_BYTES, canonical, pointer_get, pointer_tokens
from .storage import load_json

SECRET_KEYS = {"authorization", "proxy-authorization", "cookie", "set-cookie", "api_key", "api-key", "apikey", "x-api-key", "access_token", "refresh_token", "token", "password", "client_secret"}
HEADER_FIELDS = {"headers", "requestheaders", "responseheaders", "rawheaders"}
QUERY_CREDENTIAL_KEYS = SECRET_KEYS | {"key", "auth", "access_key", "signature", "sig", "x-amz-credential", "x-amz-signature", "x-goog-credential", "x-goog-signature"}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def configured_secrets(config: dict) -> list[str]:
    values = []
    def visit(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key.endswith("_env") and key != "source_user_env" and isinstance(item, str) and os.environ.get(item):
                    values.append(os.environ[item])
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
    visit(config.get("profiles", {}))
    return sorted(set(values), key=len, reverse=True)


def secret_name(value: Any) -> bool:
    return isinstance(value, str) and value.strip().lower() in SECRET_KEYS


def masked_header(value: Any) -> Any:
    return ["[REDACTED]" for _ in value] if isinstance(value, list) else "[REDACTED]"


def redact_headers(value: Any, secrets: list[str]) -> Any:
    if isinstance(value, dict):
        return {key: masked_header(item) if secret_name(key) else redact(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        if value and len(value) % 2 == 0 and all(isinstance(value[index], str) and re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", value[index]) for index in range(0, len(value), 2)):
            return [part for index in range(0, len(value), 2) for part in (value[index], masked_header(value[index + 1]) if secret_name(value[index]) else redact(value[index + 1], secrets))]
        result = []
        for item in value:
            if isinstance(item, dict):
                name = next((field for key, field in item.items() if key.lower() in {"name", "key"}), None)
                result.append({key: masked_header(field) if secret_name(name) and key.lower() in {"value", "values"} else redact(field, secrets) for key, field in item.items()})
            elif isinstance(item, list) and len(item) == 2 and isinstance(item[0], str):
                result.append([item[0], masked_header(item[1]) if secret_name(item[0]) else redact(item[1], secrets)])
            elif isinstance(item, str) and ":" in item and secret_name(item.split(":", 1)[0]):
                result.append(item.split(":", 1)[0] + ": [REDACTED]")
            else:
                result.append(redact(item, secrets))
        return result
    return redact(value, secrets)


def redact(value: Any, secrets: list[str] | None = None) -> Any:
    secrets = secrets or []
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if secret_name(key) and isinstance(item, (str, int, float, list)):
                result[key] = masked_header(item)
            elif key.lower().replace("_", "").replace("-", "") in HEADER_FIELDS:
                result[key] = redact_headers(item, secrets)
            else:
                result[key] = redact(item, secrets)
        return result
    if isinstance(value, list):
        return [redact(item, secrets) for item in value]
    if isinstance(value, str):
        if value.lstrip().startswith(("{", "[")):
            try:
                parsed = json.loads(value)
                clean = redact(parsed, secrets)
                if clean != parsed:
                    value = json.dumps(clean, ensure_ascii=False)
            except (ValueError, RecursionError):
                pass
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[REDACTED]")
        return value
    return value


def validate_url(url: str) -> str:
    if not isinstance(url, str):
        raise ValueError("Endpoint must be a URL")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("Use an http(s) endpoint without embedded credentials or fragment")
    if parsed.query:
        raise ValueError("Configured endpoint must not contain a query string")
    return url.rstrip("/")


def profile_for(config: dict, name: str) -> tuple[str, dict]:
    profiles = config.get("profiles", {})
    if not name and len(profiles) == 1:
        name = next(iter(profiles))
    if not name or name not in profiles:
        raise ValueError("Choose a configured profile; source and replay profiles are independent")
    profile = profiles[name]
    if not isinstance(profile, dict):
        raise ValueError("Profile must be an object")
    return name, profile


def env_value(name: str) -> str:
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError("Credentials must use an environment variable name")
    value = os.environ.get(name)
    if not value:
        raise ValueError(f"Missing credential environment variable: {name}")
    return value


def validate_source_profile(profile: dict) -> None:
    if "source_base_url" in profile:
        validate_url(profile["source_base_url"])
    if "source_path" in profile:
        template = profile["source_path"]
        if not isinstance(template, str) or not template or any(ord(char) <= 32 for char in template):
            raise ValueError("source_path must be a nonempty URL path template")
        parsed = urllib.parse.urlsplit(template)
        reject_query_credentials(parsed.query)
        decoded = urllib.parse.unquote(parsed.path)
        if parsed.scheme or parsed.netloc or parsed.fragment or decoded.startswith("//") or "\\" in decoded:
            raise ValueError("source_path must stay on the configured origin")
        if any(segment in {".", ".."} for segment in decoded.split("/")):
            raise ValueError("source_path must not contain traversal segments")
        remaining = template.replace("{ref}", "reference")
        if "{" in remaining or "}" in remaining:
            raise ValueError("source_path supports only the {ref} placeholder")
    for key in ("source_request_pointer", "source_response_pointer"):
        if key in profile:
            pointer_tokens(profile[key])
    fields = profile.get("source_fields", {})
    if not isinstance(fields, dict):
        raise ValueError("source_fields must be an object mapping request fields to JSON Pointers")
    for field, pointer in fields.items():
        if not isinstance(field, str) or not field:
            raise ValueError("source_fields requires nonempty top-level request field names")
        pointer_tokens(pointer)
    basic = bool(profile.get("source_user_env") or profile.get("source_password_env"))
    if basic and profile.get("token_env"):
        raise ValueError("Choose source Basic credentials or token_env, not both")
    if basic and not (profile.get("source_user_env") and profile.get("source_password_env")):
        raise ValueError("Source Basic authentication requires both source_user_env and source_password_env")
    for key in ("token_env", "source_user_env", "source_password_env"):
        if key in profile and not (isinstance(profile[key], str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", profile[key])):
            raise ValueError(f"{key} must be an environment variable name")


def source_headers(profile: dict) -> dict[str, str]:
    validate_source_profile(profile)
    headers = {"Accept": "application/json"}
    if profile.get("source_user_env"):
        user = env_value(profile["source_user_env"])
        password = env_value(profile["source_password_env"])
        headers["Authorization"] = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
    elif profile.get("token_env"):
        headers["Authorization"] = f"Bearer {env_value(profile['token_env'])}"
    return headers


def reject_query_credentials(query: str) -> None:
    if any(name.strip().lower() in QUERY_CREDENTIAL_KEYS for name, _ in urllib.parse.parse_qsl(query, keep_blank_values=True)):
        raise ValueError("Source credentials must use configured environment-backed headers, not URL query parameters")


def fetch(url: str, headers: dict, *, timeout: float = 30) -> tuple[bytes, str]:
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=timeout) as response:
            body = response.read(MAX_BYTES + 1)
            if len(body) > MAX_BYTES:
                raise ValueError("Remote body exceeds 32 MiB")
            return body, response.headers.get("Content-Type", "")
    except urllib.error.HTTPError as error:
        raise ValueError(f"Source request failed with HTTP {error.code}") from error
    except urllib.error.URLError as error:
        raise ValueError("Source endpoint could not be reached") from error


def decode_json(value: Any) -> Any:
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError as error:
            raise ValueError("Expected JSON request data") from error
    return copy.deepcopy(value)


def validate_request(request: Any) -> str:
    if not isinstance(request, dict):
        raise ValueError("Model request must be a JSON object")
    if not isinstance(request.get("model"), str) or not request["model"].strip():
        raise ValueError("Model request requires model")
    if "messages" in request:
        if "input" in request or not isinstance(request["messages"], list):
            raise ValueError("Chat request requires messages and must not include Responses input")
        kind = "chat"
    elif "input" in request:
        if not isinstance(request["input"], (list, str)):
            raise ValueError("Responses input must be a string or list")
        kind = "responses"
    else:
        raise ValueError("Model request requires messages or input")
    if "tools" in request and not isinstance(request["tools"], list):
        raise ValueError("Request tools must be an array")
    canonical(request)
    return kind


def source_url(source: dict, profile: dict) -> str:
    validate_source_profile(profile)
    base = validate_url(profile.get("source_base_url", ""))
    if "ref" in source:
        reference = source["ref"]
        template = profile.get("source_path", "")
        if not isinstance(reference, str) or not reference or len(reference) > 2048:
            raise ValueError("Source ref must be a nonempty string of at most 2048 characters")
        if "{ref}" not in template:
            raise ValueError("A source reference requires a configured source_path containing {ref}")
        encoded = urllib.parse.quote(reference, safe="")
        if encoded in {".", ".."}:
            encoded = encoded.replace(".", "%2E")
        url = base + "/" + template.lstrip("/").replace("{ref}", encoded)
    else:
        url = source.get("url")
    if not isinstance(url, str) or any(ord(char) <= 32 for char in url) or "\\" in url:
        raise ValueError("Use a complete http(s) JSON URL without whitespace or backslashes")
    parsed, configured = urllib.parse.urlsplit(url), urllib.parse.urlsplit(base)
    reject_query_credentials(parsed.query)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("Use a complete http(s) JSON URL without embedded credentials or fragment")
    origin = lambda parts: (parts.scheme, parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))
    if origin(parsed) != origin(configured):
        raise ValueError("Source URL does not match configured source origin")
    return url


def remote_source(source: dict, config: dict) -> tuple[dict, str, str, dict]:
    name, profile = profile_for(config, source.get("profile", ""))
    url = source_url(source, profile)
    raw, _ = fetch(url, source_headers(profile))
    return decode_json(raw), url, name, profile


def normalize_log(data: dict) -> dict:
    params = data.get("params", {})
    if not isinstance(params, dict):
        raise ValueError("Log params must be an object; configure a request pointer or field mapping for another shape")
    request = copy.deepcopy(params)
    for key in ("model", "input", "messages", "tools"):
        if key in data:
            request[key] = copy.deepcopy(data[key])
    return request


def capture_body(value: Any) -> Any:
    if isinstance(value, dict) and "body" in value and not any(key in value for key in ("model", "output", "choices")):
        return value["body"]
    return value


def historical_response(value: Any) -> Any:
    value = capture_body(value)
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return copy.deepcopy(value)


def normalize_source(source: dict, config: dict) -> tuple[dict, dict, Any, str]:
    if not isinstance(source, dict):
        raise ValueError("Source must be an object")
    kind = source.get("kind", "request")
    if kind not in {"request", "log", "capture"}:
        raise ValueError("Supported sources: request, log, capture")
    provided = [key for key in ("data", "path", "url", "ref") if key in source and source[key] is not None]
    if len(provided) != 1:
        raise ValueError("Provide exactly one source data, path, url or ref")
    notes, profile_name, profile = [], "", {}
    if provided[0] in {"url", "ref"}:
        raw, reference, profile_name, profile = remote_source(source, config)
    else:
        if source.get("profile"):
            profile_name, profile = profile_for(config, source["profile"])
        if provided[0] == "data":
            raw, reference = decode_json(source["data"]), "uploaded JSON"
        else:
            raw, reference = load_json(source["path"]), str(source["path"])
    validate_source_profile(profile)
    if not isinstance(raw, dict):
        raise ValueError("Source must contain a JSON object")
    secrets = configured_secrets(config)
    evidence = redact(raw, secrets)
    if evidence != raw:
        notes.append("Credential fields or configured credential values were redacted before persistence.")
    mapping = {key: copy.deepcopy(profile[key]) for key in ("source_request_pointer", "source_response_pointer", "source_fields") if key in profile}
    if "source_request_pointer" in profile:
        request = decode_json(pointer_get(evidence, profile["source_request_pointer"]))
        notes.append("Complete request selected by configured JSON Pointer from the source document.")
    elif kind == "log":
        request = normalize_log(evidence)
    elif kind == "capture":
        if "request" not in evidence:
            raise ValueError("Capture must contain request; configure source_request_pointer for another shape")
        request = decode_json(capture_body(evidence["request"]))
    else:
        request = copy.deepcopy(evidence)
    if not isinstance(request, dict):
        raise ValueError("Selected request must be a JSON object")
    for field, pointer in profile.get("source_fields", {}).items():
        request[field] = copy.deepcopy(pointer_get(evidence, pointer))
    if profile.get("source_fields"):
        notes.append("Configured source_fields explicitly overlay top-level request fields; all other selected request parameters are retained.")
    if "source_response_pointer" in profile:
        response = historical_response(pointer_get(evidence, profile["source_response_pointer"]))
    else:
        response = historical_response(evidence.get("response")) if kind in {"log", "capture"} else None
    if "response" in source:
        response = historical_response(redact(source["response"], secrets))
        notes.append("Historical response supplied separately by the importer.")
    if kind == "log" or mapping:
        fidelity = "reconstructed"
        notes.append("Reconstructed from a JSON source and explicit mappings, not a byte-for-byte wire capture. Missing request fields cannot be recovered; no transport defaults are inferred.")
    elif kind == "capture":
        fidelity = "recorded-json-body"
        notes.append("Selected the recorded JSON request body; headers, encoding and interceptor history are not reconstructed or assumed.")
    else:
        fidelity = "provided"
    request = redact(request, secrets)
    api_kind = validate_request(request)
    provenance = {"kind": kind, "ref": redact(reference, secrets), "fidelity": fidelity, "notes": notes, "evidence": evidence}
    if profile_name:
        provenance["profile"] = profile_name
    if mapping:
        provenance["mapping"] = mapping
    return provenance, request, response, api_kind
