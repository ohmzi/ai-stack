#!/usr/bin/env bash
#
# Nightly versioned backup of the ai-stack's irreplaceable state to the SandiskSSD.
#
#   ./scripts/stack_backup.sh              # run one backup now
#   systemctl --user start stack-backup    # same, via the unit (journal-logged)
#
# Why this exists: until 2026-08-01 there was NO backup automation anywhere on this box.
# webui.db (every chat, every function, every config row), its 19 manual .bak copies, and the
# vector store all lived on the same 82%-full NVMe — one disk failure lost the stack and all of
# its "backups" in the same instant. /media/SandiskSSD is a separate physical device (sdf1).
#
# What is deliberately NOT here:
#   - comfyui/models (137 GB) and ~/.hermes/hermes-agent — re-downloadable/reinstallable.
#   - The repo as a plain file tree. GitHub has only what was pushed, so its LOCAL state is
#     captured instead: a git bundle of every ref, the uncommitted diff and the untracked files
#     (see "this repo" below).
#   - An off-site leg. Both disks share one PSU and one house; fire/theft takes them together.
#     Recorded as an accepted gap (owner decision 2026-08-01) until a cloud provider is picked —
#     duplicity is already installed and can target B2 natively when that day comes.
#   - /volume1/docker/openwebui-public/config/webui.db — the PUBLIC instance's database
#     (docs/PUBLIC_INSTANCE.md). Deliberately excluded, not overlooked: everything in it is a guest
#     chat, by design fresh every visit and reaped within 24h by scripts/purge_public_guests.py — the
#     opposite of "irreplaceable state" this backup exists to protect. The three rows worth keeping
#     (owner account, Guests group, the model access_grant) are three curl calls documented in
#     PUBLIC_INSTANCE.md's bootstrap section, cheaper to redo than to restore.
#
# Databases are copied with sqlite3 ".backup", never cp: webui.db carries a live multi-MB WAL at
# all times, and a raw copy taken mid-checkpoint is a corrupt backup that LOOKS complete — the
# worst possible artifact, discovered only on restore day.
#
# Layout on the target:
#   staging/           the current sync (rsync --delete keeps it exact)
#   daily-YYYY-MM-DD/  snapshots; unchanged files are HARDLINKS to the previous day (--link-dest),
#                      so 14 days of a ~1.6 GB payload costs ~1.6 GB + daily deltas, not 14x.
#   latest -> daily-…  convenience symlink
#   …/repo/            ai-stack.bundle (+ .refs), ai-stack.worktree.diff, ai-stack.untracked.tar
#   LAST_OK            ISO timestamp of the last fully-successful run; the stack watchdog alerts
#                      when this is older than 26 h, so a silently-failing backup cannot rot.
#
# Retention: 14 dailies + any first-of-month snapshot younger than ~6 months.
set -euo pipefail

DEST="${STACK_BACKUP_DEST:-/media/SandiskSSD/ai-stack-backups}"
# Source overrides exist only so tests/test_backup.py can run the whole script against a fake
# tree it builds in a temp dir. Unset (the systemd unit sets none), they are the live paths.
OWUI="${STACK_BACKUP_OWUI:-/volume1/docker/openwebui/config}"
HERMES="${STACK_BACKUP_HERMES:-$HOME/.hermes}"
COMFY="${STACK_BACKUP_COMFY:-/volume1/docker/comfyui}"
# The repo this script lives in (the unit runs it by its absolute path inside the checkout).
REPO="${STACK_BACKUP_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
STAGING="$DEST/staging"

log() { echo "[stack-backup] $*"; }

# is_sqlite FILE — FILE is an SQLite database, judged by its content: the header every SQLite file
# starts with ("SQLite format 3\0"), or an empty main file whose data is still all in its -wal (a
# new WAL database before its first checkpoint). A name is not evidence: a plain-text prices.db an
# agent-written cron script leaves in monitor-state/ made sqlite3 fail with "file is not a
# database", and under set -e that aborted the whole night, webui.db included.
is_sqlite() {
  if [ -s "$1" ]; then
    [ "$(head -c 15 -- "$1" 2>/dev/null)" = "SQLite format 3" ]
  else
    [ -e "$1-wal" ]
  fi
}

