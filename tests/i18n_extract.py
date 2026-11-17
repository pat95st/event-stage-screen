"""Extract translatable English source strings from templates, JS and Python.

Used by tests/test_i18n_web.py to ensure the German catalog is complete.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "stagescreen"
TEMPLATE_RE = re.compile(r"""_\(\s*(["'])(.+?)(?<!\\)\1\s*[,)]""")
JS_RE = re.compile(r"""(?:\bT|ESS\.t)\(\s*(["'`])(.+?)(?<!\\)\1\s*[,)]""")
PY_CALLS = {"Msg", "DomainError", "NotFound", "t"}


def template_strings() -> set[str]:
    found = set()
    for path in (ROOT / "web" / "templates").glob("*.html"):
        text = path.read_text(encoding="utf-8")
        found |= {m.group(2) for m in TEMPLATE_RE.finditer(text)}
        found |= {m.group(2).replace('\\"', '"') for m in JS_RE.finditer(text)}
    for path in (ROOT / "web" / "static" / "js").glob("*.js"):
        found |= {m.group(2) for m in JS_RE.finditer(path.read_text(encoding="utf-8"))}
    return found


def python_strings() -> set[str]:
    found = set()
    for path in ROOT.rglob("*.py"):
        if "i18n" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                if name in PY_CALLS and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                    found.add(node.args[0].value)
            # page titles: page("x.html", "active", "Title") and render(..., title="...")
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "page" and len(node.args) == 3:
                found.add(node.args[2].value)
            if isinstance(node, ast.keyword) and node.arg == "title" and isinstance(node.value, ast.Constant):
                found.add(node.value.value)
    from stagescreen.infrastructure.system import THROTTLE_BITS

    found |= set(THROTTLE_BITS.values())
    return found


def all_strings() -> set[str]:
    return {s for s in template_strings() | python_strings() if s.strip()}


if __name__ == "__main__":
    for s in sorted(all_strings(), key=str.lower):
        print(repr(s))
