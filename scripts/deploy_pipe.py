#!/usr/bin/env python3
"""Deploy a pipe from this repo into OpenWebUI, the step that keeps getting skipped.

WHY THIS EXISTS
---------------
OpenWebUI does not import pipes from disk. It stores each Function's source as a row in
its own SQLite database, and until now the only way in was pasting into
Workspace -> Functions by hand. tests/test_deployed.py exists because that manual step was
missed on 2026-07-28: a fix was written, tested, reviewed and committed while the server
went on serving the previous build, and a green suite plus a clean `git status` read as
"shipped" when it was not.

A test that detects a missed manual step is worth having. A script that removes the manual
step is worth more. This is that script; the test stays as the backstop.

It writes `content` directly and does NOT restart OpenWebUI, because it does not need to:
get_function_module_from_cache (open_webui/utils/plugin.py) re-reads the row on every
request and reloads the module whenever the content differs from what it cached. The new
code is live on the next message.

THE CORRUPTION TRAP THIS CHECKS FOR
-----------------------------------
OpenWebUI runs replace_imports() over every function it loads, which is a naive
`str.replace` of "from utils", "from apps", "from main" and "from config" across the WHOLE
file -- comments and docstrings included, not just import lines. A pipe whose prose happens
to contain "...read from config" gets silently rewritten on load, the stored row stops
matching the repo file, and test_deployed.py then reports drift that no diff of your own
changes explains. So this refuses to deploy a file that replace_imports would alter, and
tells you which line to reword.

Usage:
  python3 scripts/deploy_pipe.py photoreal               # deploy
  python3 scripts/deploy_pipe.py photoreal --dry-run     # show what would change
  python3 scripts/deploy_pipe.py photoreal --rollback    # restore the last backup
  python3 scripts/deploy_pipe.py --all --dry-run         # audit every mapped pipe
"""
import argparse
import json
import os
import shutil
import subprocess
import tempfile
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.environ.get("OPENWEBUI_DB", "/volume1/docker/openwebui/config/webui.db")
BACKUP_DIR = os.path.join(ROOT, ".deploy-backups")

# OpenWebUI function id -> (tracked source, live working copy). The live copy is gitignored
# and exists so the deployed bytes are inspectable without opening the database; keeping it
# in step is part of deploying, not a separate chore.
PIPES = {
    "photoreal":      ("pipes/photoreal.py",      "pipes/live/photoreal.py"),
    "auto_assistant": ("pipes/auto_assistant.py", "pipes/live/auto_assistant.py"),
    "image_krea":     ("pipes/image_krea.py",     "pipes/live/image_krea.py"),
    "animate_scail":  ("pipes/animate_scail.py",  "pipes/live/animate_scail.py"),
    "flux_image":     ("pipes/flux_image.py",     "pipes/live/flux_image.py"),
}

# Files a pipe imports from OpenWebUI's data volume. Pipes are exec'd standalone and cannot
# import a repo-relative module, so shared code is copied in and reached with a sys.path
# insert. photoreal.py degrades to its old behaviour when this is missing rather than
# crashing -- correct for a live pipe, and the reason a stale copy would never surface on
# its own.
SIDECARS = {
    "pipes/shared/identity_edit.py": "/app/backend/data/identity_edit.py",
    "pipes/shared/media_session.py": "/app/backend/data/media_session.py",
}

REPLACEMENTS = ("from utils", "from apps", "from main", "from config")


def sql(query, ro=True):
    uri = f"file:{DB}?mode=ro" if ro else DB
    cmd = ["sudo", "-n", "sqlite3"] + (["-cmd", ".timeout 30000"] if not ro else []) + [uri, query]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"sqlite3 failed: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout


def check_replace_imports(src, path):
    """The trap above. Returns a list of offending 'line_no: text' strings."""
    return [f"{i}: {ln.strip()}" for i, ln in enumerate(src.splitlines(), 1)
            if any(tok in ln for tok in REPLACEMENTS)]


