#!/usr/bin/env python3
"""Does OpenWebUI actually run the code in this repo?

Why this file exists. OpenWebUI does not import pipes from disk — it stores each Function's source
as a row in its own SQLite database, and the only way in is pasting into Workspace → Functions.
`pipes/live/` is gitignored working state, so there are three copies of every pipe (tracked source,
live working copy, and the DB row the server actually executes) and nothing kept them honest.

That is not hypothetical. On 2026-07-28 the `_gpu_revoked` fix was written, tested, reviewed and
committed — and OpenWebUI went on serving the previous build, because nobody pasted it in. Every
test in this repo passed against a file the running server had never seen. A green suite plus a
clean `git status` read as "shipped", and it was not.

This test closes that gap: for every installed Function, the DB row must be byte-identical to its
repo source. It is the one check that fails when the deploy step is skipped.

The database is read through a read-only URI while OpenWebUI is live. That is deliberate — a
`sudo cp` of the main file would miss anything still sitting in the write-ahead log and could report
drift that does not exist. Verified: the read-only view and a WAL-checkpointed copy agree.

Usage:  python3 tests/test_deployed.py
        python3 tests/test_deployed.py --db /path/to/webui.db
"""
import argparse, hashlib, os, re, sqlite3, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = "/volume1/docker/openwebui/config/webui.db"

# The README's Pipes/Filters tables are the roster a reader trusts, and nothing asserted them.
# By 2026-08-02 they had drifted on five separate counts — a checkpoint swap (Krea 2 -> RedCraft),
# a workspace rename (🪄 Assistant -> Ω Assistant), a function id that did not match its own
# filename, a pipe missing entirely, and an active filter documented nowhere. Every one of those
# was invisible to a green suite, for the same reason `pipes/live/` was before this file existed:
# no check compared the doc to the box.
README = "README.md"

# OpenWebUI function id -> the file in this repo that is its source of truth.
SOURCES = {
    "auto_assistant": "pipes/live/auto_assistant.py",
    "image_krea":     "pipes/live/image_krea.py",
    "photoreal":      "pipes/live/photoreal.py",
    "animate_scail":  "pipes/live/animate_scail.py",
    "flux_image":     "pipes/live/flux_image.py",
    "adaptive_memory": "filters/adaptive_memory.py",
    "task_mode": "filters/task_mode.py",
}

# The hop BEFORE the one above, which nothing checked until now. `pipes/live/` is gitignored,
# so the chain is actually tracked source -> live copy -> DB row, and the checks above only
# ever covered the second link. The first was held together by the convention that whoever
# edited a pipe remembered to copy it across — which is the same convention that failed on
# 2026-07-28 and is the reason this file exists.
#
# The shared module has the same shape: pipes cannot import a repo-relative file, so
# identity_edit.py is copied into OpenWebUI's data volume and imported from there. If that
# copy drifts, photoreal.py silently falls back to the old drifting SDXL path — it is written
# to degrade rather than crash, which means nothing would surface it except this check.
TWINS = {
    "pipes/photoreal.py": "pipes/live/photoreal.py",
    "pipes/shared/identity_edit.py": "/volume1/docker/openwebui/config/identity_edit.py",
    "pipes/shared/media_session.py": "/volume1/docker/openwebui/config/media_session.py",
}

results = []


def check(label, ok, detail=""):
    results.append((label, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"   {detail}" if detail else ""))


def sha8(s):
    return hashlib.sha256(s.encode()).hexdigest()[:8]


def readme_roster():
    """{function id: the 'Model in the UI' cell} for every row of the README's tables.

    A filter's table has no such column, so its value is None. Rows are recognised by a
    leading backticked id, which is why the Function column has to stay the OpenWebUI id rather
    than the filename. Those agree for every pipe today, but they are separate things and have
    disagreed before — a row keyed on the filename read as a missing pipe.
    """
    roster = {}
    for line in open(os.path.join(ROOT, README)):
        m = re.match(r"\|\s*`([a-z_]+)`", line)
        if not m:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        roster[m.group(1)] = cells[1] if len(cells) >= 3 else None
    return roster


def picker_name(db, fid, content, is_active):
    """What OpenWebUI 0.10.2 actually shows in the model picker for this function.

    For a manifold pipe the label comes from the pipe's own `pipes()` return
    (`functions.py:104`), and an active `model` row overrides it (`utils/models.py:152`).
    The `function.name` column never reaches the picker, so comparing against it would pass
    while the UI said something else. Returns None for anything not selectable.
    """
    if not is_active:
        return None
    tail = content.split("def pipes", 1)[-1]
    m = re.search(r'"id"\s*:\s*"([^"]+)"\s*,\s*"name"\s*:\s*"([^"]+)"', tail)
    if not m:
        return None  # a filter or action — no picker entry of its own
    pipe_id, name = m.group(1), m.group(2)
    row = db.execute("select name, is_active from model where id = ?",
                     (f"{fid}.{pipe_id}",)).fetchone()
    if row:
        # An INACTIVE row is not a no-op that leaves the pipes() name standing — it is how a
        # manifold entry gets hidden. `get_all_models` deletes the entry outright
        # (`utils/models.py:169`), and since the picker and the dispatcher read the same
        # `app.state.MODELS`, a hidden model is also uncallable (`main.py:1026`).
        return row[0] if row[1] else None
    return name