# db_backup SRC DST — one consistent copy of a live database. The busy timeout is for a
# rollback-journal DB: a writer holding its lock at 03:30 otherwise fails the run at once with
# "database is locked" (a WAL DB never blocks a reader).
db_backup() {
  mkdir -p "$(dirname "$2")"
  sqlite3 -cmd '.timeout 30000' "$1" ".backup '$2'"
}

# rsync_lit PATH — PATH as an rsync pattern that matches only itself. rsync reads a backslash as
# an escape only in a pattern that holds a wildcard (* ? [), so escaping is applied only then.
rsync_lit() {
  local s="$1"
  case "$s" in
    *[[*?]*) s="${s//\\/\\\\}"; s="${s//\[/\\[}"; s="${s//\*/\\*}"; s="${s//\?/\\?}" ;;
  esac
  printf '%s' "$s"
}

# sync_tree SRC DST [DIRNAME...] — copy a directory tree with SQLite files taken by .backup.
#
# Every regular *.db under SRC that is_sqlite goes through sqlite3 .backup into the same relative
# path; rsync moves everything else. hermes v0.21.4 put live WAL databases inside trees this script
# used to rsync wholesale (cron/executions.db, cron/notepad.db, and the coding profile's state.db),
# and a raw copy of a WAL file is the torn "looks complete, restores corrupt" artifact the header
# warns about. The DBs are found with find, not a fixed list, so a database hermes adds later still
# lands, via .backup.
#
# rsync excludes exactly those paths, anchored, with their -wal, -shm and -journal sidecars. An
# exclude by NAME ('*.db') also matched directories: a memories/vectors.db/ directory vanished from
# the backup while the run reported OK. Now a directory, or a file that only happens to be called
# *.db, is ordinary data and rsync copies it. The -journal exclude matters for a rollback-mode DB:
# its hot journal, copied raw before the .backup ran, would be replayed onto that later copy the
# first time it opens, mixing two points in time.
#
# --delete-excluded clears the raw copies and stale sidecars earlier runs left in staging, so each
# .backup writes a fresh file and a DB deleted at the source leaves staging too.
# Each DIRNAME (e.g. cache) is skipped at any depth, by both rsync and find.
sync_tree() {
  local src="$1" dst="$2"; shift 2
  local ex=() prune=() dbs=() d db rel lit
  for d in "$@"; do
    ex+=(--exclude="$d/")
    prune+=(-type d -name "$d" -prune -o)
  done
  while IFS= read -r -d '' db; do
    is_sqlite "$db" || continue
    rel="${db#"$src"/}"
    dbs+=("$rel")
    lit="$(rsync_lit "$rel")"
    ex+=(--exclude="/$lit" --exclude="/$lit-wal" --exclude="/$lit-shm" --exclude="/$lit-journal")
  done < <(find "$src" "${prune[@]}" -type f -name '*.db' -print0)
  mkdir -p "$dst"
  rsync -a --delete --delete-excluded "${ex[@]}" "$src/" "$dst/"
  for rel in "${dbs[@]}"; do
    db_backup "$src/$rel" "$dst/$rel"
  done
}

# put_if_changed NEW DST — move NEW over DST only when the bytes differ, else drop NEW. An
# unchanged file keeps its mtime, so --link-dest hardlinks it into the next snapshot for free.
put_if_changed() {
  if [ -f "$2" ] && cmp -s -- "$1" "$2"; then rm -f -- "$1"; else mv -f -- "$1" "$2"; fi
}

# The guard MUST be `mountpoint`, not `-d`.
#
# /media/SandiskSSD is an /etc/fstab entry, so the directory exists on the ROOT filesystem whether
# or not the disk is mounted. A `-d` test therefore passes on an unmounted disk, mkdir -p happily
# recreates the tree, and the whole nightly lands on the 82%-full NVMe this backup exists to
# escape — while LAST_OK is stamped and the watchdog stays green. At ~300 MB/night that hides for
# weeks. This box already has two proofs of the failure mode: /media/seagate16tb and
# /media/WD18new are empty 755 dirs right now with nothing mounted.
MOUNT="${STACK_BACKUP_MOUNT:-$(dirname "$DEST")}"
mountpoint -q "$MOUNT" || {
  echo "REFUSING: $MOUNT is not a mountpoint — the backup disk is not mounted." >&2
  echo "Writing here would put the backup on the same disk as the original." >&2
  exit 1
}
mkdir -p "$STAGING"/{openwebui,hermes,comfyui,repo}

