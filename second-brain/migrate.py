#!/usr/bin/env python3
"""
Second-brain migration: old folder-per-type layout -> new layout.

    wiki/                     wiki/
      sources/       ->         sources/            (unchanged)
      people/        ->         people/             (+ stackav profiles merged in)
      entities/      ->         people/ or <root>   (split: humans vs everything else)
      concepts/      ->         <root>
      topics/        ->         <root> or desk/
      synthesis/     ->         <root> or desk/ or sources/
      profiles/      ->         merged into people/
      archived/      ->         desk/archived/
      */pics|pictures ->        assets/people/

Rewrites every [[wikilink]] to match, strips the now-redundant `type:` frontmatter
field, and moves files with `git mv` so history follows.

Usage:  migrate.py [--apply] [--brain NAME]
Default is a dry run.
"""
import argparse
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path("/Users/vinay/Documents/brains")

# ---------------------------------------------------------------------------
# Judgment calls. Everything not listed here follows the mechanical default for
# its source folder (concepts/ + topics/ + synthesis/ -> wiki root).
# ---------------------------------------------------------------------------

# entities/ pages that are humans -> people/
PEOPLE = {
    "stackav": {
        "al-costa", "alex-church", "bernardine-dias", "bob-hansen", "bob-hyer",
        "brett-browning", "bryan-salesky", "colin", "cynthia-kwon", "dillon-collins",
        "dj-hoff", "glenn-mcgill", "jacob-manning", "jason-ziglar", "jenn-gustin",
        "john-rich", "me", "melvin-walls", "michael-surge-kirkman", "mike-deasy",
        "mike-sebetich", "nick-meyer", "niels-joubert", "noe-brito", "patrick-roberts",
        "peter-rander", "randall-nortman", "rich-mattes", "steve-schwartz",
        "steve-taylor", "steven-bilitzky", "sydney-career-coach", "tariq-ahamed",
        "tony-poerio", "waldo-perez-regalado",
    },
    "vinay2": {
        "ayden", "jody", "kurt-keutzer", "pradeep-bisht", "rob-kandell", "sofia",
        "surbhi", "sydney", "val-miftakhov", "vinay",
    },
    "jovialiki": set(),  # already has a people/ folder; entities/ holds no humans
}

# pages that have a status or an expiry -> desk/
DESK = {
    "stackav": {
        "topics/manifest-based-deploy",      # "primary deliverable for the rest of Q3"
        "synthesis/daily-briefings",         # recurring morning ritual
        "synthesis/odm-outreach-plan",       # a plan
        "synthesis/onboard-infra-team-connect",  # who to meet, actionable
        "synthesis/relationship-tracker",    # operational instrument
    },
    "jovialiki": {
        "topics/family-trip-2028",           # "parked, long-term", dated
        "entities/reminders",                # standing reminders with dates
    },
    "vinay2": set(),
}

# pages that are really a reading of an external thing -> sources/
TO_SOURCES = {
    "stackav": {"synthesis/stack-av-people-linkedin-research"},
    "jovialiki": set(),
    "vinay2": set(),
}

# explicit renames (basename collisions). old wiki-rel -> new basename
RENAMES = {
    "stackav": {
        # 572-line writeup keeps the canonical name at root; the 66-line source page
        # takes the dated form used by every other stackav source page.
        "sources/clockwork-vs-allison-stack": "clockwork-vs-allison-stack-2026-05-01",
    },
    "jovialiki": {},
    "vinay2": {},
}

# stackav only: profiles/X.md is a relationship dossier companion to entities/X.md.
# Merge the dossier into the person page as a section, then drop the profile file.
MERGE_PROFILES = "stackav"

BRAINS = ["jovialiki", "stackav", "vinay2"]

# brain-root files that belong in the wiki as desk pages
ROOT_FILES_TO_DESK = {
    "stackav": {"MORNING-BRIEFING.md": "desk/morning-briefing.md"},
}

PIC_DIRS = ["people/pics", "profiles/pictures"]
LEGACY_DIRS = ["entities", "concepts", "topics", "synthesis", "profiles", "archived"]

WIKILINK_RE = re.compile(r"(!?)\[\[([^\]|#]+)(#[^\]|]+)?(\|[^\]]+)?\]\]")


# ---------------------------------------------------------------------------

def sh(cmd, cwd):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


def wiki_md(brain):
    return sorted((ROOT / brain / "wiki").rglob("*.md"))


