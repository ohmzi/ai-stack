#!/usr/bin/env python3
"""The backup must refuse to run when its disk is not mounted, and must never raw-copy a live DB.

Why this file exists. scripts/stack_backup.sh originally guarded with

    [ -d "$(dirname "$DEST")" ]

and /media/SandiskSSD is an /etc/fstab entry — so the directory exists on the ROOT filesystem
whether or not the disk is mounted. On an unmounted disk the test passed, mkdir -p recreated the
tree, and the entire nightly landed on the 82%-full NVMe the backup exists to escape, while
LAST_OK was stamped and the watchdog stayed green. At ~300 MB/night that hides for weeks, and the
"backup" would be on the same device as the original — worthless at exactly the moment it matters.

This box has two live examples of the trap: /media/seagate16tb and /media/WD18new are empty
directories right now with nothing mounted.

The second half runs the WHOLE script against a fake OpenWebUI/hermes/ComfyUI tree built in a temp
dir (STACK_BACKUP_OWUI/HERMES/COMFY/DEST/MOUNT). hermes v0.21.4 put live WAL databases inside trees
the script used to rsync wholesale (cron/executions.db, cron/notepad.db, the coding profile's
state.db and cron/executions.db). The fake DBs hold rows that exist ONLY in their -wal file, so a
raw copy of the .db alone is provably missing them and only a real .backup carries them over.
Databases are chosen by content, not name: the fake tree also holds a plain-text *.db, a
DIRECTORY named *.db and a rollback-journal sidecar, each of which the name-based version got
wrong (an aborted night, a silently dropped directory, a raw journal beside its .backup). And the
repo's local state (a bundle, the uncommitted diff, the untracked files) is taken from a small git
repo built for the run, never from the real checkout.

Offline. Reads the script and the real mount table; writes only inside temp dirs it deletes. Never
touches the real backup target or its LAST_OK.

Usage:  python3 tests/test_backup.py
"""
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "stack_backup.sh")

results = []


def check(label, ok, detail=""):
    results.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"   {detail}" if detail and not ok else ""))