# --- OpenWebUI ------------------------------------------------------------------------------
log "webui.db (sqlite .backup, WAL-safe)"
sqlite3 "$OWUI/webui.db" ".backup '$STAGING/openwebui/webui.db'"

log "vector_db"
mkdir -p "$STAGING/openwebui/vector_db"
# Collection dirs first, then the sqlite file via .backup so the pair is as close to a single
# point in time as a live copy gets. Chroma only writes during ingestion, which is rare here.
# The glob also keeps out chroma.sqlite3-wal/-shm/-journal: the live DB has none today (rollback
# mode), but a raw sidecar beside the .backup copy gets replayed onto it the first time it opens.
rsync -a --delete --exclude 'chroma.sqlite3*' "$OWUI/vector_db/" "$STAGING/openwebui/vector_db/"
sqlite3 "$OWUI/vector_db/chroma.sqlite3" ".backup '$STAGING/openwebui/vector_db/chroma.sqlite3'"

log "uploads, alerts, keys, metrics"
rsync -a --delete "$OWUI/uploads/" "$STAGING/openwebui/uploads/"
# alerts/contacts.json is the LIVE alert-address file (the ~/.hermes copy is a stale fallback).
rsync -a --delete "$OWUI/alerts/" "$STAGING/openwebui/alerts/"
rsync -a "$OWUI/hermes_api_key" "$STAGING/openwebui/"
# The coding profile's key. It must match API_SERVER_KEY in ~/.hermes/profiles/coding/.env, and the
# pair can only be regenerated together, so both halves are captured (the .env via profiles/ below).
[ -f "$OWUI/hermes_coding_api_key" ] && rsync -a "$OWUI/hermes_coding_api_key" "$STAGING/openwebui/"
[ -f "$OWUI/media_metrics.jsonl" ] && rsync -a "$OWUI/media_metrics.jsonl" "$STAGING/openwebui/"

# --- hermes-agent state (NOT the runtime — that is reinstallable) ---------------------------
log "hermes state"
for f in config.yaml .env alert_transports.env alert_contacts.json channel_directory.json \
         owui_webhook_url gateway_state.json SOUL.md; do
  [ -e "$HERMES/$f" ] && rsync -a "$HERMES/$f" "$STAGING/hermes/"
done
# Every top-level *.db, swept like the trees below rather than named. state.db and kanban.db were
# once rsynced raw (a torn copy without its -wal is the "looks complete, restores corrupt"
# artifact), and a fixed list of the two then missed response_store.db, runs_idempotency.db and
# shared-state.db, and would miss whatever hermes adds later (memory_store.db, projects.db,
# verification_evidence.db when those features are enabled), while the same file under a profile
# was captured by sync_tree. A *.db that is not SQLite is copied as an ordinary file.
while IFS= read -r -d '' db; do
  if is_sqlite "$db"; then
    db_backup "$db" "$STAGING/hermes/${db##*/}"
  else
    rsync -a "$db" "$STAGING/hermes/"
  fi
done < <(find "$HERMES" -maxdepth 1 -type f -name '*.db' -print0)
# Whole trees, all through sync_tree so any SQLite inside them is taken with .backup. cron/ holds
# jobs.json plus two WAL databases (executions.db, notepad.db). plugins/ is symlinks into this
# repo; rsync -a keeps them as symlinks, and their targets are captured with the repo below, which
# is what makes them resolve after a restore.
for d in cron monitor-state sessions skills memories scripts plugins; do
  [ -d "$HERMES/$d" ] && sync_tree "$HERMES/$d" "$STAGING/hermes/$d"
done
# profiles/<name>/ is a full hermes home per profile: for coding, config.yaml, a .env holding its
# API_SERVER_KEY, state.db (WAL), cron/, plugins/ symlinks and skills/. The caches regenerate.
log "hermes profiles"
[ -d "$HERMES/profiles" ] && sync_tree "$HERMES/profiles" "$STAGING/hermes/profiles" \
  cache audio_cache image_cache

# --- ComfyUI (workflows/nodes/IO — never the 137 GB of models) ------------------------------
log "comfyui custom_nodes/input/output"
for d in custom_nodes input output; do
  [ -d "$COMFY/$d" ] && rsync -a --delete "$COMFY/$d/" "$STAGING/comfyui/$d/"
