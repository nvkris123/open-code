#!/usr/bin/env python3
"""Verify a brain against the new layout, using the Markdown server's own
wikilink resolution order (exact path -> walk-up -> basename fallback)."""
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

CONTENT = Path("/Users/vinay/Documents/docserver").resolve()
BRAINS = ["jovialiki", "stackav", "vinay2"]
WIKILINK_RE = re.compile(r"!?\[\[([^\]|#]+)(?:#([^\]|]+))?(?:\|([^\]]+))?\]\]")
ALLOWED_DIRS = {"people", "sources", "desk", "desk/archived", "assets", "assets/people"}


def build_index():
    by_path, by_base = {}, defaultdict(list)
    def walk(d, prefix, depth=0):
        if depth > 25:
            return
        try:
            with os.scandir(d) as ents:
                vis = [e for e in ents if not e.name.startswith(".")]
        except OSError:
            return
        for e in vis:
            rel = f"{prefix}/{e.name}" if prefix else e.name
            if e.is_dir(follow_symlinks=True):
                walk(Path(e.path), rel, depth + 1)
            elif e.is_file() and e.name.endswith(".md"):
                by_path[rel[:-3]] = rel
                by_base[e.name[:-3]].append(rel)
    walk(CONTENT, "")
    for m in by_base.values():
        m.sort(key=str.lower)
    return by_path, by_base


def resolve(t, cur, by_path, by_base):
    if t in by_path:
        return by_path[t], "exact"
    d = cur
    while True:
        c = f"{d}/{t}" if d else t
        if c in by_path:
            return by_path[c], "walkup"
        if not d:
            break
        s = d.rfind("/")
        d = d[:s] if s >= 0 else ""
    if "/" not in t:
        m = by_base.get(t)
        if m:
            if len(m) == 1:
                return m[0], "basename"
            cp = cur.split("/") if cur else []
            def sc(p):
                pt = p.split("/"); k = 0
                while k < len(pt) - 1 and k < len(cp) and pt[k] == cp[k]:
                    k += 1
                return -k
            return min(m, key=sc), "basename"
    return None, "broken"


def check(brain, by_path, by_base, verbose=False):
    wiki = Path(f"brains/{brain}/wiki")
    full = CONTENT / wiki
    md = sorted(full.rglob("*.md"))
    out = {}

    broken, broken_log, leaks, bad_form = [], [], [], []
    for p in md:
        rel = (wiki / p.relative_to(full)).as_posix()
        cur = rel.rsplit("/", 1)[0]
        text = p.read_text(encoding="utf-8", errors="replace")
        for m in WIKILINK_RE.finditer(text):
            t = m.group(1).strip()
            r, how = resolve(t, cur, by_path, by_base)
            if r is None:
                (broken_log if p.name == "log.md" else broken).append((rel, t))
            elif not r.startswith(f"brains/{brain}/"):
                leaks.append((rel, t, r))
            if m.group(0).startswith("!["):
                bad_form.append((rel, t))

    out["files"] = len(md)
    out["broken (non-log)"] = len(broken)
    out["broken (log.md, informational)"] = len(broken_log)
    out["cross-brain leaks"] = len(leaks)
    out["transclusions ![[ ]]"] = len(bad_form)

    dupes = {b: v for b, v in
             ((b, [x for x in v if x.startswith(f"brains/{brain}/wiki/")])
              for b, v in by_base.items()) if len(v) > 1}
    out["duplicate basenames"] = len(dupes)

    dirs = set()
    for p in full.rglob("*"):
        if p.is_dir():
            dirs.add(p.relative_to(full).as_posix())
    stray = sorted(d for d in dirs if d not in ALLOWED_DIRS)
    out["stray directories"] = len(stray)

    type_fields = sum(1 for p in md
                      if re.search(r"^type:\s", p.read_text(encoding="utf-8", errors="replace"),
                                   re.M))
    out["pages with type: field"] = type_fields

    print(f"\n--- {brain} ---")
    for k, v in out.items():
        flag = "" if v == 0 or k == "files" or "informational" in k else "   <-- "
        print(f"  {k:36} {v:5d}{flag}")
    if stray:
        print(f"     stray dirs: {stray}")
    if dupes and verbose:
        for b, v in list(dupes.items())[:10]:
            print(f"     dup basename: {b} -> {[x.split('/wiki/')[1] for x in v]}")
    if verbose:
        for rel, t in broken[:15]:
            print(f"     BROKEN [[{t}]] in {rel.split('/wiki/')[1]}")
        for rel, t, r in leaks[:10]:
            print(f"     LEAK   [[{t}]] in {rel.split('/wiki/')[1]} -> {r}")
    return out


if __name__ == "__main__":
    verbose = "-v" in sys.argv
    bp, bb = build_index()
    print(f"indexed {len(bp)} markdown files under {CONTENT}")
    for b in BRAINS:
        check(b, bp, bb, verbose)
