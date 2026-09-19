# Notebook mode

Answer a question from **one notebook in Open Notebook**, chosen from the user's own words.

```
"check islamic guidance to answer this question: what does zakat mean?"
        │
        ├─ resolve the name  ──►  Islamic guidance   (deterministic string scoring)
        │                              │
        │                              ├─ 0 sources?          → refuse, ask nothing
        │                              ├─ server ignores scope? → refuse, explain
        │                              └─ ok → POST /api/search/ask  (scoped)
        │                                        └─ stream → renumber [source:x] → Sources footer
        └─ ambiguous or unnamed  →  offer the closest, HOLD the question, answer on the next turn
```

## The one operational fact: Open Notebook must be built from `main`

**Notebook scoping does not exist in any released Open Notebook image.** The newest release is
**v1.14.0 (2026-07-21)**; notebook-scoped search landed afterwards and is `[Unreleased]`.

In the released image, `SearchRequest` and `AskRequest` never declare `notebook_id`, so Pydantic
drops the field and the search runs against the **whole knowledge base** while returning HTTP 200.
Measured on the stock image: `POST /api/search` with `notebook_id: "notebook:totallybogus123"`
returned real global results. `docs/7-DEVELOPMENT/decisions/ADR-008-notebook-scoped-search.md` in
the Open Notebook repo states the bug verbatim.

This is why the stack runs a locally built image:

```yaml
# /home/ohmz/StudioProjects/open-notebook/docker-compose.override.yml  (gitignored)
services:
  open_notebook:
    build: .
    image: open-notebook:local
    pull_policy: never     # load-bearing: the base file sets `always`, which would pull the
                           # released image back over this build on the next `up -d`
```

Rebuild after pulling upstream changes:

```bash
cd /home/ohmz/StudioProjects/open-notebook
git pull
docker compose build open_notebook && docker compose up -d open_notebook
```

**Verify the gate after every rebuild** — this is the check that says the feature is honest:

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost:5055/api/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"x","type":"text","limit":1,"notebook_id":"notebook:totallybogus123"}'
# 404 = scoping is honoured.  200 = it is NOT, and Notebook mode will refuse every request.
```

### Rollback

Migrations 24 and 25 redefine `fn::vector_search` / `fn::text_search` with an extra
`$notebook_ids` parameter, so retiring this image is **not** a tag flip — the old code calls those
functions with the old arity. Restore the dump taken before the first boot on the built image:

```bash
docker cp surreal_data/pre-notebook-mode.surrealql open-notebook-surrealdb-1:/mydata/restore.surrealql
# then, in the surrealdb container:  /surreal import --endpoint http://localhost:8000 \
#   --ns open_notebook --db open_notebook --user root --pass "$SURREAL_PASSWORD" \
#   /mydata/restore.surrealql
```

## The resolver

`pipes/shared/notebook_resolver.py`. Pure functions take the notebook list as an argument, so the
decision is testable offline and identical on every run. `tests/test_notebook_resolver.py` pins
the table below **as measured**, not as intended.

Scoring takes the max of two arms against each notebook name:

1. **Containment** — the normalized name appears in the normalized message → `1.00`.
2. **Window ratio** — windows of `k-1`, `k`, `k+1` tokens scored with `max(ratio, token_set ×
   (0.97 if exact arity else 0.92))`. The demotion stops `token_set_ratio`'s subset-100 from
   letting one word outscore a phrase.

**The `named` gate is what keeps this usable.** A single-word notebook name makes "contains the
word" meaningless — measured, `"the technical debt in this codebase is high"` scores **0.889**
against the notebook `techincal`, higher than many genuine mentions. A match is only `named` when
the name appears verbatim, or has ≥2 tokens, or a naming cue sits within ±3 tokens, or the name is
≥34% of the message. Naming cues deliberately exclude `in`, `from`, `about`, `to`, `the` —
positional prepositions are exactly the noise being rejected.

Bands: `HIGH=0.85`, `LOW=0.55`, `MARGIN=0.15`, `SUGGEST_FLOOR=0.50`.

| Message | Result |
|---|---|
| `check islamic guidance to answer this question` | **answer** Islamic guidance (1.00) |
| `check the technical book` | **answer** techincal (0.89) — the typo resolves |
| `use the self help book to answer: how do i stop procrastinating` | **answer** Self help (1.00) |
| `check the historical fiction notebook` | **confirm** — two names, margin 0.12 < 0.15 |
| `the technical debt in this codebase is high` | **suggest** — 0.89 but not *named* |
| `tell me about the history of rome` | **suggest** |
| `check the book` / `whats the weather` | **suggest** |

The question handed to Open Notebook has the name and the boilerplate stripped
(`"check islamic guidance to answer this question: what does zakat mean?"` → `"what does zakat
mean?"`). Naming a notebook with no question parks it and asks what to ask.

