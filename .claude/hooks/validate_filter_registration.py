#!/usr/bin/env python3
"""Hook helper: every GigFilter subclass in filters.py must be registered in main.py."""

import json
import re
from pathlib import Path

proj_root = Path(__file__).parent.parent.parent  # .claude/hooks/ -> project root

filters_src = (proj_root / "organist_bot" / "filters.py").read_text()
main_src = (proj_root / "main.py").read_text()

filter_classes = set(re.findall(r"^class (\w+Filter)\(GigFilter\)", filters_src, re.MULTILINE))
# Both pre_filter.add() and filter_chain.add() count as registered
registered = set(re.findall(r"\.add\((\w+Filter)", main_src))

missing = sorted(filter_classes - registered)
if missing:
    print(
        json.dumps({"systemMessage": f"⚠️ Filters not registered in main.py: {', '.join(missing)}"})
    )
