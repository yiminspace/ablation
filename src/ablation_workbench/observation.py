from __future__ import annotations

import json
import re
import time
from string import Formatter
from urllib.parse import quote, urlsplit

from .models import pointer_get, pointer_tokens
from .sources import fetch, redact, reject_query_credentials, source_headers, validate_source_profile, secret_name

_CONTEXT = {"experiment_id", "batch_id", "run_id", "request_id", "record_id", "response_id", "start_time", "end_time"}
_HEADER = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")


def render(template: str, context: dict, *, url: bool = False) -> str:
    values = {key: quote(str(value), safe="") if url else str(value) for key, value in context.items()}
    return template.format_map(values)


def validate_template(template: str) -> None:
    if not isinstance(template, str) or not template or any(ord(c) < 32 for c in template):
        raise ValueError("Observation templates must be nonempty strings without control characters")
    for _, field, spec, conversion in Formatter().parse(template):
        if field is not None and (field not in _CONTEXT or spec or conversion):
            raise ValueError("Unknown observation placeholder or formatting directive")


def evidence_url(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Evidence links must be HTTP(S) URLs without credentials")
    if any(ord(c) <= 32 for c in url):
        raise ValueError("Evidence links must not contain whitespace or control characters")
    reject_query_credentials(parsed.query)
    return url


def validate_observers(observers: list) -> None:
    if not isinstance(observers, list) or len(observers) > 16:
        raise ValueError("Observers must be a list of at most 16 configurations")
    names, headers = set(), set()
    for observer in observers:
        if not isinstance(observer, dict) or not isinstance(observer.get("name"), str) or not observer["name"].strip():
            raise ValueError("Each observer requires a name")
        if observer["name"] in names:
            raise ValueError("Observer names must be unique")
        names.add(observer["name"])
        allowed = {"name", "headers", "request_id_pointer", "request_id_header", "record_id_pointer", "record_id_header", "url_pointer", "url_header", "url_template", "batch_url_template", "lookup"}
        if set(observer) - allowed:
            raise ValueError("Unknown observer configuration field")
        if not isinstance(observer.get("headers", {}), dict):
            raise ValueError("Observer headers must be an object")
        for name, template in observer.get("headers", {}).items():
            if not isinstance(name, str) or not _HEADER.fullmatch(name) or secret_name(name) or name.lower() in {"authorization", "proxy-authorization", "cookie", "host", "content-type", "accept", "content-length"}:
                raise ValueError("Observation headers cannot override transport or authentication headers")
            if name.lower() in headers:
                raise ValueError("Observation headers must not conflict")
            headers.add(name.lower())
            validate_template(template)
            fields = {f for _, f, _, _ in Formatter().parse(template) if f}
            if not fields <= {"experiment_id", "batch_id", "run_id"}:
                raise ValueError("Request headers can reference only local tracking IDs")
        for key in ("request_id_pointer", "record_id_pointer", "url_pointer"):
            if key in observer:
                pointer_tokens(observer[key])
        for key in ("request_id_header", "record_id_header", "url_header"):
            if key in observer and (not isinstance(observer[key], str) or not _HEADER.fullmatch(observer[key])):
                raise ValueError("Response header name is invalid")
        for key in ("url_template", "batch_url_template"):
            if key in observer:
                validate_template(observer[key])
                evidence_url(render(observer[key], dict.fromkeys(_CONTEXT, "fixture"), url=True))
        if "lookup" in observer:
            lookup = observer["lookup"]
            if not isinstance(lookup, dict):
                raise ValueError("Observation lookup must be an object")
            allowed_lookup = {"source_base_url", "source_path", "token_env", "source_user_env", "source_password_env", "records_pointer", "match_pointer", "match_value", "record_id_pointer", "url_pointer", "attempts", "interval_seconds"}
            if set(lookup) - allowed_lookup:
                raise ValueError("Unknown observation lookup field")
            validate_template(lookup.get("source_path"))
            if not lookup["source_path"].startswith("/"):
                raise ValueError("Observation lookup source_path must start with /")
            validate_source_profile({**lookup, "source_path": render(lookup["source_path"], dict.fromkeys(_CONTEXT, "fixture"), url=True)})
            if "source_base_url" not in lookup:
                raise ValueError("Observation lookup requires a separate source_base_url")
            for key in ("records_pointer", "match_pointer", "record_id_pointer", "url_pointer"):
                if key in lookup:
                    pointer_tokens(lookup[key])
            if "match_pointer" not in lookup or "match_value" not in lookup:
                raise ValueError("Lookup requires an exact match_pointer and match_value")
            validate_template(lookup["match_value"])
            attempts = lookup.get("attempts", 1)
            interval = lookup.get("interval_seconds", 1)
            if isinstance(attempts, bool) or not isinstance(attempts, int) or not 1 <= attempts <= 5:
                raise ValueError("Lookup attempts must be an integer from 1 to 5")
            if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not 0 <= interval <= 5:
                raise ValueError("Lookup interval_seconds must be between 0 and 5")


def tracking_headers(observers: list, context: dict) -> dict:
    validate_observers(observers)
    return {name: render(template, context) for observer in observers for name, template in observer.get("headers", {}).items()}


def extract(document: dict, pointer: str | None):
    if pointer is None:
        return None
    try:
        value = pointer_get(document, pointer)
        return str(value) if isinstance(value, (str, int)) and not isinstance(value, bool) else None
    except ValueError:
        return None


def collect_evidence(observers: list, context: dict, response: dict | None, headers: dict, secrets: list) -> list:
    evidence = []
    response = response or {}
    context = {**context, "response_id": extract(response, "/id") or ""}
    for observer in observers:
        item = {"platform": observer["name"], "status": "unavailable"}
        values = {**context}
        try:
            request_id = extract(response, observer.get("request_id_pointer")) or headers.get(observer.get("request_id_header", "").lower())
            if request_id:
                item["request_id"] = request_id
                values["request_id"] = request_id
            record_id = extract(response, observer.get("record_id_pointer")) or headers.get(observer.get("record_id_header", "").lower())
            url = extract(response, observer.get("url_pointer")) or headers.get(observer.get("url_header", "").lower())
            if record_id or url:
                item["status"] = "available"
            lookup = observer.get("lookup")
            if lookup:
                settings = {**lookup, "source_path": render(lookup["source_path"], values, url=True)}
                attempts = lookup.get("attempts", 1)
                for attempt in range(attempts):
                    raw, _ = fetch(settings["source_base_url"].rstrip("/") + settings["source_path"], source_headers(settings), timeout=5)
                    document = json.loads(raw)
                    records = pointer_get(document, lookup.get("records_pointer", ""))
                    records = records if isinstance(records, list) else [records]
                    expected = render(lookup["match_value"], values)
                    matches = [r for r in records if extract(r, lookup["match_pointer"]) == expected]
                    if matches or attempt + 1 == attempts:
                        break
                    time.sleep(lookup.get("interval_seconds", 1))
                if len(matches) != 1:
                    raise ValueError("External log was not uniquely matched")
                record_id = extract(matches[0], lookup.get("record_id_pointer"))
                url = extract(matches[0], lookup.get("url_pointer"))
                if not (record_id or url):
                    raise ValueError("Matched external log did not contain a record ID or URL")
                item["status"] = "available"
            if record_id:
                item["record_id"] = record_id
                values["record_id"] = record_id
            if url:
                item["url"] = evidence_url(url)
            elif "url_template" in observer:
                item["url"] = evidence_url(render(observer["url_template"], values, url=True))
                if item["status"] == "unavailable":
                    item["status"] = "linked"
            if "batch_url_template" in observer:
                item["batch_url"] = evidence_url(render(observer["batch_url_template"], values, url=True))
            if item["status"] == "unavailable":
                item["error"] = "No external log was resolved; a request ID or list link alone does not identify a log"
        except (ValueError, KeyError, TypeError, OSError) as error:
            item["status"] = "unavailable"
            item["error"] = str(error)
        evidence.append(redact(item, secrets))
    return evidence
