#!/usr/bin/env python3
"""PreToolUse hook: always ask the user before the migration writes anything.

Antigravity sends the proposed tool call as JSON on stdin and reads a decision
from stdout (see https://antigravity.google/docs/hooks). For `apply.py --apply`
this returns "force_ask", which prompts even when the command was previously
allowed. Every other command gets an empty object, so normal permission
handling is unchanged.
"""
import json
import re
import sys

try:
    call = json.load(sys.stdin).get("toolCall") or {}
except ValueError:
    call = {}
cmd = str((call.get("args") or {}).get("CommandLine", ""))
if call.get("name") == "run_command" and re.search(r"apply\.py\b", cmd) and re.search(r"(^|\s)--apply(\s|$)", cmd):
    print(json.dumps({
        "decision": "force_ask",
        "reason": "claude-migrate-to-agy is about to write Antigravity config from the migration plan. "
                  "Approve only if you have reviewed the plan.",
    }))
else:
    print("{}")
