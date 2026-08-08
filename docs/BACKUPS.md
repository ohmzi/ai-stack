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
| `~/.hermes` config and trees: `config.yaml`, `.env`, `alert_transports.env`, `alert_contacts.json`, `channel_directory.json`, `owui_webhook_url`, `gateway_state.json`, `SOUL.md`, then the `cron/` (jobs + run history), `monitor-state/`, `sessions/`, `skills/` directories | rsync (explicit file list, then whole dirs) |
| `~/.hermes/state.db`, `~/.hermes/kanban.db` | `sqlite3 .backup` — same rule as `webui.db` |
| ComfyUI `custom_nodes/ input/ output/` | rsync |

**Superseded 2026-08-08 (commit 9d3372b).** The hermes row above originally read "`~/.hermes`
state: config, env, cron jobs + run history, monitor-state, sessions, skills, state/kanban DBs |
rsync (explicit list)". That was true when this doc was written (2beea62, 2026-08-01 02:36) and
stopped being true nine hours later: 9d3372b (2026-08-01 11:35) deleted `state.db` and `kanban.db`
from the plain-file rsync list and put them through `sqlite3 .backup`
(`scripts/stack_backup.sh:86-88`), because raw-rsyncing them contradicted the script's own
"never cp" header and hermes writes `state.db` continuously — a torn copy restores corrupt while
looking complete. `tests/test_backup.py` pins this by parsing the `for f in …` list and asserting
neither DB appears in it (a bare substring match false-positives on the `for db in state.db
kanban.db` loop); 11 checks, all passing on 2026-08-08.

**One SQLite file inside the rsynced trees is still a raw copy.** `cron/` is swept wholesale by
`rsync -a --delete` (`scripts/stack_backup.sh:89-91`), so `~/.hermes/cron/executions.db` (SQLite
3.x, 102,400 B) is captured as a byte-for-byte copy of the live file rather than a `.backup` —
`cmp` of the live file against `latest/hermes/cron/executions.db` reports them identical
(2026-08-08). Disclosed, not fixed: it is run-history bookkeeping the cron ticker touches only when
a job runs, not every tick — its mtime was 2026-08-07 23:34 while `cron/ticker_heartbeat` had just
been rewritten at 2026-08-08 05:47 — so it is not being written underneath the copy the way
`state.db` is. If it is ever moved into the `.backup` loop, `tests/test_backup.py:56` has to gain it
in the same commit or the harness keeps asserting the old four. **Not captured at all:**
`~/.hermes/response_store.db` (SQLite, 20,480 B, last written 2026-07-30) — it appears in neither
the explicit file list nor the `.backup` loop, and is absent from `latest/hermes/`.

**Deliberately not captured:** `comfyui/models` (~149 GB measured 2026-08-08, re-downloadable —
this read 137 GB when written on 2026-08-01; the model set grew, the figure did not),
`~/.hermes/hermes-agent` (reinstallable runtime), the repo (lives on GitHub), Ollama models
(re-creatable — but note `hermes-genesis` V5 must be rebuilt from
`/home/ohmz/models/hermes-genesis/`, which IS worth keeping; it is not in this backup because 18 GB
nightly is not, and upstream `:latest` resolves to V3, not the V5 in use).

Snapshots are `daily-YYYY-MM-DD/` with unchanged files **hardlinked** to the previous day
(`rsync --link-dest`), so 14 days of ~1.2 GB costs ~1.2 GB plus deltas. Retention: 14 dailies +
first-of-month for ~6 months.

