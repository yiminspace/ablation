from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

SCHEMA_VERSION = 1
MAX_CALLS = 30
MAX_REPEATS = 10
MAX_BYTES = 32 * 1024 * 1024
ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:16]}"


def validate_id(value: str) -> str:
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        raise ValueError("Invalid identifier")
    return value


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def pointer_tokens(pointer: str) -> list[str]:
    if not isinstance(pointer, str) or (pointer and not pointer.startswith("/")):
        raise ValueError("JSON Pointer must be empty or start with /")
    tokens = pointer[1:].split("/") if pointer else []
    if any(re.search(r"~(?![01])", token) for token in tokens):
        raise ValueError("Invalid JSON Pointer escape")
    return [token.replace("~1", "/").replace("~0", "~") for token in tokens]


def index_of(token: str, length: int, *, add: bool = False) -> int:
    if add and token == "-":
        return length
    if not re.fullmatch(r"0|[1-9][0-9]*", token):
        raise ValueError("Invalid JSON array index")
    index = int(token)
    if index > length or (not add and index == length):
        raise ValueError("JSON array index is out of bounds")
    return index


def pointer_get(value: Any, pointer: str) -> Any:
    for token in pointer_tokens(pointer):
        if isinstance(value, list):
            value = value[index_of(token, len(value))]
        elif isinstance(value, dict) and token in value:
            value = value[token]
        else:
            raise ValueError(f"JSON Pointer does not exist: {pointer}")
    return value


def apply_patch(value: Any, patch: list) -> Any:
    if not isinstance(patch, list) or len(patch) > 200:
        raise ValueError("Patch must be a list with at most 200 operations")
    result = copy.deepcopy(value)
    for operation in patch:
        if not isinstance(operation, dict):
            raise ValueError("Patch operation must be an object")
        op, path = operation.get("op"), operation.get("path")
        if op not in {"add", "replace", "remove"}:
            raise ValueError("Supported patch operations: add, replace, remove")
        tokens = pointer_tokens(path)
        if op != "remove" and "value" not in operation:
            raise ValueError("Patch add/replace requires value")
        if not tokens:
            if op == "remove":
                raise ValueError("Cannot remove the request root")
            result = copy.deepcopy(operation["value"])
            continue
        parent = result
        for token in tokens[:-1]:
            if isinstance(parent, list):
                parent = parent[index_of(token, len(parent))]
            elif isinstance(parent, dict) and token in parent:
                parent = parent[token]
            else:
                raise ValueError(f"Patch parent does not exist: {path}")
        key = tokens[-1]
        if isinstance(parent, list):
            index = index_of(key, len(parent), add=op == "add")
            if op == "add":
                parent.insert(index, copy.deepcopy(operation["value"]))
            elif op == "replace":
                parent[index] = copy.deepcopy(operation["value"])
            else:
                parent.pop(index)
        elif isinstance(parent, dict):
            if op != "add" and key not in parent:
                raise ValueError(f"Patch target does not exist: {path}")
            if op == "remove":
                del parent[key]
            else:
                parent[key] = copy.deepcopy(operation["value"])
        else:
            raise ValueError(f"Patch target is not a container: {path}")
    canonical(result)
    return result


def validate_checks(checks: list | None) -> list:
    checks = [] if checks is None else copy.deepcopy(checks)
    if not isinstance(checks, list) or len(checks) > 100:
        raise ValueError("Checks must be a list with at most 100 assertions")
    for check in checks:
        if not isinstance(check, dict):
            raise ValueError("Each check must be an object")
        kind = check.get("kind")
        if kind == "json_equals":
            pointer_tokens(check.get("pointer"))
            if "value" not in check:
                raise ValueError("json_equals requires value")
        elif kind == "text_contains":
            if not isinstance(check.get("value"), str) or not check["value"]:
                raise ValueError("text_contains requires nonempty string value")
        elif kind in {"tool_called", "tool_argument_equals"}:
            if not isinstance(check.get("name"), str) or not check["name"]:
                raise ValueError(f"{kind} requires a name")
            if kind == "tool_argument_equals":
                pointer_tokens(check.get("pointer"))
                if "value" not in check:
                    raise ValueError("tool_argument_equals requires value")
        else:
            raise ValueError(f"Unknown check kind: {kind}")
    canonical(checks)
    return checks