done

# --- this repo's local state -------------------------------------------------------------------
# GitHub holds only what was pushed, and this branch routinely runs ahead of it with uncommitted
# work on top. On 2026-09-30 it was 8 commits ahead, among them ac1e67e (the whole coding_task
# plugin) and a0d828a (the gpuguard fix without which v0.21.4's cron ticker raises on every spawn),
# and the watchdog ran an untracked scripts/health_alert.py. hermes's plugins/ are symlinks into
# this repo, so a restore from this disk plus GitHub brought back a dangling coding_task and a
# pre-fix gpuguard. Three files:
#   ai-stack.bundle          every ref (git bundle --all): `git clone ai-stack.bundle ai-stack`
#   ai-stack.worktree.diff   uncommitted changes to tracked files: `git apply` it on that clone
#   ai-stack.untracked.tar   untracked files that are not gitignored: untar into the clone
# The bundle is rebuilt only when a ref moved (its .refs file is the fingerprint); the other two
# go through put_if_changed. --no-optional-locks: a background read never takes index.lock.
# $STAGING must be absolute here: git -C resolves a relative output path inside the repo.
log "repo local state ($REPO)"
git_r() { git --no-optional-locks -C "$REPO" "$@"; }
git_r rev-parse --git-dir > /dev/null
refs="$(git_r for-each-ref --format='%(objectname) %(refname)'; echo "HEAD $(git_r rev-parse HEAD)")"
if [ -f "$STAGING/repo/ai-stack.bundle" ] && [ -f "$STAGING/repo/ai-stack.bundle.refs" ] \
   && [ "$refs" = "$(cat "$STAGING/repo/ai-stack.bundle.refs")" ]; then
  log "repo: no ref moved, bundle kept"
else
  git_r bundle create -q "$STAGING/repo/ai-stack.bundle.tmp" --all
  mv -f "$STAGING/repo/ai-stack.bundle.tmp" "$STAGING/repo/ai-stack.bundle"
  printf '%s\n' "$refs" > "$STAGING/repo/ai-stack.bundle.refs"
fi
git_r diff --binary HEAD > "$STAGING/repo/ai-stack.worktree.diff.tmp"
put_if_changed "$STAGING/repo/ai-stack.worktree.diff.tmp" "$STAGING/repo/ai-stack.worktree.diff"
git_r ls-files -z -o --exclude-standard \
  | tar -C "$REPO" --null --ignore-failed-read -T - -cf "$STAGING/repo/ai-stack.untracked.tar.tmp"
put_if_changed "$STAGING/repo/ai-stack.untracked.tar.tmp" "$STAGING/repo/ai-stack.untracked.tar"

# --- rotate into a dated, hardlink-deduped snapshot -----------------------------------------
TODAY="daily-$(date +%F)"
LINKDEST=()
[ -d "$DEST/latest" ] && LINKDEST=(--link-dest="$DEST/latest/")
log "snapshot $TODAY"
rsync -a --delete "${LINKDEST[@]}" "$STAGING/" "$DEST/$TODAY/"
ln -sfn "$TODAY" "$DEST/latest"

# --- retention ------------------------------------------------------------------------------
now=$(date +%s)
i=0
while IFS= read -r d; do
  i=$((i + 1))
  [ "$i" -le 14 ] && continue
  day="${d#*daily-}"
  if [[ "$day" == *-01 ]]; then
    age=$(( (now - $(date -d "$day" +%s)) / 86400 ))
    [ "$age" -le 190 ] && continue
  fi
  log "retention: dropping $d"
  rm -rf -- "$DEST/$d"
done < <(cd "$DEST" && ls -1d daily-* 2>/dev/null | sort -r)

date -Is > "$DEST/LAST_OK"
# Report APPARENT size and the disk cost this snapshot actually added. `du -sh` on one snapshot
# counts hardlinked blocks in full, so it prints ~the whole payload every night and would never
# reveal that --link-dest had stopped deduping. The pair does.
apparent=$(du -sh "$DEST/$TODAY" | cut -f1)
added=$(du -sh --exclude=staging "$DEST" | cut -f1)
log "OK — $TODAY holds $apparent; whole backup set is $added on disk; LAST_OK written"
log "target: $(findmnt -no SOURCE --target "$DEST")"