def check_readme(db, rows):
    print(f"\n  {README} roster → installed functions")
    roster = readme_roster()
    if not roster:
        check(f"{README} lists any functions", False,
              "no backticked ids found — did the table format change?")
        return

    for fid, content, is_active in sorted(rows):
        documented = fid in roster
        check(f"{README} documents {fid}", documented,
              "" if documented else "installed on the box but absent from the tables")
        if not documented:
            continue
        shown, claimed = picker_name(db, fid, content, is_active), roster[fid]
        if claimed is None:            # filters table: two columns, nothing to compare
            continue
        if shown is None:
            ok = any(w in claimed.lower() for w in ("disabled", "hidden"))
            check(f"{README} marks {fid} as not selectable", ok,
                  "" if ok else f"claims {claimed!r} but it is not in the picker")
        else:
            ok = claimed.strip("*_ ") == shown
            check(f"{README} names {fid} as {shown!r}", ok,
                  "" if ok else f"README says {claimed!r}, the picker shows {shown!r}")

    installed_ids = {fid for fid, _, _ in rows}
    for fid in sorted(roster):
        listed = fid in installed_ids
        check(f"{fid} in {README} is installed", listed,
              "" if listed else "documented but no such function on the box")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.environ.get("OPENWEBUI_DB", DEFAULT_DB))
    a = ap.parse_args()

    if not os.path.exists(a.db):
        print(f"cannot find OpenWebUI's database at {a.db}")
        print("pass --db /path/to/webui.db (it lives in the container's /app/backend/data)")
        return 2
    try:
        db = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
        rows = db.execute("select id, content, is_active from function").fetchall()
    except sqlite3.Error as e:
        print(f"could not read {a.db}: {e}")
        return 2

    print(f"db   : {a.db}")
    print(f"repo : {ROOT}")
    print(f"installed functions: {len(rows)}\n")

    installed = {}
    for fid, content, is_active in rows:
        installed[fid] = content
        rel = SOURCES.get(fid)
        state = "active" if is_active else "inactive"
        if rel is None:
            # Not a failure: something was installed through the UI that this repo does not own.
            # Worth saying out loud, because it is code running on the box with no source in git.
            print(f"  [ .. ] {fid} ({state}) — installed but not mapped in SOURCES; no repo file to compare")
            continue
        path = os.path.join(ROOT, rel)
        if not os.path.exists(path):
            check(f"{fid}: repo source exists", False, f"{rel} is missing")
            continue
        src = open(path).read()
        ok = src == installed[fid]
        check(f"{fid} ({state}) matches {rel}", ok,
              "" if ok else f"db={sha8(installed[fid])} ({len(installed[fid])}B) "
                            f"repo={sha8(src)} ({len(src)}B)")

    # A repo pipe that is not installed at all is usually intentional (video.py was retired into
    # auto_assistant), so report it without failing.
    for fid, rel in SOURCES.items():
        if fid not in installed and os.path.exists(os.path.join(ROOT, rel)):
            print(f"  [ .. ] {rel} has no installed function called {fid!r} — not deployed")

    print("\n  tracked source → deployed copy")
    for tracked, twin in TWINS.items():
        tpath = os.path.join(ROOT, tracked)
        wpath = twin if os.path.isabs(twin) else os.path.join(ROOT, twin)
        if not os.path.exists(tpath):
            continue
        tsrc = open(tpath).read()
        if not os.path.exists(wpath):
            check(f"{tracked} → {twin}", False, "the deployed copy does not exist")
            continue
        wsrc = open(wpath).read()
        check(f"{tracked} → {twin}", tsrc == wsrc,
              "" if tsrc == wsrc else f"repo={sha8(tsrc)} ({len(tsrc)}B) "
                                      f"deployed={sha8(wsrc)} ({len(wsrc)}B) — copy it across")

    check_readme(db, rows)

    fails = sum(1 for _, ok, _ in results if not ok)
    if fails:
        print("\nOpenWebUI is running code that differs from this repo.")
        print("Deploy: python3 scripts/deploy_pipe.py <function-id>")
        print("        (or by hand: Workspace → Functions → edit → paste the repo file → Save)")
    print(f"\n{len(results)} checks — {'ALL PASS' if not fails else str(fails) + ' FAILURE(S)'}")
    return 1 if fails else 0


sys.exit(main())