def owui_preflight(src):
    """(manifest, error) computed by OpenWebUI's OWN code, inside its own container.

    Two things the UI and the REST API do that a bare `update function set content=...`
    silently does not, and both matter:

      1. meta.manifest is rebuilt from the file's frontmatter. Skip it and the Functions
         list keeps advertising the previous version forever — observed: the row still read
         version 0.4.0 while serving 0.5.0 code. Cosmetic until you are trying to work out
         which build is live during an incident.
      2. The module is imported before anything is committed, so a file that cannot load is
         rejected instead of stored. That is the check worth having here, because this pipe
         imports identity_edit from the data volume: if that sidecar is missing the pipe
         degrades silently to the old drifting SDXL path rather than erroring, and nothing
         downstream would ever tell you.

    Asking OpenWebUI rather than reimplementing means extract_frontmatter and replace_imports
    cannot drift away from whatever this container's version actually does.
    """
    code = (
        "import sys, json\n"
        "sys.path.insert(0, '/app/backend')\n"
        "sys.path.insert(0, '/app/backend/data')\n"
        "from open_webui.utils.plugin import extract_frontmatter, replace_imports\n"
        "src = sys.stdin.read()\n"
        "out = {'rewritten': replace_imports(src) != src,\n"
        "       'manifest': extract_frontmatter(src)}\n"
        "ns = {}\n"
        "try:\n"
        "    exec(compile(src, 'pipe.py', 'exec'), ns)\n"
        "    out['loads'] = True\n"
        "    p = ns.get('Pipe')\n"
        "    out['pipes'] = p().pipes() if p and hasattr(p(), 'pipes') else None\n"
        "except Exception as e:\n"
        "    out['loads'] = False\n"
        "    out['error'] = f'{type(e).__name__}: {e}'\n"
        "print(json.dumps(out))\n"
    )
    r = subprocess.run(["docker", "exec", "-i", "open-webui", "python", "-c", code],
                       input=src, text=True, capture_output=True)
    if r.returncode != 0:
        return None, f"preflight could not run: {r.stderr.strip()[:200]}"
    try:
        d = json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        return None, f"preflight returned junk: {r.stdout.strip()[:200]}"
    if d.get("rewritten"):
        return None, "replace_imports() would rewrite this file"
    if not d.get("loads"):
        return None, f"the pipe does not import inside the container — {d.get('error')}"
    return d.get("manifest") or {}, None


def deployed_content(fid):
    """Exact stored bytes, via writefile() -- sqlite3's stdout would mangle a 23 KB source.

    The scratch file must live in a directory this user owns. sqlite3 runs under sudo so the
    file lands owned by root, and /tmp is sticky: unlinking there fails with EPERM even
    though the file is world-readable, because removing a file needs write permission on the
    DIRECTORY. mkdtemp() gives us one we own, so the cleanup succeeds.
    """
    d = tempfile.mkdtemp(prefix="owui-deploy-")
    tmp = os.path.join(d, f"{fid}.py")
    try:
        sql(f"select writefile('{tmp}', content) from function where id='{fid}';")
        if not os.path.exists(tmp):
            return None
        with open(tmp, encoding="utf-8") as f:
            return f.read()
    finally:
        shutil.rmtree(d, ignore_errors=True)


def deploy(fid, dry_run=False):
    if fid not in PIPES:
        print(f"  unknown pipe id {fid!r} — known: {', '.join(sorted(PIPES))}")
        return 1
    rel, live_rel = PIPES[fid]
    path = os.path.join(ROOT, rel)
    if not os.path.exists(path):
        print(f"  {rel} does not exist")
        return 1
    with open(path, encoding="utf-8") as f:
        src = f.read()

    bad = check_replace_imports(src, path)
    if bad:
        print(f"  REFUSING to deploy {rel}: OpenWebUI's replace_imports() would rewrite it.")
        print("  Reword these lines (they contain a literal 'from utils/apps/main/config'):")
        for b in bad:
            print(f"    {b}")
        return 1

    current = deployed_content(fid)
    if current is None:
        print(f"  no function row called {fid!r} in the database — create it once in the UI first")
        return 1
    if current == src:
        print(f"  {fid}: already up to date ({len(src)} chars)")
    elif dry_run:
        print(f"  {fid}: WOULD UPDATE — deployed {len(current)} chars, repo {len(src)} chars")
        manifest, err = owui_preflight(src)
        if err:
            print(f"  {fid}: PREFLIGHT WOULD FAIL — {err}")
            return 1
        print(f"  {fid}: preflight ok — imports in-container, manifest v{manifest.get('version')}")
    else:
        # Preflight BEFORE the backup and the write: a file that cannot import must never
        # reach the row, because OpenWebUI would then serve a broken model.
        manifest, err = owui_preflight(src)
        if err:
            print(f"  {fid}: REFUSING to deploy — {err}")
            return 1
        os.makedirs(BACKUP_DIR, exist_ok=True)
        stamp = time.strftime("%Y%m%dT%H%M%S")
        bak = os.path.join(BACKUP_DIR, f"{fid}.{stamp}.py")
        with open(bak, "w", encoding="utf-8") as f:
            f.write(current)
        meta_json = sql(f"select meta from function where id='{fid}';").strip() or "{}"
        with open(os.path.join(BACKUP_DIR, f"{fid}.{stamp}.meta.json"), "w") as f:
            f.write(meta_json)
        try:
            meta = json.loads(meta_json)
        except Exception:
            meta = {}
        meta["manifest"] = manifest
        # readfile() keeps the source out of the command line entirely — no quoting, no
        # escaping, no truncation risk on a 23 KB file full of quotes and backslashes. meta
        # is small and JSON-safe, so a bound-ish literal is fine with quotes doubled.
        meta_sql = json.dumps(meta).replace("'", "''")
        sql(f"update function set content = cast(readfile('{path}') as text), "
            f"meta = '{meta_sql}', "
            f"updated_at = strftime('%s','now') where id = '{fid}';", ro=False)
        after = deployed_content(fid)
        if after != src:
            print(f"  {fid}: VERIFY FAILED — stored {len(after or '')} chars, expected {len(src)}")
            print(f"  previous content is at {bak}")
            return 1
        print(f"  {fid}: deployed {len(src)} chars, manifest v{manifest.get('version')} "
              f"(backup: {os.path.relpath(bak, ROOT)})")

    live = os.path.join(ROOT, live_rel)
    if not os.path.exists(live) or open(live, encoding="utf-8").read() != src:
        if dry_run:
            print(f"  {fid}: WOULD SYNC {live_rel}")
        else:
            os.makedirs(os.path.dirname(live), exist_ok=True)
            shutil.copyfile(path, live)
            print(f"  {fid}: synced {live_rel}")
    return 0


