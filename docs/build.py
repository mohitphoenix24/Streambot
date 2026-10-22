#!/usr/bin/env python3
"""Build the Streambot Guide: bundle content/**.md into assets/js/content.js.

    python3 docs/build.py              # build, then run every runnable example to verify it
    python3 docs/build.py --no-verify  # build only
    python3 docs/build.py --watch      # rebuild whenever a lesson or a quoted source file changes

Lessons quote the REAL app code, so the guide cannot drift from it:

    ```python src="AI/app/chat/history.py" fn="build_history,History"
    ```                                  <- body is replaced by those definitions, found by name

    ```ts src="frontend/src/cite.ts" fn="linkCitations"
    ```

    ```python run include="AI/app/chat/history.py:approx_tokens,_eligible|AI/app/chat/memory.py:KEEP_RECENT" level=easy
    print(approx_tokens("hello world"))     <- a runnable demo; the included real code is put in front
    ```

Attributes: `src` + `fn` (comma separated names; Python: functions, classes, `Class.method`, UPPER constants;
TS/TSX: function, const, interface, type, class), or `src` + `from="text" to="text"` (the first line containing
`from` through the next line containing `to`, for code nested inside a function), or `src` + `lines="10-30"`,
or `src` alone for the whole file.
If a name or file is missing the build FAILS, which is the point.
"""
from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

DOCS = Path(__file__).resolve().parent
REPO = DOCS.parent
CONTENT = DOCS / "content"
OUT = DOCS / "assets" / "js" / "content.js"

FENCE_OPEN = re.compile(r"^(`{3,})([^\n`]*)$")
ATTR = re.compile(r"""(\w+)=(?:"([^"]*)"|'([^']*)'|(\S+))""")
PY_HEADER = "from __future__ import annotations\nimport json, logging, re, time\nfrom collections import deque\nfrom dataclasses import dataclass, field\nfrom decimal import Decimal\nfrom typing import Any, Callable\nfrom urllib.parse import urlparse\n"


class BuildError(SystemExit):
    pass


def fail(msg: str):
    raise BuildError(f"\nBUILD ERROR: {msg}")


# ── front matter ─────────────────────────────────────────────────────────
def parse_front_matter(text: str):
    meta = {}
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            for line in text[3:end].strip().splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip()] = v.strip().strip('"')
            text = text[end + 4:].lstrip("\n")
    return meta, text


def strip_number(name: str) -> str:
    return re.sub(r"^\d+[-_]", "", name)


# ── quoting real source ──────────────────────────────────────────────────
def read_source(rel: str) -> str:
    p = REPO / rel
    if not p.is_file():
        fail(f"quoted file does not exist: {rel}")
    return p.read_text(encoding="utf-8")


def py_extract(rel: str, names: list[str]) -> str:
    src = read_source(rel)
    tree = ast.parse(src)
    lines = src.splitlines()

    def with_comments(start: int, end: int) -> str:  # 1-based inclusive; pulls in the comment lines right above
        i = start - 1
        while i > 0 and lines[i - 1].lstrip().startswith("#"):
            i -= 1
        return "\n".join(lines[i:end])

    def find(body, name):
        for n in body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == name:
                return n
            if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in n.targets):
                return n
            if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.target.id == name:
                return n
        return None

    parts = []
    for name in names:
        node, dedent = None, False
        if "." in name:
            cls_name, meth = name.split(".", 1)
            cls = find(tree.body, cls_name)
            node = find(cls.body, meth) if isinstance(cls, ast.ClassDef) else None
            dedent = True
        else:
            node = find(tree.body, name)
        if node is None:
            fail(f"{rel}: no definition named '{name}'")
        start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
        text = with_comments(start, node.end_lineno)
        parts.append(textwrap.dedent(text) if dedent else text)
    return "\n\n".join(parts)


