#!/usr/bin/env python3
"""A fake `agy` for the brand-suggest tests and acceptance runs. Never calls a
network or a model: it answers from canned data and records how it was called.

Environment:
  FAKE_AGY_DIR   a folder. `answers.json` there maps a heard name to
                 {"answer": {...}, "queries": [...], "model": str,
                  "fail_first": n, "fail": "rc"|"quota"|"bad_output"}.
                 A name with no entry gets a low-confidence "not a brand"
                 answer. Every call appends one line to `calls.jsonl`
                 (argv, cwd, the entries of cwd, the --json-schema file).
Output: the stream-json events agy prints (init, step updates, result).
"""
import json
import os
import re
import sys
from pathlib import Path

UNSURE = {"is_brand": False, "company": None, "heard_is_exact": False, "confidence": 0.3,
          "official_domain": None, "reason": "nothing clear"}


def main(argv):
    root = Path(os.environ["FAKE_AGY_DIR"])
    args = argv[1:]
    prompt = next((a[len("-p="):] for a in args if a.startswith("-p=")), "")
    schema_path = args[args.index("--json-schema") + 1] if "--json-schema" in args else None
    name = (re.search(r'heard the name "(.*?)" in podcast ad reads', prompt) or [None, ""])[1]
    entry = (json.loads((root / "answers.json").read_text()) if (root / "answers.json").exists()
             else {}).get(name, {})
    with open(root / "calls.jsonl", "a") as fh:
        fh.write(json.dumps({
            "argv": argv, "cwd": os.getcwd(), "cwd_entries": sorted(os.listdir(".")),
            "name": name, "prompt": prompt,
            "schema": json.loads(Path(schema_path).read_text()) if schema_path else None,
            "schema_path": schema_path}) + "\n")
    counter = root / ("n-" + re.sub(r"[^a-z0-9]+", "_", name.lower()))
    seen = int(counter.read_text()) if counter.exists() else 0
    counter.write_text(str(seen + 1))
    fail = entry.get("fail")
    if fail and seen < entry.get("fail_first", 10 ** 9):
        if fail == "quota":
            sys.stderr.write("429 RESOURCE_EXHAUSTED: quota\n")
            return 1
        if fail == "rc":
            sys.stderr.write("agy exploded\n")
            return 2
    asked = args[args.index("--model") + 1] if "--model" in args else "fake-model-1"
    model = entry.get("model", asked)
    out = {"event": "init", "conversation_id": "c1", "init": {"model": model, "cwd": os.getcwd()}}
    print(json.dumps(out))
    for i, q in enumerate(entry.get("queries", [name]), 2):
        step = {"conversation_id": "c1", "step_index": i, "step_type": "tool",
                "tool_name": "search_web",
                "tool_info": {"name": "search_web", "parameters": {"query": q}}}
        print(json.dumps({"event": "step_update", "step_update": dict(step, state="ACTIVE")}))
        print(json.dumps({"event": "step_update", "step_update": dict(step, state="DONE")}))
    print(json.dumps({"event": "step_update", "step_update": {
        "step_index": 9, "state": "DONE", "step_type": "finish"}}))
    answer = entry.get("answer", UNSURE)
    if fail == "bad_output":
        answer = {"is_brand": True}
    usage = {"input_tokens": 1000, "output_tokens": 50, "thinking_tokens": 0,
             "cache_read_tokens": 10, "total_tokens": 1050}
    print(json.dumps({"event": "result", "result": {
        "conversation_id": "c1", "status": "SUCCESS", "response": json.dumps(answer),
        "duration_seconds": 1.5, "num_turns": 1, "structured_output": answer, "usage": usage}}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
