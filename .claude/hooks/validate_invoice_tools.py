#!/usr/bin/env python3
"""Hook helper: every tool in TOOLS must have an elif branch in _execute_tool."""

import json
import re
import sys

src = open(sys.argv[1]).read()

# Locate the TOOLS list using bracket-depth counting. Allow an optional type
# annotation, e.g. `TOOLS: list[dict] = [`. Exit quietly if there's no TOOLS
# list in this file so the hook can never crash on an unrelated edit.
m = re.search(r"^TOOLS\b[^\n=]*=\s*\[", src, re.MULTILINE)
if m is None:
    sys.exit(0)
ts = m.start()
depth = 0
pos = m.end()
te = pos
for i, c in enumerate(src[pos:]):
    if c == "[":
        depth += 1
    elif c == "]":
        if depth == 0:
            te = pos + i
            break
        depth -= 1

names = set(re.findall(r'"name":\s*"([^"]+)"', src[ts:te]))

fn = src.index("async def _execute_tool(")
handlers = set(re.findall(r'(?:if|elif) name == "([^"]+)"', src[fn:]))

missing = sorted(names - handlers)
if missing:
    print(
        json.dumps(
            {"systemMessage": f"⚠️ Tools missing handlers in _execute_tool: {', '.join(missing)}"}
        )
    )