def ts_extract(rel: str, names: list[str]) -> str:
    lines = read_source(rel).splitlines()
    parts = []
    for name in names:
        pat = re.compile(rf"^(export\s+)?(default\s+)?(async\s+)?(function|const|let|class|interface|type)\s+{re.escape(name)}\b")
        start = next((i for i, l in enumerate(lines) if pat.match(l)), None)
        if start is None:
            fail(f"{rel}: no declaration named '{name}'")
        indent = len(lines[start]) - len(lines[start].lstrip())
        first = lines[start].rstrip()
        end = start
        if not first.endswith(";") or first.count("{") != first.count("}"):
            for j in range(start + 1, len(lines)):
                s = lines[j]
                ind = len(s) - len(s.lstrip())
                if s.strip() and ind <= indent and s.strip()[0] in "})]":
                    end = j
                    break
                if first.rstrip().endswith("=>") and s.rstrip().endswith(";") and ind > indent:
                    end = j
                    break
            else:
                fail(f"{rel}: could not find the end of '{name}'")
        top = start
        while top > 0 and (lines[top - 1].lstrip().startswith(("//", "/*", "*", "*/"))):
            top -= 1
        parts.append("\n".join(lines[top:end + 1]))
    return "\n\n".join(parts)


def extract(rel: str, fn: str | None, line_range: str | None, frm: str | None = None, to: str | None = None) -> str:
    if frm:
        lines = read_source(rel).splitlines()
        a = next((i for i, l in enumerate(lines) if frm in l), None)
        if a is None:
            fail(f"{rel}: no line contains from=\"{frm}\"")
        b = next((i for i in range(a, len(lines)) if to and to in lines[i]), None)
        if b is None:
            fail(f"{rel}: no line at or after from=\"{frm}\" contains to=\"{to}\"")
        return textwrap.dedent("\n".join(lines[a:b + 1]))
    if line_range:
        a, b = (int(x) for x in line_range.split("-"))
        return "\n".join(read_source(rel).splitlines()[a - 1:b])
    if not fn:
        return read_source(rel).rstrip("\n")
    names = [n.strip() for n in fn.split(",") if n.strip()]
    return (py_extract if rel.endswith(".py") else ts_extract)(rel, names)


def build_include(spec: str) -> str:
    """`path:name,name|path:name` -> the real definitions, joined (Python only)."""
    chunks = []
    for item in spec.split("|"):
        rel, names = item.split(":", 1)
        chunks.append(f"# --- from {rel.strip()} ---\n" + py_extract(rel.strip(), [n.strip() for n in names.split(",")]))
    return "\n\n".join(chunks)


# ── lesson processing ────────────────────────────────────────────────────
def process_body(body: str, lesson: str, runnable: list):
    out, i, lines = [], 0, body.split("\n")
    quoted = 0
    while i < len(lines):
        m = FENCE_OPEN.match(lines[i])
        if not m:
            out.append(lines[i])
            i += 1
            continue
        ticks, info = m.group(1), m.group(2).strip()
        j = i + 1
        while j < len(lines) and lines[j].strip() != ticks:
            j += 1
        if j >= len(lines):
            fail(f"{lesson}: unclosed code fence starting at: {lines[i]}")
        code = "\n".join(lines[i + 1:j])
        attrs = {k: next(v for v in (a, b, c) if v) if (a or b or c) else "" for k, a, b, c in ATTR.findall(info)}
        bare = ATTR.sub(" ", info).split()  # the words that are NOT key=value pairs (lang, run, local, ...)
        lang = bare[0] if bare else ""
        words = bare[1:]

        if "src" in attrs or "include" in attrs:
            if "src" in attrs:
                real = extract(attrs["src"], attrs.get("fn"), attrs.get("lines"), attrs.get("from"), attrs.get("to"))
                code = real if not code.strip() else real + "\n\n" + code
                title = attrs.get("title") or (attrs["src"] + (f": {attrs['fn']}" if attrs.get("fn") else ""))
                quoted += 1
            else:
                real = build_include(attrs["include"])
                header = PY_HEADER if lang == "python" else ""
                banner_top = "# =====  The app's real code, quoted from the repository  ====="
                banner_demo = "# =====  Your turn: edit anything below, then press Run  ====="
                code = header + "\n" + banner_top + "\n" + real + "\n\n\n" + banner_demo + "\n" + code
                title = attrs.get("title", "")
                quoted += 1
            meta = [lang] + words
            if title:
                meta.append(f'title="{title}"')
            if "level" in attrs:
                meta.append(f"level={attrs['level']}")
            info = " ".join(meta)
        if lang == "python" and "run" in words:
            runnable.append((lesson, code))
        out.append(f"{ticks}{info}")
        out.append(code)
        out.append(ticks)
        i = j + 1
    return "\n".join(out), quoted