### A settled notebook is remembered for the chat

Added 2026-09-19. Resolving from the user's words is right for the first turn and wrong for every
turn after it: once a chat is plainly about Islamic guidance, being asked which notebook you meant —
again, and again — reads as though the assistant forgot the conversation between messages.

So once a notebook is settled — named unambiguously, or picked from the candidates — it is recorded
for that chat, and a later turn that names none is answered from it instead of re-asking. Naming a
different notebook always wins and replaces the memory.

Three properties are deliberate, and each one is a failure this mode must not have:

- **It cannot cross chats.** The store is keyed on the chat id *and* the user handle, and there is
  deliberately **no "most recent notebook" fallback**: a missing chat id means *no memory*, never
  someone else's. A memory read back in the wrong chat answers from the wrong book while the
  interface says otherwise, which is the one failure the whole feature is built to avoid. The guard
  is the same one `_park_jobs` uses for the same reason.
- **A notebook that has gone falls back to asking.** It is re-resolved against the live list every
  turn, so one deleted in Open Notebook between two messages is not answered from. Answering from a
  book that is gone would be a fabrication.
- **An unanswerable notebook is never remembered.** `_nb_answer` refuses a notebook with no sources;
  recording one would turn "which notebook?" into a refusal replayed on every later turn, with no
  way back to the list.

It lives at `/app/backend/data/notebook_memory.json` (override with `NOTEBOOK_MEMORY`), written
atomically and bounded to the newest 200 chats. A file on the data volume rather than a dict beside
`_notebook_pending` — which is in-memory and wiped by every redeploy, and losing the memory that way
is most of the annoyance. A chat that falls off the end is simply asked once more.

`tests/test_notebook_memory.py` pins all of it, including both contamination cases and the
deletion case.

## The catalogue ("what's in here, and how do I ask about it?")

With Notebook on, a question *about the collection* rather than about its contents is answered from
the collection itself — no model, no summarising:

```
Open Notebook has 5 notebooks, 1 of them with sources.

Turn **Notebook** on and name what you want in your message — the name is matched against
these, so you do not have to be exact:

**Islamic guidance** — 1 source
  • The Barakah Effect_ More With Less - Mohammed Faris (1).pdf
  → say: "check islamic guidance to answer this question: …"

**techincal** — no sources yet
  • *no sources yet — add one in Open Notebook before asking*
```

Naming a notebook scopes it (`list the books in islamic guidance`).

### Follow-up chips

The catalogue also pushes clickable suggestions to Open WebUI by emitting the same event its own
generator uses:

```python
await emitter({"type": "chat:message:follow_ups", "data": {"follow_ups": [...]}})
```

Those are built from the notebook names rather than from a model, so — the property that matters —
**every chip is a phrasing this mode actually resolves.** A suggestion naming a *book* would look
helpful and then fail to find anything, because the resolver matches notebook names. Only notebooks
that have sources are offered: suggesting one the catalogue just called empty sends the user to a
dead end.