def deploy_sidecars(dry_run=False):
    rc = 0
    for rel, dest in SIDECARS.items():
        path = os.path.join(ROOT, rel)
        if not os.path.exists(path):
            continue
        r = subprocess.run(["docker", "exec", "open-webui", "cat", dest],
                           capture_output=True, text=True)
        current = r.stdout if r.returncode == 0 else None
        with open(path, encoding="utf-8") as f:
            src = f.read()
        if current == src:
            print(f"  {rel}: already in place")
            continue
        if dry_run:
            print(f"  {rel}: WOULD COPY -> {dest}")
            continue
        # Written through the container: the data volume is root-owned on the host, and the
        # container already runs as root with the same directory mounted.
        p = subprocess.run(["docker", "exec", "-i", "open-webui", "sh", "-c", f"cat > {dest}"],
                           input=src, text=True, capture_output=True)
        if p.returncode != 0:
            print(f"  {rel}: FAILED -> {dest}: {p.stderr.strip()}")
            rc = 1
        else:
            print(f"  {rel}: copied -> {dest}")
    return rc


def rollback(fid):
    if not os.path.isdir(BACKUP_DIR):
        print("  no backups")
        return 1
    baks = sorted(f for f in os.listdir(BACKUP_DIR) if f.startswith(f"{fid}."))
    if not baks:
        print(f"  no backups for {fid}")
        return 1
    bak = os.path.join(BACKUP_DIR, baks[-1])
    sql(f"update function set content = cast(readfile('{bak}') as text), "
        f"updated_at = strftime('%s','now') where id = '{fid}';", ro=False)
    with open(bak, encoding="utf-8") as f:
        want = f.read()
    ok = deployed_content(fid) == want
    print(f"  {fid}: {'restored' if ok else 'RESTORE VERIFY FAILED'} from {baks[-1]}")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pipe", nargs="?", help="OpenWebUI function id, e.g. photoreal")
    ap.add_argument("--all", action="store_true", help="every pipe in the map")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--rollback", action="store_true")
    a = ap.parse_args()

    if not os.path.exists(DB):
        print(f"cannot find OpenWebUI's database at {DB}")
        return 2
    if a.rollback:
        if not a.pipe:
            print("--rollback needs a pipe id")
            return 2
        return rollback(a.pipe)
    if not a.pipe and not a.all:
        ap.print_usage()
        return 2

    targets = sorted(PIPES) if a.all else [a.pipe]
    print(f"{'(dry run) ' if a.dry_run else ''}deploying to {DB}")
    rc = 0
    for fid in targets:
        rc |= deploy(fid, a.dry_run)
    print("sidecars:")
    rc |= deploy_sidecars(a.dry_run)
    if not a.dry_run and rc == 0:
        print("\nNo OpenWebUI restart needed — it re-reads the row per request and reloads "
              "when the content changes.")
        print("Confirm with: python3 tests/test_deployed.py")
    return rc


if __name__ == "__main__":
    sys.exit(main())
