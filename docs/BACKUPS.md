# Backups

_First automated backup: 2026-08-01. Before that date there was **no** backup automation — the
production DB and all 19 manual `.bak` copies lived on the same NVMe as the stack itself._

## What runs

`scripts/stack_backup.sh`, nightly at **03:30** via the `stack-backup.timer` systemd **user** unit
(`Persistent=true`, so a powered-off night catches up on boot). Target:
**`/media/SandiskSSD/ai-stack-backups/`** — a separate physical disk (ext4, mounted by UUID from
`/etc/fstab`). It enumerated as `/dev/sdf1` when this was written and as `/dev/sdh1` on 2026-09-30,
so trust the mountpoint, not the device letter.

| Captured | How |
|---|---|
| `webui.db` (chats, functions, config) | `sqlite3 .backup` — **WAL-safe; never cp** |
| `vector_db/` incl. `chroma.sqlite3` | rsync without `chroma.sqlite3*`, then `.backup` for the sqlite file |
| `uploads/`, `alerts/` (`contacts.json` there is the LIVE alert-address file), `hermes_api_key`, `hermes_coding_api_key`, `media_metrics.jsonl` | rsync |
| `~/.hermes` files: `config.yaml`, `.env`, `alert_transports.env`, `alert_contacts.json`, `channel_directory.json`, `owui_webhook_url`, `gateway_state.json`, `SOUL.md` | rsync (explicit list) |
| `~/.hermes` trees: `cron/` (jobs + run history), `monitor-state/`, `sessions/`, `skills/`, `memories/`, `scripts/`, `plugins/` (kept as symlinks), and `profiles/` without its `cache/`, `audio_cache/` and `image_cache/` dirs | `sync_tree`: rsync for the files, `.backup` for each SQLite file inside |
| Every `~/.hermes` SQLite database: each top-level `*.db` (today `state.db`, `kanban.db`, `response_store.db`, `runs_idempotency.db`, `shared-state.db`) and each one inside the trees (today `cron/executions.db`, `cron/notepad.db`, `profiles/coding/state.db`, `profiles/coding/cron/executions.db`) | `sqlite3 .backup`, with a 30 s wait for a writer's lock |
| ComfyUI `custom_nodes/ input/ output/` | rsync |
| This repo's local state, under `repo/`: `ai-stack.bundle` (every ref), `ai-stack.worktree.diff` (uncommitted changes), `ai-stack.untracked.tar` (untracked files git does not ignore) | `git bundle --all`, `git diff --binary HEAD`, `tar`; each rewritten only when it changed |

