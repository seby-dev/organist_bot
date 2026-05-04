#!/usr/bin/env python3
"""Hook helper: every tool in TOOLS must have an elif branch in _execute_tool."""

import json
import re
import sys

src = open(sys.argv[1]).read()

# Locate TOOLS = [...] using bracket-depth counting
tools_marker = "TOOLS = ["
ts = src.index(tools_marker)
depth = 0
pos = ts + len(tools_marker)
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