def build_moves(brain):
    """Return {old_wiki_rel_path: new_wiki_rel_path} for .md files, plus asset moves."""
    wiki = ROOT / brain / "wiki"
    moves, assets = {}, {}
    desk, to_src = DESK[brain], TO_SOURCES[brain]
    renames, people = RENAMES[brain], PEOPLE[brain]

    for p in wiki.rglob("*.md"):
        rel = p.relative_to(wiki).as_posix()
        stem = rel[:-3]
        folder = rel.split("/")[0] if "/" in rel else ""
        base = p.stem

        if rel in ("index.md", "log.md"):
            continue
        if folder in ("sources", "people") and stem not in renames:
            continue                                    # already in place
        if folder == "assets":
            continue

        new_base = renames.get(stem, base)

        if stem in renames and folder == "sources":
            dest = f"sources/{new_base}.md"
        elif stem in desk:
            dest = f"desk/{new_base}.md"
        elif stem in to_src:
            dest = f"sources/{new_base}.md"
        elif folder == "archived":
            dest = f"desk/archived/{new_base}.md"
        elif folder == "profiles":
            continue                                    # handled by the merge step
        elif folder == "entities":
            dest = f"people/{new_base}.md" if base in people else f"{new_base}.md"
        elif folder in ("concepts", "topics", "synthesis"):
            dest = f"{new_base}.md"
        else:
            continue

        if rel != dest:
            moves[rel] = dest

    # non-markdown: pictures -> assets/people, other loose files -> assets/
    for d in PIC_DIRS:
        src = wiki / d
        if src.is_dir():
            for f in src.iterdir():
                if f.is_file() and not f.name.startswith("."):
                    assets[f"{d}/{f.name}"] = f"assets/people/{f.name}"
    for folder in LEGACY_DIRS:
        src = wiki / folder
        if src.is_dir():
            for f in src.iterdir():
                if f.is_file() and not f.name.endswith(".md") and not f.name.startswith("."):
                    assets[f"{folder}/{f.name}"] = f"assets/{f.name}"

    return moves, assets


def link_target_map(brain, moves):
    """old link target -> new link target, for both prefixed and bare forms."""
    wiki = ROOT / brain / "wiki"
    basenames = defaultdict(list)
    for p in wiki.rglob("*.md"):
        basenames[p.stem].append(p.relative_to(wiki).as_posix())

    tmap = {}
    for old, new in moves.items():
        old_t, new_t = old[:-3], new[:-3]
        tmap[old_t] = new_t
        if len(basenames[Path(old).stem]) == 1:          # bare form is unambiguous
            tmap[Path(old).stem] = new_t

    # profiles/X -> people/X (merged away)
    if brain == MERGE_PROFILES:
        for p in (wiki / "profiles").glob("*.md"):
            tmap[f"profiles/{p.stem}"] = f"people/{p.stem}"

    # pages that stay put but whose links were written with a folder prefix that
    # is now the file's real location need no change; pages that move to root lose
    # their prefix, which the loop above already handles.
    return tmap


def rewrite_links(text, tmap):
    def sub(m):
        bang, target, heading, alias = m.group(1), m.group(2).strip(), m.group(3) or "", m.group(4) or ""
        new = tmap.get(target)
        if new is None:
            return m.group(0)
        return f"{bang}[[{new}{heading}{alias}]]"
    return WIKILINK_RE.sub(sub, text)


def rewrite_images(text, in_people_dir):
    """pics/x.jpg and pictures/x.jpg -> ../assets/people/x.jpg"""
    if not in_people_dir:
        return text
    text = re.sub(r"\]\(\s*pics/([^)\s]+)\s*\)", r"](../assets/people/\1)", text)
    text = re.sub(r"\]\(\s*pictures/([^)\s]+)\s*\)", r"](../assets/people/\1)", text)
    return text


def rewrite_frontmatter_sources(text, brain, tmap):
    """`sources:` in frontmatter is a slug list, not a wikilink, so the link regex
    misses it. Rewrite any entry that names a page we moved.

    Only path-qualified entries (containing "/") are remapped, plus bare slugs that
    exactly match a renamed SOURCE page. A bare slug in `sources:` always names a
    source, and sources don't move — remapping bare slugs generally would risk
    colliding with a same-named subject page."""
    if not text.startswith("---\n"):
        return text
    end = text.find("\n---", 4)
    if end == -1:
        return text
    fm, rest = text[4:end], text[end:]
    bare_ren = {old.split("/")[-1]: new for old, new in RENAMES[brain].items()
                if old.startswith("sources/")}

    def remap(entry):
        e = entry.strip()
        if "/" in e:
            return tmap.get(e, e)
        return bare_ren.get(e, e)

    out, in_sources = [], False
    for ln in fm.split("\n"):
        inline = re.match(r"^(\s*sources:\s*)\[(.*)\]\s*$", ln)
        if inline:
            items = [remap(i) for i in inline.group(2).split(",") if i.strip()]
            out.append(f"{inline.group(1)}[{', '.join(items)}]")
            in_sources = False
            continue
        if re.match(r"^sources:\s*$", ln):
            in_sources = True
            out.append(ln)
            continue
        if in_sources:
            item = re.match(r"^(\s*-\s*)(\S+)\s*$", ln)
            if item:
                out.append(item.group(1) + remap(item.group(2)))
                continue
            if ln.strip() and not ln.startswith((" ", "\t", "-")):
                in_sources = False
        out.append(ln)
    return "---\n" + "\n".join(out) + rest


def strip_type_field(text):
    if not text.startswith("---\n"):
        return text
    end = text.find("\n---", 4)
    if end == -1:
        return text
    fm, rest = text[4:end], text[end:]
    fm = "\n".join(l for l in fm.split("\n") if not re.match(r"^type:\s", l))
    return "---\n" + fm + rest


