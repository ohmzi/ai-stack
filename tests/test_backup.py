#!/usr/bin/env python3
"""The backup must refuse to run when its disk is not mounted.

Why this file exists. scripts/stack_backup.sh originally guarded with

    [ -d "$(dirname "$DEST")" ]

and /media/SandiskSSD is an /etc/fstab entry — so the directory exists on the ROOT filesystem
whether or not the disk is mounted. On an unmounted disk the test passed, mkdir -p recreated the
tree, and the entire nightly landed on the 82%-full NVMe the backup exists to escape, while
LAST_OK was stamped and the watchdog stayed green. At ~300 MB/night that hides for weeks, and the
"backup" would be on the same device as the original — worthless at exactly the moment it matters.

This box has two live examples of the trap: /media/seagate16tb and /media/WD18new are empty
directories right now with nothing mounted.

Offline apart from reading the script and the real mount table. Writes nothing.

Usage:  python3 tests/test_backup.py
"""
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "stack_backup.sh")

results = []


def check(label, ok, detail=""):
    results.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"   {detail}" if detail and not ok else ""))


def main():
    src = open(SCRIPT).read()

    print("--- the guard is a mountpoint test, not a directory test ---")
    check("uses mountpoint(1)", "mountpoint -q" in src)
    check("does NOT guard on a bare -d of the parent",
          '[ -d "$(dirname "$DEST")" ]' not in src)

    print("--- an unmounted target is REFUSED, and nothing is written ---")
    tmp = tempfile.mkdtemp()          # a real dir on / — exactly the dangerous case
    dest = os.path.join(tmp, "would-be-backups")
    r = subprocess.run(["bash", SCRIPT], env={**os.environ, "STACK_BACKUP_DEST": dest},
                       capture_output=True, text=True, timeout=120)
    check("exits non-zero", r.returncode != 0, f"rc={r.returncode}")
    check("says why", "not a mountpoint" in (r.stderr + r.stdout).lower(), r.stderr[:120])
    check("wrote nothing", not os.path.exists(dest), f"{dest} exists")

    print("--- databases go through sqlite3 .backup, never a raw copy ---")
    # The header promises this; state.db/kanban.db were rsynced raw until 2026-08-01.
    for db in ("webui.db", "chroma.sqlite3", "state.db", "kanban.db"):
        check(f"{db} uses .backup", f'".backup' in src and db in src, "not found")
    # Precisely: the plain-file rsync loop (`for f in …`) must not name either DB. Matching a bare
    # substring is not enough — the sqlite loop is literally `for db in state.db kanban.db`, which
    # a naive check flags as a false positive (it did, on first run).
    import re
    rsync_lists = re.findall(r"for f in ([^;]+); do", src, re.S)
    named = [db for db in ("state.db", "kanban.db")
             for lst in rsync_lists if db in lst]
    check("neither DB is in the raw rsync file list", not named, f"raw-copied: {named}")

    print("--- the live target really is a separate device ---")
    live = subprocess.run(["findmnt", "-no", "SOURCE", "--target",
                           "/media/SandiskSSD/ai-stack-backups"],
                          capture_output=True, text=True).stdout.strip()
    root = subprocess.run(["findmnt", "-no", "SOURCE", "--target", "/"],
                          capture_output=True, text=True).stdout.strip()
    if live:
        check(f"backup device ({live}) is not the root device ({root})", live != root)
    else:
        print("  [SKIP] backup target not mounted right now")

    fails = results.count(False)
    print(f"\n{len(results)} checks — {'ALL PASS' if not fails else str(fails) + ' FAILURE(S)'}")
    return 1 if fails else 0


sys.exit(main())
