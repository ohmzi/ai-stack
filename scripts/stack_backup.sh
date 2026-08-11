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
#   - The repo itself — it lives on GitHub.
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
#   LAST_OK            ISO timestamp of the last fully-successful run; the stack watchdog alerts
#                      when this is older than 26 h, so a silently-failing backup cannot rot.
#
# Retention: 14 dailies + any first-of-month snapshot younger than ~6 months.
set -euo pipefail

DEST="${STACK_BACKUP_DEST:-/media/SandiskSSD/ai-stack-backups}"
OWUI=/volume1/docker/openwebui/config
HERMES="$HOME/.hermes"
COMFY=/volume1/docker/comfyui
STAGING="$DEST/staging"

log() { echo "[stack-backup] $*"; }

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
mkdir -p "$STAGING"/{openwebui,hermes,comfyui}

# --- OpenWebUI ------------------------------------------------------------------------------
log "webui.db (sqlite .backup, WAL-safe)"
sqlite3 "$OWUI/webui.db" ".backup '$STAGING/openwebui/webui.db'"

log "vector_db"
mkdir -p "$STAGING/openwebui/vector_db"
# Collection dirs first, then the sqlite file via .backup so the pair is as close to a single
# point in time as a live copy gets. Chroma only writes during ingestion, which is rare here.
rsync -a --delete --exclude chroma.sqlite3 "$OWUI/vector_db/" "$STAGING/openwebui/vector_db/"
sqlite3 "$OWUI/vector_db/chroma.sqlite3" ".backup '$STAGING/openwebui/vector_db/chroma.sqlite3'"

log "uploads, alerts, keys, metrics"
rsync -a --delete "$OWUI/uploads/" "$STAGING/openwebui/uploads/"
# alerts/contacts.json is the LIVE alert-address file (the ~/.hermes copy is a stale fallback).
rsync -a --delete "$OWUI/alerts/" "$STAGING/openwebui/alerts/"
rsync -a "$OWUI/hermes_api_key" "$STAGING/openwebui/"
[ -f "$OWUI/media_metrics.jsonl" ] && rsync -a "$OWUI/media_metrics.jsonl" "$STAGING/openwebui/"

# --- hermes-agent state (NOT the runtime — that is reinstallable) ---------------------------
log "hermes state"
for f in config.yaml .env alert_transports.env alert_contacts.json channel_directory.json \
         owui_webhook_url gateway_state.json SOUL.md; do
  [ -e "$HERMES/$f" ] && rsync -a "$HERMES/$f" "$STAGING/hermes/"
done
# These two are SQLite and were being rsynced raw, which contradicted this file's own header —
# a torn copy without its -wal is the "looks complete, restores corrupt" artifact. hermes writes
# to state.db continuously (session/cron bookkeeping), so this is not theoretical.
for db in state.db kanban.db; do
  [ -f "$HERMES/$db" ] && sqlite3 "$HERMES/$db" ".backup '$STAGING/hermes/$db'"
done
for d in cron monitor-state sessions skills; do
  [ -d "$HERMES/$d" ] && rsync -a --delete "$HERMES/$d/" "$STAGING/hermes/$d/"
done

# --- ComfyUI (workflows/nodes/IO — never the 137 GB of models) ------------------------------
log "comfyui custom_nodes/input/output"
for d in custom_nodes input output; do
  [ -d "$COMFY/$d" ] && rsync -a --delete "$COMFY/$d/" "$STAGING/comfyui/$d/"
done

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