def merge_profiles(brain, apply):
    """Fold profiles/X.md into people-bound entities/X.md as a section."""
    wiki = ROOT / brain / "wiki"
    pdir = wiki / "profiles"
    merged = []
    if not pdir.is_dir():
        return merged
    for prof in sorted(pdir.glob("*.md")):
        ent = wiki / "entities" / prof.name
        if not ent.exists():
            print(f"  !! profiles/{prof.name} has no entities/ counterpart — leaving alone")
            continue
        ptext = prof.read_text(encoding="utf-8")
        body = ptext
        if body.startswith("---\n"):
            e = body.find("\n---", 4)
            body = body[e + 4:] if e != -1 else body
        body = re.sub(r"^\s*#\s+[^\n]*\n", "", body.lstrip("\n"), count=1)
        body = rewrite_images(body, True).strip()

        etext = ent.read_text(encoding="utf-8").rstrip()
        new = etext + "\n\n## Relationship dossier\n\n" + body + "\n"
        merged.append(prof.name)
        if apply:
            ent.write_text(new, encoding="utf-8")
    return merged


def run(brain, apply):
    wiki = ROOT / brain / "wiki"
    repo = ROOT / brain
    print(f"\n{'='*70}\n{brain}\n{'='*70}")

    print("\n-- merge stackav profile dossiers --" if brain == MERGE_PROFILES else "", end="")
    merged = merge_profiles(brain, apply) if brain == MERGE_PROFILES else []
    for m in merged:
        print(f"  merge profiles/{m} -> people/{m} (## Relationship dossier)")

    moves, assets = build_moves(brain)
    tmap = link_target_map(brain, moves)

    buckets = defaultdict(int)
    for old, new in moves.items():
        dest = new.rsplit("/", 1)[0] if "/" in new else "<root>"
        buckets[f"{old.split('/')[0] if '/' in old else '<root>'} -> {dest}"] += 1
    print("\n-- moves --")
    for k in sorted(buckets):
        print(f"  {k:34} {buckets[k]:4d}")
    print(f"  {'assets':34} {len(assets):4d}")

    # link rewrite across every md file in the brain (wiki + brain-root docs)
    targets = list(wiki.rglob("*.md")) + [p for p in repo.glob("*.md")]
    changed = 0
    for p in targets:
        try:
            t0 = p.read_text(encoding="utf-8")
        except Exception:
            continue
        in_people = p.parent.name in ("people", "profiles", "entities")
        t1 = rewrite_frontmatter_sources(
            strip_type_field(rewrite_images(rewrite_links(t0, tmap), in_people)),
            brain, tmap)
        if t1 != t0:
            changed += 1
            if apply:
                p.write_text(t1, encoding="utf-8")
    print(f"\n-- link/frontmatter rewrite: {changed} files touched --")

    if not apply:
        return

    # move files
    for d in ["people", "sources", "desk/archived", "assets/people"]:
        (wiki / d).mkdir(parents=True, exist_ok=True)
    for old, new in sorted(moves.items()):
        s, t = wiki / old, wiki / new
        t.parent.mkdir(parents=True, exist_ok=True)
        r = sh(["git", "mv", "-f", str(s.relative_to(repo)), str(t.relative_to(repo))], repo)
        if r.returncode != 0:
            shutil.move(str(s), str(t))
    for old, new in sorted(assets.items()):
        s, t = wiki / old, wiki / new
        if not s.exists():
            continue
        t.parent.mkdir(parents=True, exist_ok=True)
        if t.exists():
            s.unlink()                                   # duplicate photo, drop it
            continue
        r = sh(["git", "mv", "-f", str(s.relative_to(repo)), str(t.relative_to(repo))], repo)
        if r.returncode != 0:
            shutil.move(str(s), str(t))
    # brain-root files -> desk
    for fn, dest in ROOT_FILES_TO_DESK.get(brain, {}).items():
        s = repo / fn
        if s.exists():
            t = wiki / dest
            t.parent.mkdir(parents=True, exist_ok=True)
            r = sh(["git", "mv", "-f", fn, str(t.relative_to(repo))], repo)
            if r.returncode != 0:
                shutil.move(str(s), str(t))
    # drop merged profile files + empty legacy dirs
    for prof in merged:
        f = wiki / "profiles" / prof
        if f.exists():
            r = sh(["git", "rm", "-f", "-q", str(f.relative_to(repo))], repo)
            if r.returncode != 0:
                f.unlink()
    # deepest first, so profiles/pictures goes before profiles
    for d in sorted(LEGACY_DIRS + PIC_DIRS, key=lambda x: -x.count("/")):
        p = wiki / d
        if p.is_dir():
            for junk in [".gitkeep", ".DS_Store"]:
                if (p / junk).exists():
                    (p / junk).unlink()
            try:
                p.rmdir()
            except OSError as e:
                print(f"  !! {d} not empty: {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--brain")
    a = ap.parse_args()
    for b in ([a.brain] if a.brain else BRAINS):
        run(b, a.apply)
    print("\nDRY RUN — nothing written. Re-run with --apply." if not a.apply else "\nAPPLIED.")


if __name__ == "__main__":
    main()
