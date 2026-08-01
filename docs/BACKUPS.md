# Backups

_First automated backup: 2026-08-01. Before that date there was **no** backup automation — the
production DB and all 19 manual `.bak` copies lived on the same NVMe as the stack itself._

## What runs

`scripts/stack_backup.sh`, nightly at **03:30** via the `stack-backup.timer` systemd **user** unit
(`Persistent=true`, so a powered-off night catches up on boot). Target:
**`/media/SandiskSSD/ai-stack-backups/`** — a separate physical disk (`/dev/sdf1`, ext4).

| Captured | How |
|---|---|
| `webui.db` (chats, functions, config) | `sqlite3 .backup` — **WAL-safe; never cp** |
| `vector_db/` incl. `chroma.sqlite3` | rsync + `.backup` for the sqlite file |
| `uploads/`, `alerts/contacts.json` (the LIVE alert addresses), `hermes_api_key`, `media_metrics.jsonl` | rsync |
| `~/.hermes` state: config, env, cron jobs + run history, monitor-state, sessions, skills, state/kanban DBs | rsync (explicit list) |
| ComfyUI `custom_nodes/ input/ output/` | rsync |

**Deliberately not captured:** `comfyui/models` (137 GB, re-downloadable), `~/.hermes/hermes-agent`
(reinstallable runtime), the repo (lives on GitHub), Ollama models (re-creatable — but note
`hermes-genesis` V5 must be rebuilt from `/home/ohmz/models/hermes-genesis/`, which IS worth
keeping; it is not in this backup because 18 GB nightly is not, and upstream `:latest` resolves to
V3, not the V5 in use).

Snapshots are `daily-YYYY-MM-DD/` with unchanged files **hardlinked** to the previous day
(`rsync --link-dest`), so 14 days of ~1.2 GB costs ~1.2 GB plus deltas. Retention: 14 dailies +
first-of-month for ~6 months. `LAST_OK` is stamped only on full success — the stack watchdog
alerts when it is older than 26 h, so a quietly-failing backup cannot rot.

**Accepted gap (owner decision 2026-08-01):** no off-site leg. Both disks share one box; fire,
theft or a PSU event takes them together. When a provider is picked, `duplicity` (already
installed) can push the same `staging/` tree to B2/S3 encrypted.

## Restore drill — run this quarterly, and after any change to the script

The 2026-08-01 drill passed: `PRAGMA integrity_check` = ok, `function/model/config/user` row
counts matched live, `cron/jobs.json` round-tripped byte-equal, chroma integrity ok.

```bash
D=/media/SandiskSSD/ai-stack-backups
T=$(mktemp -d)
cp "$D/latest/openwebui/webui.db" "$T/restored.db"
sqlite3 "$T/restored.db" 'PRAGMA integrity_check;'          # must print: ok
for t in function model config user; do
  echo "$t: live=$(sqlite3 'file:/volume1/docker/openwebui/config/webui.db?mode=ro' "SELECT COUNT(*) FROM $t") \
        restored=$(sqlite3 "$T/restored.db" "SELECT COUNT(*) FROM $t")"
done
rm -rf "$T"
```

## Actual restore (disaster)

1. Stop the consumer: `docker stop open-webui` (or for hermes state: `systemctl --user stop hermes-gateway hermes-delivery.timer`).
2. Copy back from `latest/` (or a dated `daily-*/` for point-in-time):
   `cp $D/latest/openwebui/webui.db /volume1/docker/openwebui/config/webui.db` — and **delete any
   stale `-wal`/`-shm`** beside it, or sqlite will replay the old journal over the restore.
3. Restart the consumer; run `python3 tests/test_deployed.py` (OWUI) or `hermes cron list` (hermes)
   to confirm state is sane.
4. Branding lives inside the container image, not the DB — re-run `branding/apply.sh` if the
   container was also recreated.

## Operations

```bash
systemctl --user start stack-backup            # run one now
systemctl --user list-timers stack-backup.timer
journalctl --user -u stack-backup -n 30        # last run's log
cat /media/SandiskSSD/ai-stack-backups/LAST_OK
```

Disable: `systemctl --user disable --now stack-backup.timer`.
