"""Explicit, evidence-linked tool continuations; no tools are executed here."""
from __future__ import annotations

import copy

from .models import apply_patch, pointer_tokens, validate_checks
from .sources import validate_request


def pending_calls(response: dict) -> list[dict]:
    if not isinstance(response, dict):
        return []
    if isinstance(response.get("output"), list):
        return [{"call_id": item.get("call_id"), "name": item.get("name"), "arguments": item.get("arguments")}
                for item in response["output"] if isinstance(item, dict) and item.get("type") == "function_call"]
    choices = response.get("choices", [])
    if len(choices) == 1:
        return [{"call_id": item.get("id"), **item.get("function", {})}
                for item in choices[0].get("message", {}).get("tool_calls", [])]
    return []


def prepare_continuation(parent: dict, record: dict) -> dict:
    if parent.get("status") != "completed" or not isinstance(parent.get("response"), dict):
        raise ValueError("Continue a completed model response with recorded tool calls")
    if not isinstance(record, dict) or set(record) != {"rationale", "tool_results", "context_patch", "checks"}:
        raise ValueError("Continuation requires rationale, tool_results, context_patch and checks")
    if not isinstance(record["rationale"], str) or not record["rationale"].strip():
        raise ValueError("Explain why this response needs continuation and the origin of supplied context")
    validate_checks(record["checks"])
    calls = pending_calls(parent["response"])
    ids = [call.get("call_id") for call in calls]
    if not ids or any(not isinstance(i, str) or not i for i in ids) or len(set(ids)) != len(ids):
        raise ValueError("Parent must contain uniquely identified tool calls")
    results = record["tool_results"]
    if not isinstance(results, list) or any(not isinstance(r, dict) or set(r) != {"call_id", "output"} or not isinstance(r["output"], str) for r in results):
        raise ValueError("Tool results must contain call_id and a string output from the tool or runtime")
    result_ids = [r["call_id"] for r in results]
    if len(result_ids) != len(ids) or set(result_ids) != set(ids):
        raise ValueError("Supply exactly one result for each recorded tool call")
    patch = record["context_patch"]
    if not isinstance(patch, list):
        raise ValueError("Context patch must be a JSON Patch list")
    for op in patch:
        if not isinstance(op, dict) or op.get("op") not in {"add", "replace", "remove", "test"}:
            raise ValueError("Context patch supports add, replace, remove and test")
        tokens = pointer_tokens(op.get("path"))
        if not tokens or tokens[0] not in {"instructions", "tools"}:
            raise ValueError("Context patch may only change instructions or tools; history is assembled from evidence")
    request = apply_patch(parent["request"], patch)
    response = parent["response"]
    if response.get("status") in {"incomplete", "failed", "cancelled"}:
        raise ValueError("The model response is incomplete; investigate the execution failure first")
    if "input" in request:
        history = request["input"]
        if isinstance(history, str):
            history = [{"role": "user", "content": history}]
        history = copy.deepcopy(history)
        history.extend(copy.deepcopy(response["output"]))
        history.extend({"type": "function_call_output", "call_id": r["call_id"], "output": r["output"]} for r in results)
        request["input"] = history
    else:
        request["messages"].append(copy.deepcopy(response["choices"][0]["message"]))
        request["messages"].extend({"role": "tool", "tool_call_id": r["call_id"], "content": r["output"]} for r in results)
    validate_request(request)
    return request