> **The dedupe arithmetic was false when written (measured and corrected 2026-08-08).** Hardlinking
> is real, but the two largest files in the payload can never take part in it. `sqlite3 .backup`
> writes a brand-new file every run, so `--link-dest` sees a new inode and copies it in full — and
> `webui.db` and `chroma.sqlite3` went through `.backup` from the first commit, so this never held.
> Measured on the target today: `webui.db` (332,800,000 B) and `vector_db/chroma.sqlite3`
> (269,733,888 B) each have a distinct inode in all 8 snapshots on disk, as do the two hermes DBs
> (`state.db` 10,817,536 B, `kanban.db` 118,784 B). What *does* hardlink: `uploads/`, the
> `vector_db/` collection dirs, the captured comfyui dirs, and the hermes plain files — in
> `latest/`, `uploads/*.jpg`, `config.yaml` and `SOUL.md` carry link counts of 7-8 while `webui.db`
> carries 1. Real cost, from `du -sh daily-*` in one invocation (which counts shared blocks once):
> 1.2G, 639M, 696M, 588M, 584M, 587M, 585M, 588M; whole set `du -sh --exclude=staging` = 5.4G for
> 8 days. That is ~584-588 MB of new blocks every night, not deltas, so 14 dailies is roughly
> 1.2 GB + 13 × ~0.59 GB ≈ 8.9 GB. The payload also grew: `du -sh` on one snapshot counts
> hardlinked blocks in full, and `daily-2026-08-08` alone prints 1.3G against the ~1.2 GB stated
> above. No capacity problem —
> `df -h /media/SandiskSSD` → 1.8T, 12G used, 1% — but this arithmetic is the stated reason for
> keeping 14 dailies, so it should be the real arithmetic. As of 2026-08-08 the script's own
> header still carries both stale numbers this doc just corrected: `scripts/stack_backup.sh:26-27`
> repeats the dedupe arithmetic with a third payload figure again ("a ~1.6 GB payload"), and `:14`
> and `:93` still say 137 GB of comfyui models. Neither was corrected in the script.

`LAST_OK` is stamped only on full success — the stack watchdog alerts when it is older than 26 h
(`scripts/stack_watchdog.py:41`, `BACKUP_MAX_AGE_S = 26 * 3600`), so a backup that stops happening
at all cannot rot. A run that *fails* alerts far faster, by an independent path:
`stack-backup.service` carries `OnFailure=stack-alert@%n.service`, so a non-zero exit texts and
emails the owner within the minute, quoting the last journal line (`scripts/stack_alert.py`, which
pulls `journalctl --user -u <unit> -n 5` and calls `alert_transports.send_alert`). The 26 h
`LAST_OK` check is the backstop for the case that path cannot see: a run that never starts.

**Accepted gap (owner decision 2026-08-01):** no off-site leg. Both disks share one box; fire,
theft or a PSU event takes them together. When a provider is picked, `duplicity` (already
installed) can push the same `staging/` tree to B2/S3 encrypted.

### If the disk is not mounted, the run refuses

`/media/SandiskSSD` is an `/etc/fstab` entry, so the directory exists on the root filesystem whether
or not the disk is mounted. The guard is therefore `mountpoint -q`, not `[ -d ]`
(`scripts/stack_backup.sh:52-56`): with the mount missing the script prints
`REFUSING: … is not a mountpoint`, exits 1, writes nothing and stamps no `LAST_OK` — and the
non-zero exit alerts at once through `OnFailure`, above. The original guard was
`[ -d "$(dirname "$DEST")" ]`, which passes on an unmounted disk: `mkdir -p` would recreate the tree
and the whole nightly would land on the same NVMe this backup exists to escape (82% full when the
script was written; `df -h /` reports 86% on 2026-08-08), while `LAST_OK` was stamped and the
watchdog stayed green. This box has two standing examples of the trap — `/media/seagate16tb` and
`/media/WD18new` are empty directories with nothing mounted. `STACK_BACKUP_DEST` and
`STACK_BACKUP_MOUNT` (`scripts/stack_backup.sh:35`, `:51`) override the target and the mountpoint
for testing; `tests/test_backup.py` uses the first to drive the real script at a bogus destination
and assert the refusal, the reason text, and that nothing was written.

## Restore drill — run this quarterly, and after any change to the script

After any change to `scripts/stack_backup.sh`, run `python3 tests/test_backup.py` before the
drill — it is the harness that proves the mountpoint refusal still refuses, that no SQLite DB
slipped back into the raw rsync list, and that the live target is not the root device (11 checks,
all passing on 2026-08-08, including "backup device (/dev/sdf1) is not the root device
(/dev/nvme0n1p2)").

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
