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
import argparse, hashlib, os, sqlite3, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = "/volume1/docker/openwebui/config/webui.db"

# OpenWebUI function id -> the file in this repo that is its source of truth.
SOURCES = {
    "auto_assistant": "pipes/live/auto_assistant.py",
    "image_krea":     "pipes/live/image_krea.py",
    "uncensored":     "pipes/live/uncensored.py",
    "animate_scail":  "pipes/live/animate_scail.py",
    "flux_image":     "pipes/live/flux_image.py",
    "adaptive_memory": "filters/adaptive_memory.py",
}

results = []


def check(label, ok, detail=""):
    results.append((label, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"   {detail}" if detail else ""))


def sha8(s):
    return hashlib.sha256(s.encode()).hexdigest()[:8]


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

    fails = sum(1 for _, ok, _ in results if not ok)
    if fails:
        print("\nOpenWebUI is running code that differs from this repo.")
        print("Deploy: Workspace → Functions → edit the function → paste the repo file → Save.")
    print(f"\n{len(results)} checks — {'ALL PASS' if not fails else str(fails) + ' FAILURE(S)'}")
    return 1 if fails else 0


sys.exit(main())