def build(verify: bool = True):
    modules, ids, runnable, quoted_total = [], set(), [], 0
    for mdir in sorted(p for p in CONTENT.iterdir() if p.is_dir()):
        mod_file = mdir / "_module.md"
        if not mod_file.exists():
            continue
        meta, desc = parse_front_matter(mod_file.read_text(encoding="utf-8"))
        module = {
            "id": strip_number(mdir.name), "title": meta.get("title", mdir.name), "icon": meta.get("icon", "book"),
            "level": meta.get("level", "mixed"), "kind": meta.get("kind", "learn"),
            "description": meta.get("description", desc.strip()), "lessons": [],
        }
        for f in sorted(mdir.glob("*.md")):
            if f.name.startswith("_"):
                continue
            lmeta, body = parse_front_matter(f.read_text(encoding="utf-8"))
            lid = strip_number(f.stem)
            if lid in ids:
                fail(f"duplicate lesson id '{lid}' ({f})")
            ids.add(lid)
            body, q = process_body(body, lid, runnable)
            quoted_total += q
            lesson = {"id": lid, "title": lmeta.get("title", lid), "body": body}
            for k in ("level", "time", "summary", "tags", "icon", "cover"):
                if k in lmeta:
                    lesson[k] = lmeta[k]
            module["lessons"].append(lesson)
        modules.append(module)

    payload = json.dumps({"modules": modules, "built": time.strftime("%Y-%m-%d %H:%M")}, ensure_ascii=False)
    OUT.write_text(f"/* generated by build.py: edit the Markdown in content/ instead */\nwindow.COURSE = {payload};\n", encoding="utf-8")
    n = sum(len(m["lessons"]) for m in modules)
    print(f"built {OUT.relative_to(DOCS)}: {len(modules)} modules, {n} lessons, {quoted_total} quoted code blocks, "
          f"{len(runnable)} runnable examples, {len(payload) // 1024} KB")
    if verify:
        verify_examples(runnable)
    return modules


def verify_examples(runnable):
    """Run every ```python run example with the real interpreter. A broken demo fails the build."""
    bad = 0
    for lesson, code in runnable:
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "demo.py"
            script.write_text(code, encoding="utf-8")
            r = subprocess.run([sys.executable, "-I", str(script)], capture_output=True, text=True, timeout=30, cwd=tmp)
        if r.returncode != 0:
            bad += 1
            print(f"  ✗ {lesson}: example failed\n{textwrap.indent(r.stderr.strip()[-600:], '      ')}")
    if bad:
        fail(f"{bad} runnable example(s) failed")
    print(f"verified {len(runnable)} runnable examples with {sys.version.split()[0]}")


if __name__ == "__main__":
    build(verify="--no-verify" not in sys.argv)
    if "--watch" in sys.argv:
        def stamp():
            files = list(CONTENT.rglob("*.md")) + list((REPO / "AI" / "app").rglob("*.py")) + list((REPO / "frontend" / "src").rglob("*.ts*"))
            return max(p.stat().st_mtime for p in files)
        last = stamp()
        while True:
            time.sleep(1)
            s = stamp()
            if s != last:
                last = s
                try:
                    build(verify=False)
                except BuildError as e:
                    print(e)
