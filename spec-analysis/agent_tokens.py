#!/usr/bin/env python3
"""Sum agent-side token usage from codex --json event streams, per task."""
import json, sys
from pathlib import Path
FIELDS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
          "output_tokens", "reasoning_output_tokens")
def walk(o, acc):
    if isinstance(o, dict):
        for k, v in o.items():
            if k in FIELDS and isinstance(v, int):
                acc[k] = acc.get(k, 0) + v
            else:
                walk(v, acc)
    elif isinstance(o, list):
        for v in o: walk(v, acc)
grand = {}
for f in sorted(Path(sys.argv[1]).glob("*/*/stdout.jsonl")):
    acc = {}
    for ln in f.read_text(errors="replace").splitlines():
        try: walk(json.loads(ln), acc)
        except json.JSONDecodeError: pass
    fresh = acc.get("input_tokens", 0) + acc.get("output_tokens", 0) \
        + acc.get("reasoning_output_tokens", 0)
    print(f"{f.parent.parent.name}/{f.parent.name:35} fresh={fresh:>9,} "
          f"cached_in={acc.get('cached_input_tokens',0):>10,}")
    for k, v in acc.items(): grand[k] = grand.get(k, 0) + v
fresh = grand.get("input_tokens",0)+grand.get("output_tokens",0)+grand.get("reasoning_output_tokens",0)
print(f"\nAGENT TOTAL fresh={fresh:,}  cached_input={grand.get('cached_input_tokens',0):,}  "
      f"cache_writes={grand.get('cache_write_input_tokens',0):,}")
print(f"speculation added ~603k fresh-ish tokens -> added/agent-fresh ratio = {603000/max(fresh,1):.2f}x")
