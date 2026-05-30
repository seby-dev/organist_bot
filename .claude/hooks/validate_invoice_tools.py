#!/usr/bin/env python3
"""Hook helper: every tool in TOOLS must have a registered dispatch handler."""

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

# A tool is "handled" if it is registered via the @_handler("name") dispatch
# decorator, a direct _TOOL_HANDLERS["name"] = ... assignment, or the legacy
# `if name == "name"` chain — accept all so the hook works before and after the
# dispatch-dict refactor.
handlers = (
    set(re.findall(r'@_handler\(\s*"([^"]+)"\s*\)', src))
    | set(re.findall(r'_TOOL_HANDLERS\[\s*"([^"]+)"\s*\]', src))
    | set(re.findall(r'(?:if|elif) name == "([^"]+)"', src))
)

missing = sorted(names - handlers)
if missing:
    print(json.dumps({"systemMessage": f"⚠️ Tools missing dispatch handlers: {', '.join(missing)}"}))