Two limits worth knowing. The chips are emitted for the current view only — Open WebUI's own
generator additionally persists them onto the chat row (`Chats.upsert_message_to_chat_by_id_and_message_id`),
and a pipe cannot reach that, so they may not survive a reload. And the mode is not required to be
on for the chips to make sense, but they only appear when the catalogue answers, which happens
inside the mode.

**A notebook answer gets generated chips too**, via the same `_suggest_follow_ups` the chat paths
use — offered at the two exits in `_nb_stream` where an answer actually reached the user, and at no
other. The other exits are failures and truncations, and a suggestion built on a half-delivered
answer is worse than none: it invites the user to ask next about something the assistant never
said. This path asks the **1B**, not the model that answered (that model lives inside Open Notebook
and the pipe cannot address it), so there is no warm tag to reuse and loading the 17 GB chat tenant
purely to write three questions would be absurd.

So Notebook mode has both kinds of chip, and they are different on purpose. A **catalogue** question
gets the deterministic ones, built from your real notebook names — every chip a phrasing the
resolver actually resolves. An **answer** gets generated ones, written from the whole conversation.
The first is a map of what exists; the second is a way onward.

**The classifier is high-precision on purpose.** `book` appears in ordinary questions constantly —
this box's own session history opens with *"What does **this book** talk about in regards to living
within your means…"* and *"According to islamic **books** what should I do…"*. Every pattern
therefore needs a collection noun **and** an inventory-shaped question around it; none of them fires
on "what does the book say about X". Recall is cheap to get wrong: a missed catalogue question falls
through to the resolver's own fallback, which already lists the notebooks and says how to name one.
A false positive would refuse a real question. The pattern list is in
`CATALOG_PATTERNS` (`pipes/shared/notebook_resolver.py`) and every case — positive and the real
negative corpus — is pinned in `tests/test_notebook_resolver.py`.

It answers only inside Notebook mode. A collection question asked with the mode off goes to the
chat model like any other message, which is the same rule the other three modes follow: modes you
set, not modes it guesses.

## Refusals

Every one of these returns a message. **Nothing falls through to normal chat while the control is
on** — that would answer from a model with no notebook behind it while the interface claims
otherwise, which is the failure this whole design exists to prevent.

| Condition | Behaviour |
|---|---|
| Notebook has **0 sources** | Refused **before any request**. Measured: asking an empty notebook does not error — it returns a confident answer citing a source from a *different* notebook |
| Scope probe says the server ignores `notebook_id` | Refused; points at this document |
| Open Notebook unreachable / 401 / 500 | Refused, with the status and the server's own detail |
| No chat model configured in Open Notebook | Refused; no ask attempted |
| Stream dies mid-answer | Whatever arrived, plus a cut-off warning |
| Sidecar missing | "run scripts/deploy_pipe.py" |

## Deployment

```bash
python3 tests/test_notebook_resolver.py        # offline, green before anything ships
python3 scripts/deploy_pipe.py --all           # writes the row + sidecar; no restart needed
python3 tests/test_deployed.py                 # DB row == repo file, sidecar bytes
python3 tests/test_notebook_mode.py            # end-to-end through pipe()
```

The filter must be **attached** to the Assistant model or nothing happens and nothing complains:

```bash
sudo -n sqlite3 "file:/volume1/docker/openwebui/config/webui.db?mode=ro" \
  "select json_extract(meta,'\$.filterIds') from model where id='auto_assistant.auto';"
# expect: ["adaptive_memory","task_mode","notebook_mode"]
```

`tests/test_notebook_mode.py` asserts exactly that, because an installed-but-unattached filter is
the silent total failure for any filter.

## The button (frontend fork)

The control is reachable from the **Integrations dropdown** with no frontend change at all — that
is the staged rollout, and it is worth exercising before touching Svelte. The dedicated fourth
button in the composer comes from the fork (`compose/openwebui/fork/gen/01_mode_buttons.py`), is
data-driven off this filter's own frontmatter, and requires rebuilding
`ai-stack/open-webui:task-mode` plus `compose/openwebui/run.sh`. See the fork's `gen/README.md`.