def write(path, text="x\n", mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    if mode is not None:
        os.chmod(path, mode)


def wal_db(path, marker):
    """A WAL-mode DB whose `marker` row lives ONLY in the -wal file while the returned
    connection stays open (autocheckpoint off, so nothing is folded back into the .db)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE t (v TEXT)")
    con.commit()
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")   # schema into the main file...
    con.execute("PRAGMA wal_autocheckpoint=0")
    con.execute("INSERT INTO t VALUES (?)", (marker,))  # ...the row only into the -wal
    con.commit()
    return con


def rows(path):
    """Read a DB without creating -wal/-shm next to it (immutable=1)."""
    con = sqlite3.connect(f"file:{path}?immutable=1", uri=True)
    try:
        ok = con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        vals = [r[0] for r in con.execute("SELECT v FROM t")]
        return ok, vals
    finally:
        con.close()


def raw_copy_rows(path, tmp):
    """What a raw rsync of the .db alone (no -wal) would restore."""
    cp = os.path.join(tmp, "rawcopy.db")
    shutil.copyfile(path, cp)
    try:
        return rows(cp)[1]
    finally:
        os.remove(cp)


def git(repo, *args):
    """git in the throwaway fixture repo, isolated from the user's global config."""
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    return subprocess.run(["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                           "-c", "commit.gpgsign=false", *args],
                          env=env, capture_output=True, text=True, check=True).stdout


def fake_repo(repo):
    """A repo with one commit, an uncommitted edit, an untracked file and an ignored one."""
    os.makedirs(repo)
    git(repo, "init", "-q", "-b", "main")
    write(os.path.join(repo, ".gitignore"), "ignored.bin\n")
    write(os.path.join(repo, "hermes", "plugins", "coding_task", "__init__.py"), "V = 1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "coding_task plugin")
    write(os.path.join(repo, "hermes", "plugins", "coding_task", "__init__.py"), "V = 2\n")
    write(os.path.join(repo, "scripts", "health_alert.py"), "print('untracked')\n")
    write(os.path.join(repo, "ignored.bin"), "junk\n")


def fake_tree(base):
    """Minimal OWUI/hermes/ComfyUI trees shaped like the live ones. Returns (env, open conns)."""
    owui, hermes, comfy = (os.path.join(base, n) for n in ("owui", "hermes", "comfy"))
    conns = {}
    # OpenWebUI: every source the script copies unconditionally must exist, or rsync fails.
    conns["webui"] = wal_db(os.path.join(owui, "webui.db"), "webui-wal-row")
    conns["chroma"] = wal_db(os.path.join(owui, "vector_db", "chroma.sqlite3"), "chroma-wal-row")
    write(os.path.join(owui, "vector_db", "coll-1", "data.bin"))
    write(os.path.join(owui, "uploads", "u.txt"))
    write(os.path.join(owui, "alerts", "contacts.json"), "{}\n")
    write(os.path.join(owui, "hermes_api_key"), "k1\n", 0o640)
    write(os.path.join(owui, "hermes_coding_api_key"), "k2\n", 0o640)
    # hermes top level
    write(os.path.join(hermes, "config.yaml"))
    write(os.path.join(hermes, ".env"), "A=1\n", 0o600)
    conns["state"] = wal_db(os.path.join(hermes, "state.db"), "state-wal-row")
    conns["kanban"] = wal_db(os.path.join(hermes, "kanban.db"), "kanban-wal-row")
    # Top-level DBs the old fixed list (state.db, kanban.db) never named.
    conns["resp"] = wal_db(os.path.join(hermes, "response_store.db"), "resp-wal-row")
    write(os.path.join(hermes, "notes.db"), "not sqlite at the top level\n")
    # hermes trees
    write(os.path.join(hermes, "cron", "jobs.json"), "[]\n")
    write(os.path.join(hermes, "cron", "output", "job1", "run.md"))
    conns["cron_exec"] = wal_db(os.path.join(hermes, "cron", "executions.db"), "cron-exec-wal-row")
    conns["cron_note"] = wal_db(os.path.join(hermes, "cron", "notepad.db"), "cron-note-wal-row")
    write(os.path.join(hermes, "memories", "MEMORY.md"), "remember\n", 0o600)
    write(os.path.join(hermes, "scripts", "amz.py"), "print(1)\n")
    write(os.path.join(hermes, "sessions", "s1.json"), "{}\n")
    write(os.path.join(hermes, "skills", "a", "SKILL.md"))
    write(os.path.join(hermes, "monitor-state", "m.json"), "{}\n")
    # Named *.db, not SQLite: an agent-written cron script's own state file.
    write(os.path.join(hermes, "monitor-state", "prices.db"), "rtx5090 1999.99\n")
    # A DIRECTORY named *.db (an LSM/vector store): the old '*.db' exclude dropped it silently.
    write(os.path.join(hermes, "memories", "vectors.db", "000001.sst"), "sst\n")
    # A rollback-journal DB with a -journal beside it. The journal's first byte is zero, so SQLite
    # does not treat it as hot, but rsync still saw it and copied it raw.
    rb = os.path.join(hermes, "skills", "a", "rollback.db")
    con = sqlite3.connect(rb)
    con.execute("CREATE TABLE t (v TEXT)")
    con.execute("INSERT INTO t VALUES ('rollback-row')")
    con.commit()
    con.close()
    with open(rb + "-journal", "wb") as f:
        f.write(b"\0" * 512)
    plugin_target = os.path.join(base, "repo", "hermes", "plugins", "gpuguard")
    write(os.path.join(plugin_target, "__init__.py"))
    os.makedirs(os.path.join(hermes, "plugins"))
    os.symlink(plugin_target, os.path.join(hermes, "plugins", "gpuguard"))
    # the coding profile
    prof = os.path.join(hermes, "profiles", "coding")
    write(os.path.join(prof, "config.yaml"))
    write(os.path.join(prof, ".env"), "API_SERVER_KEY=k2\n", 0o600)
    conns["prof_state"] = wal_db(os.path.join(prof, "state.db"), "prof-state-wal-row")
    conns["prof_exec"] = wal_db(os.path.join(prof, "cron", "executions.db"), "prof-exec-wal-row")
    write(os.path.join(prof, "skills", "b", "SKILL.md"))
    os.makedirs(os.path.join(prof, "plugins"))
    os.symlink(plugin_target, os.path.join(prof, "plugins", "coding_task"))
    for c in ("cache", "audio_cache", "image_cache"):
        write(os.path.join(prof, c, "blob.bin"))
    # ComfyUI
    for d in ("custom_nodes", "input", "output"):
        write(os.path.join(comfy, d, "f.txt"))
    mount = subprocess.run(["findmnt", "-no", "TARGET", "--target", base],
                           capture_output=True, text=True).stdout.strip()
    repo = os.path.join(base, "checkout")
    fake_repo(repo)
    env = {**os.environ,
           "STACK_BACKUP_OWUI": owui, "STACK_BACKUP_HERMES": hermes, "STACK_BACKUP_COMFY": comfy,
           "STACK_BACKUP_REPO": repo,
           "STACK_BACKUP_DEST": os.path.join(base, "dest", "ai-stack-backups"),
           "STACK_BACKUP_MOUNT": mount}
    return env, conns


def hermetic_run():
    print("--- full run against a fake tree: every DB via .backup, new paths captured ---")
    base = tempfile.mkdtemp(prefix="stack-backup-test-")
    conns = {}
    try:
        env, conns = fake_tree(base)
        dest = env["STACK_BACKUP_DEST"]
        if not env["STACK_BACKUP_MOUNT"]:
            print("  [SKIP] could not resolve a mountpoint for the temp dir")
            return
        stg = os.path.join(dest, "staging")
        # A stale raw WAL sidecar an old (pre-fix) run left in staging. It must not survive,
        # or it rides into every snapshot next to a .backup it does not belong to.
        write(os.path.join(stg, "hermes", "cron", "executions.db-wal"), "stale\n")

        # Self-check: the fixture really is the dangerous case. A raw copy of the .db file
        # alone must be MISSING the marker row, or the .backup assertions below prove nothing.
        h = env["STACK_BACKUP_HERMES"]
        raw = raw_copy_rows(os.path.join(h, "cron", "executions.db"), base)
        check("fixture: a raw .db copy lacks the WAL-only row", "cron-exec-wal-row" not in raw,
              f"raw copy saw {raw}")

        r = subprocess.run(["bash", SCRIPT], env=env, capture_output=True, text=True, timeout=300)
        check("script exits 0", r.returncode == 0, f"rc={r.returncode} {r.stderr[-400:]}")
        if r.returncode != 0:
            return
        check("LAST_OK written in the TEMP target", os.path.isfile(os.path.join(dest, "LAST_OK")))
        latest = os.path.join(dest, "latest")
        check("latest -> daily-* snapshot", os.path.islink(latest)
              and os.readlink(latest).startswith("daily-"))

        dbs = {
            "openwebui/webui.db": "webui-wal-row",
            "openwebui/vector_db/chroma.sqlite3": "chroma-wal-row",
            "hermes/state.db": "state-wal-row",
            "hermes/kanban.db": "kanban-wal-row",
            "hermes/response_store.db": "resp-wal-row",
            "hermes/skills/a/rollback.db": "rollback-row",
            "hermes/cron/executions.db": "cron-exec-wal-row",
            "hermes/cron/notepad.db": "cron-note-wal-row",
            "hermes/profiles/coding/state.db": "prof-state-wal-row",
            "hermes/profiles/coding/cron/executions.db": "prof-exec-wal-row",
        }
        for tree in ("staging", "latest"):
            for rel, marker in dbs.items():
                p = os.path.join(dest, tree, rel)
                if not os.path.isfile(p):
                    check(f"{tree}/{rel} exists", False, "missing")
                    continue
                side = [s for s in ("-wal", "-shm", "-journal") if os.path.exists(p + s)]
                check(f"{tree}/{rel}: no -wal/-shm/-journal beside it", not side, f"found {side}")
                ok, vals = rows(p)
                check(f"{tree}/{rel}: integrity ok and holds the WAL-only row (a .backup)",
                      ok and marker in vals, f"integrity={ok} rows={vals}")

        s = lambda *p: os.path.join(stg, *p)
        check("hermes_coding_api_key captured", os.path.isfile(s("openwebui", "hermes_coding_api_key")))
        check("memories/MEMORY.md captured", os.path.isfile(s("hermes", "memories", "MEMORY.md")))
        check("scripts/ captured", os.path.isfile(s("hermes", "scripts", "amz.py")))
        check("cron/jobs.json and cron/output/ still captured",
              os.path.isfile(s("hermes", "cron", "jobs.json"))
              and os.path.isfile(s("hermes", "cron", "output", "job1", "run.md")))
        for link in (("hermes", "plugins", "gpuguard"),
                     ("hermes", "profiles", "coding", "plugins", "coding_task")):
            p = s(*link)
            src = os.path.join(h, *link[1:])
            check(f"{'/'.join(link)} kept as a symlink, same target",
                  os.path.islink(p) and os.readlink(p) == os.readlink(src),
                  f"islink={os.path.islink(p)}")
        penv = s("hermes", "profiles", "coding", ".env")
        check("profile .env captured with mode 0600",
              os.path.isfile(penv) and (os.stat(penv).st_mode & 0o777) == 0o600)
        check("profile config.yaml and skills/ captured",
              os.path.isfile(s("hermes", "profiles", "coding", "config.yaml"))
              and os.path.isfile(s("hermes", "profiles", "coding", "skills", "b", "SKILL.md")))
        leaked = [c for c in ("cache", "audio_cache", "image_cache")
                  if os.path.exists(s("hermes", "profiles", "coding", c))]
        check("profile caches NOT captured", not leaked, f"captured {leaked}")
        check("stale staging -wal from an old raw copy is gone",
              not os.path.exists(s("hermes", "cron", "executions.db-wal")))

        print("--- chosen by content: a *.db that is not SQLite, and a directory named *.db ---")
        p = s("hermes", "monitor-state", "prices.db")
        check("a plain-text *.db is copied as an ordinary file (the run did not abort on it)",
              os.path.isfile(p) and open(p).read() == "rtx5090 1999.99\n")
        check("a directory named vectors.db/ is captured with its contents",
              os.path.isfile(s("hermes", "memories", "vectors.db", "000001.sst")))
        p = s("hermes", "notes.db")
        check("a top-level *.db that is not SQLite is copied as a file",
              os.path.isfile(p) and open(p).read() == "not sqlite at the top level\n")

        print("--- the repo's local state: every ref, the uncommitted diff, the untracked files ---")
        rp = lambda n: os.path.join(stg, "repo", n)
        clone = os.path.join(base, "restored")
        try:
            git(base, "clone", "-q", rp("ai-stack.bundle"), clone)
            log = git(clone, "log", "--format=%s")
        except subprocess.CalledProcessError as e:
            log = f"clone failed: {e.stderr}"
        check("the bundle clones and carries the local commit", "coding_task plugin" in log, log)
        diff = open(rp("ai-stack.worktree.diff")).read() if os.path.isfile(
            rp("ai-stack.worktree.diff")) else ""
        check("the worktree diff holds the uncommitted edit", "+V = 2" in diff, diff[-200:])
        try:
            git(clone, "apply", rp("ai-stack.worktree.diff"))
            applied = open(os.path.join(clone, "hermes", "plugins", "coding_task",
                                        "__init__.py")).read()
        except (subprocess.CalledProcessError, OSError) as e:
            applied = f"apply failed: {e}"
        check("...and applies cleanly onto the restored clone", applied == "V = 2\n", applied)
        names = subprocess.run(["tar", "-tf", rp("ai-stack.untracked.tar")],
                               capture_output=True, text=True).stdout.split()
        check("the untracked tar has the untracked file and not the gitignored one",
              "scripts/health_alert.py" in names and "ignored.bin" not in names, names)
        kept = {n: os.stat(rp(n)).st_mtime_ns for n in
                ("ai-stack.bundle", "ai-stack.worktree.diff", "ai-stack.untracked.tar")}

        print("--- second run: a DB and the coding key removed at the source ---")
        conns.pop("cron_note").close()
        for suffix in ("", "-wal", "-shm"):
            p = os.path.join(h, "cron", "notepad.db" + suffix)
            if os.path.exists(p):
                os.remove(p)
        os.remove(os.path.join(env["STACK_BACKUP_OWUI"], "hermes_coding_api_key"))
        r = subprocess.run(["bash", SCRIPT], env=env, capture_output=True, text=True, timeout=300)
        check("second run exits 0 without hermes_coding_api_key", r.returncode == 0,
              f"rc={r.returncode} {r.stderr[-400:]}")
        check("a DB deleted at the source leaves staging too",
              not os.path.exists(s("hermes", "cron", "notepad.db")))
        ok, vals = rows(s("hermes", "cron", "executions.db"))
        check("surviving cron DB re-taken cleanly", ok and "cron-exec-wal-row" in vals)
        same = [n for n, t in kept.items() if os.stat(rp(n)).st_mtime_ns == t]
        check("nothing in the repo changed: all three files kept (mtime intact, so hardlinked)",
              len(same) == 3, f"rewritten: {sorted(set(kept) - set(same))}")
        git(env["STACK_BACKUP_REPO"], "commit", "-q", "-am", "second commit")
        r = subprocess.run(["bash", SCRIPT], env=env, capture_output=True, text=True, timeout=300)
        check("third run exits 0", r.returncode == 0, f"rc={r.returncode} {r.stderr[-400:]}")
        heads = git(base, "ls-remote", rp("ai-stack.bundle"))
        check("a moved ref rebuilds the bundle with the new commit",
              git(env["STACK_BACKUP_REPO"], "rev-parse", "HEAD").strip() in heads, heads)
        check("...and the now-empty worktree diff replaces the old one",
              os.path.getsize(rp("ai-stack.worktree.diff")) == 0)
    finally:
        for c in conns.values():
            c.close()
        shutil.rmtree(base, ignore_errors=True)


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
    shutil.rmtree(tmp, ignore_errors=True)

    print("--- databases go through sqlite3 .backup, never a raw copy ---")
    # The header promises this; state.db/kanban.db were rsynced raw until 2026-08-01. Every other
    # DB (hermes's top level, the swept trees, profiles/*) is found by find, not named, so the full
    # run below is what proves each of them lands via .backup.
    for db in ("webui.db", "chroma.sqlite3"):
        check(f"{db} uses .backup", f'".backup' in src and db in src, "not found")
    check("hermes's top-level DBs are swept (find -maxdepth 1), not a fixed list",
          'find "$HERMES" -maxdepth 1 -type f -name \'*.db\'' in src
          and "for db in state.db kanban.db" not in src)
    # Precisely: the plain-file rsync loop (`for f in …`) must not name either DB. Matching a bare
    # substring is not enough — the script's comments name both files, which a naive check flags
    # as a false positive (it did, on first run, against the old named .backup loop).
    rsync_lists = re.findall(r"for f in ([^;]+); do", src, re.S)
    named = [db for db in ("state.db", "kanban.db")
             for lst in rsync_lists if db in lst]
    check("neither DB is in the raw rsync file list", not named, f"raw-copied: {named}")
    # Every hermes tree goes through sync_tree (DB-excluding rsync + .backup), none through a bare
    # `rsync -a --delete` that would sweep a live WAL file raw.
    trees = re.search(r"for d in ([^;]+); do\n\s*\[ -d \"\$HERMES/\$d\" \] && sync_tree", src)
    listed = trees.group(1).split() if trees else []
    want = ["cron", "monitor-state", "sessions", "skills", "memories", "scripts", "plugins"]
    check("hermes tree loop covers " + " ".join(want) + " via sync_tree",
          all(d in listed for d in want), f"loop lists {listed}")
    check("profiles/ goes through sync_tree", 'sync_tree "$HERMES/profiles"' in src)
    check("no hermes tree is rsynced raw", not re.search(r'rsync[^\n]*"\$HERMES/\$d/"', src))
    check("sync_tree no longer excludes by name ('*.db' also matched directories)",
          "--exclude='*.db'" not in src)
    check("...it excludes each SQLite file found, anchored, with -wal, -shm and -journal",
          all(x in src for x in ('--exclude="/$lit"', '--exclude="/$lit-wal"',
                                 '--exclude="/$lit-shm"', '--exclude="/$lit-journal"')))
    check("a DB is recognised by its header, not its name", "SQLite format 3" in src)
    check("every .backup waits out a writer's lock (busy timeout)",
          "-cmd '.timeout 30000'" in src)

    hermetic_run()

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
