#!/usr/bin/env python3
import json
import os
import sys
import time
from pathlib import Path


def safe_len(value):
    if value is None:
        return 0
    if isinstance(value, str):
        return len(value.encode("utf-8", errors="ignore"))
    try:
        return len(json.dumps(value).encode("utf-8", errors="ignore"))
    except Exception:
        return 0


def main():
    out_path = os.environ.get("AGENT_PROFILE_JSONL")
    if not out_path:
        return 0
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except Exception as exc:
        payload = {"_parse_error": repr(exc), "_raw": raw[:4096]}
    event = (
        payload.get("hook_event_name")
        or payload.get("event")
        or payload.get("type")
        or "unknown"
    )
    tool_input = payload.get("tool_input") or {}
    tool_response = payload.get("tool_response") or {}
    command = None
    if isinstance(tool_input, dict):
        command = tool_input.get("command") or tool_input.get("cmd")
    row = {
        "ts_ns": time.time_ns(),
        "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "event": event,
        "tool_name": payload.get("tool_name") or payload.get("tool") or None,
        "tool_use_id": payload.get("tool_use_id") or payload.get("id") or None,
        "command": command,
        "exit_code": tool_response.get("exit_code") if isinstance(tool_response, dict) else None,
        "stdin_bytes": safe_len(raw),
        "tool_input_bytes": safe_len(tool_input),
        "tool_response_bytes": safe_len(tool_response),
        "payload_keys": sorted(payload.keys()) if isinstance(payload, dict) else [],
    }
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