**Every SQLite file goes through `.backup`, found rather than listed (2026-09-30).** The script used
to name its databases: `state.db` and `kanban.db` went through `.backup` (since 9d3372b, 2026-08-01,
which took them off the raw rsync list), and everything else under `~/.hermes` was rsynced. Then
v0.21.4 put live WAL databases inside the trees that were rsynced wholesale (`cron/executions.db`,
`cron/notepad.db`, the coding profile's `state.db`), and a raw copy of a WAL database is the artifact
this script exists to avoid: it looks complete and restores corrupt. The fixed list also missed
three top-level databases (`response_store.db`, `runs_idempotency.db`, `shared-state.db`), and the
coding profile, its API key, `memories/`, `scripts/` and `plugins/` were not captured at all.

Now `sync_tree` finds every `*.db` in a tree, and a sweep finds every top-level one, and each goes
through `.backup` when its header says SQLite; rsync excludes exactly those paths and their `-wal`,
`-shm` and `-journal` files. The header decides, not the name. An agent-written script is free to
leave a plain-text `*.db` in `monitor-state/`, and `sqlite3` failing on it would, under `set -e`, end
the run before the snapshot, so the night would save nothing. The first version of this change also
excluded by name, which matches directories too: a directory called `vectors.db/` would have
vanished while the run reported OK. Both cases are in `tests/test_backup.py`, and both are now
copied as ordinary data. Run against the real `~/.hermes` into a scratch directory on 2026-09-30: 9
databases, each passing `quick_check`, and the plugin symlinks kept.

**The repo is captured as local state, not trusted to GitHub (2026-09-30).** GitHub holds only what
was pushed. On 2026-09-30 `personal-pipeline` was 8 commits ahead of it, among them `ac1e67e` (the
whole `coding_task` plugin) and `a0d828a` (the gpuguard fix without which v0.21.4's ticker raises on
every spawn), and the watchdog was running an untracked `scripts/health_alert.py`. hermes's
`plugins/` are symlinks into this checkout, so this disk plus GitHub would have restored a dangling
`coding_task` and a pre-fix gpuguard. The bundle is rebuilt only when a ref moves
(`ai-stack.bundle.refs` is the fingerprint), and the diff and the tar are replaced only when their
bytes change, so unchanged copies hardlink between snapshots. Measured: 2.2 MB, 0.3 MB and 0.4 MB,
in 0.55 s. The tar takes every untracked file git does not ignore, stray test output included.

**The newest snapshot predates part of this.** `daily-2026-09-30` (03:31) was taken by the first
version of the change. It has the coding profile and key, `memories/`, `scripts/` and `plugins/`,
but its `hermes/` still lacks `response_store.db`, `runs_idempotency.db` and `shared-state.db`, and
it has no `repo/`. The 2026-10-01 03:30 run is the first real one with both; if it fails,
`OnFailure=` and the watchdog's backup check say so (below).

**Deliberately not captured:** `comfyui/models` (~149 GB measured 2026-08-08, re-downloadable —
this read 137 GB when written on 2026-08-01; the model set grew, the figure did not),
`~/.hermes/hermes-agent` (reinstallable runtime: check out tag `v2026.9.21`, commit `d337b736`), the
rest of `~/.hermes` (`logs/`, the caches, `bin/` with its tirith and uv binaries, the flight browser
profile, runtime `state/`, the 2026-09-23 upgrade snapshot), the repo as a plain file tree (its
local state is the three `repo/` files above), Ollama models
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
(`BACKUP_MAX_AGE_S = 26 * 3600` in `scripts/stack_watchdog.py`), so a backup that stops happening
at all cannot rot. It alerts on the first run that sees the stale stamp: since 2026-09-29 the
watchdog's other checks wait for a second failed run before alerting, but 26 hours of staleness is
already slow-moving, and a second look 5 minutes later adds no evidence. Its recovery is likewise
announced on the first good run. A run that *fails* alerts far faster, by an independent path:
`stack-backup.service` carries `OnFailure=stack-alert@%n.service`, so a non-zero exit texts and
emails the owner within the minute (`scripts/stack_alert.py`, sending through
`alert_transports.send_report`). The text quotes the failed run's own last meaningful journal line,
not whatever line came last: the excerpt is cut at that run's invocation and skips systemd's
bookkeeping ("Consumed … CPU time"), which is what 8 of the 11 unit-failure texts on 2026-09-22..24
had quoted instead of the cause. The email carries the last 5 such lines. The 26 h `LAST_OK` check is
the backstop for the case that path cannot see: a run that never starts.

**Accepted gap (owner decision 2026-08-01):** no off-site leg. Both disks share one box; fire,
theft or a PSU event takes them together. When a provider is picked, `duplicity` (already
installed) can push the same `staging/` tree to B2/S3 encrypted.

### If the disk is not mounted, the run refuses

`/media/SandiskSSD` is an `/etc/fstab` entry, so the directory exists on the root filesystem whether
or not the disk is mounted. The guard is therefore `mountpoint -q`, not `[ -d ]` (the
`mountpoint -q "$MOUNT"` line in `scripts/stack_backup.sh`): with the mount missing the script prints
`REFUSING: … is not a mountpoint`, exits 1, writes nothing and stamps no `LAST_OK` — and the
non-zero exit alerts at once through `OnFailure`, above. The original guard was
`[ -d "$(dirname "$DEST")" ]`, which passes on an unmounted disk: `mkdir -p` would recreate the tree
and the whole nightly would land on the same NVMe this backup exists to escape (82% full when the
script was written; `df -h /` reports 86% on 2026-08-08), while `LAST_OK` was stamped and the
watchdog stayed green. This box has two standing examples of the trap — `/media/seagate16tb` and
`/media/WD18new` are empty directories with nothing mounted. `STACK_BACKUP_DEST` and
`STACK_BACKUP_MOUNT` override the target and the mountpoint for testing, and `STACK_BACKUP_OWUI`,
`STACK_BACKUP_HERMES`, `STACK_BACKUP_COMFY` and `STACK_BACKUP_REPO` the sources; the systemd unit
sets none of them. `tests/test_backup.py` uses the first to drive the real script at a bogus
destination and assert the refusal, the reason text, and that nothing was written, and the rest to
run the whole script against a fake tree it builds in a temp dir.

## The stack watchdog

`scripts/stack_watchdog.py` runs every 5 minutes from `stack-watchdog.timer` and watches the pieces
that deliver alerts, which otherwise had no alarm of their own. A probe that errors is a failure of
that check, never a crash. It alerts on confirmed failures, through the engine it shares with the
search canary (`scripts/health_alert.py`): a check alerts after 2 failed runs and recovers after 2
good runs in a row, except the four where a second look 5 minutes later adds no evidence, which
alert and recover on one run. A down check texts and emails; a degraded one only emails.

| Check | Fails when | Runs to confirm | Severity |
|---|---|---|---|
| `gateway` | `hermes-gateway` is not `active` | 2 | down |
| `api` | `GET 127.0.0.1:8642/health` gets no HTTP answer at all. Any status proves liveness, and `/health` is the one route that needs no key | 2 | down |
| `delivery` | `hermes-delivery.timer` last fired 10+ min ago (it fires every minute) | 2 | down |
| `backup` | `LAST_OK` is older than 26 h | 1 | down |
| `flightclaw` | the unit is not active, or `127.0.0.1:8765/mcp` gives no HTTP answer | 2 | down |
| `pubgate` | `127.0.0.1:4568/api/config` gives no HTTP answer | 2 | down |
| `pubquota` | the `owui-public-quota` container's healthcheck is not `healthy` | 2 | down |
| `ticker` | any profile's `cron/ticker_heartbeat` or `ticker_last_success` is 5+ min old | 2 | down |
| `gwrestarts` | 3+ gateway starts in the trailing 6 h, from hermes's own `~/.hermes/gateway-starts.log` | 1 | degraded |
| `backlog` | a LOG result has waited 15+ min for its channel post (`~/.hermes/cron/output/.delivered.json`) | 1 | degraded |
| `hermesver` | `~/.hermes/hermes-agent` is off the pinned commit, or `/health` reports another version (`HERMES_PIN`: `v2026.9.21`, `d337b736`, `0.21.4`) | 1 | degraded |

Two runs in a row killed by systemd text "Watchdog runs DOWN" at once. ComfyUI and Ollama are probed
and logged, never alerted. The last four checks went live on 2026-09-29, each for a failure every
other check passed: a dead or failing cron ticker inside a live gateway, liveness restarts that are
back between two probes, a channel webhook that withholds results in silence, and an unplanned
`hermes update`. `python3 scripts/stack_watchdog.py --dry-run` probes and prints what it would send,
and neither sends nor saves state. On 2026-09-30 all 11 checks were OK.

## Restore drill — run this quarterly, and after any change to the script

After any change to `scripts/stack_backup.sh`, run `python3 tests/test_backup.py` before the
drill. It is the harness that proves the mountpoint refusal still refuses, that every SQLite file
arrives as a `.backup` copy with no raw `-wal`/`-shm`/`-journal` beside it (a plain-text `*.db` and
a directory called `*.db` arriving as ordinary data), that the repo bundle clones and the diff
applies onto it, and that the live target is not the root device: 85 checks, all passing on
2026-09-30, including "backup device (/dev/sdh1) is not the root device (/dev/nvme0n1p2)". It was
11 checks on 2026-08-08.

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
   stale `-wal`/`-shm`** beside it, or sqlite will replay the old journal over the restore. The same
   goes for every hermes `*.db`, profiles included.
3. Restart the consumer; run `python3 tests/test_deployed.py` (OWUI) or `hermes cron list` (hermes)
   to confirm state is sane.
4. Branding lives inside the container image, not the DB — re-run `branding/apply.sh` if the
   container was also recreated.
5. If the checkout is gone too, rebuild it from `repo/` before starting hermes, because its
   `plugins/` are symlinks into `/home/ohmz/StudioProjects/ai-stack` (the checkout moved there
   from `/home/ohmz/ai-stack` on 2026-10-04; the host symlinks were retargeted in the same move):
   `git clone $D/latest/repo/ai-stack.bundle /home/ohmz/StudioProjects/ai-stack`, then inside it
   `git apply $D/latest/repo/ai-stack.worktree.diff` and `tar -xf $D/latest/repo/ai-stack.untracked.tar`,
   and point `origin` back at GitHub (`git remote set-url origin https://github.com/ohmzi/ai-stack.git`).

## Operations

```bash
systemctl --user start stack-backup            # run one now
systemctl --user list-timers stack-backup.timer
journalctl --user -u stack-backup -n 30        # last run's log
cat /media/SandiskSSD/ai-stack-backups/LAST_OK
```

Disable: `systemctl --user disable --now stack-backup.timer`.
