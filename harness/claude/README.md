# The `claude` harness — skills, rules and hooks for the DeepSeek-backed session

> This is the reference for the Claude Code configuration layer. For the relay that
> lets one session switch between DeepSeek cloud and the local 3090, see
> [harness/deepseek/](../deepseek/README.md). For how the two Claude Code paths are
> launched, see [docs/CLAUDE_CODE.md](../../docs/CLAUDE_CODE.md).

## 1. What this is

Claude Code reads four kinds of instruction, and they do not cost the same. This
harness configures all four deliberately rather than installing skill packs and
hoping, because on this box the context window is the binding constraint and the
model is a third party's.

```
harness/claude/
  install.sh                  symlinks the pieces into ~/.claude/, installs the
                              plugins, merges the hook into settings.json
  hooks/git-guardrails.py     PreToolUse gate on Bash
  commands/pr-ready.md        the pre-PR pipeline
  README.md                   this file
```

`install.sh` follows the deepseek harness contract: **this repo is the source of
truth, and the pieces are symlinked out of it**, so editing the checkout changes
what runs and `git status` shows drift instead of hiding it.

There is no `settings.fragment.json`. The installer performs the merge itself,
because two of its inputs (`claude plugin install`, the marketplace registrations)
write their own settings keys, and a static fragment would fight them.

## 2. The arithmetic that decides everything

Three measurements, all taken on this box, that explain every choice below.

**Path B advertises 98304 tokens, and Claude Code's baseline request is ~25K.**
`harness/deepseek/README.md` §4 measures the baseline — system prompt plus every
tool schema — at roughly 25,000 tokens before the conversation starts. So the
budget a skill set is spending from is not 98304, it is about 73,000, and on the
`haiku` local route it is nearer zero because that tag advertises 24576.

**MCP tool search is off.** Documented in `mcp.md`: Claude Code disables deferred
MCP tool discovery when `ANTHROPIC_BASE_URL` points at a non-first-party host,
"since most proxies don't forward `tool_reference` blocks." The consequence is that
**every configured MCP server loads its complete tool schema payload into every
session on every turn** — the usual "add more servers, it barely costs anything"
assumption is false here. This is why Artemis is project-scoped and not global, and
why the harness installs no MCP server at all.

**A skill's cost depends on how it is invoked, not on how big its body is.**

| Frontmatter | Always-on cost |
|---|---|
| `disable-model-invocation: true` | **Nothing** — the description is not loaded |
| default | its description, capped at 1,536 chars |
| `paths:` on a rule file | nothing until Claude reads a matching file |

The adopted set measures **~2,895 tokens always-on**, which is 2.9% of a 98304
window. `claude plugin details <name>` prints most of that figure; `/skill-doctor`
reports per-skill cost and 7-day usage. One caveat worth knowing before trusting it:
`claude plugin details` **does not count context a `SessionStart` hook injects**, and
labels such hooks "no model context cost" — superpowers' hook is ~851 tokens it omits
from its total (§3). Budget for hook output by hand.

## 3. What is installed, and what was rejected

| Project | Verdict | What is wired | Always-on |
|---|---|---|---|
| [mattpocock/skills](https://github.com/mattpocock/skills) | adopt | plugin, official marketplace | ~1,102 tok |
| [obra/superpowers](https://github.com/obra/superpowers) | adopt | plugin, official marketplace | ~1,410 tok |
| [blader/humanizer](https://github.com/blader/humanizer) | adopt | plugin, own marketplace | ~116 tok |
| [ayghri/i-have-adhd](https://github.com/ayghri/i-have-adhd) | adopt, opt-in | plugin, own marketplace | ~94 tok |
| [cloudflare/security-audit-skill](https://github.com/cloudflare/security-audit-skill) | adopt | vendored checkout, symlinked as a skill, plus a `rules/` pairing policy | ~1 description + ~175 tok |
| [google/artemis](https://github.com/google/artemis) | **defer** | not installed — see §7 | — |
| [affaan-m/ECC](https://github.com/affaan-m/ECC) | **reject** | nothing installed — see §6 | — |

### mattpocock/skills — the backbone

25 skills, and the repo is disciplined about the distinction that matters here:
14 are user-invoked (`disable-model-invocation: true`, zero context cost) and 11 are
model-invoked. The ones this harness leans on:

- **During a session** — `tdd`, `diagnosing-bugs`, `implement`, `domain-modeling`,
  `resolving-merge-conflicts`
- **At PR time** — `code-review`, `triage`
- **Planning** — `grilling` (and its user-invoked front ends `grill-me` and
  `grill-with-docs`), `to-spec`, `to-tickets`, `wayfinder`
- **Design** — `codebase-design`, `improve-codebase-architecture`

It fits this stack because it reads the repo rather than assuming: `code-review`
looks for the project's own documented standards, which T'Day has in
`docs/CODING_STANDARDS.md`, and the skills write to `CLAUDE.md` when one exists and
`AGENTS.md` otherwise, which is the convention several checkouts here already use.

Run `/setup-matt-pocock-skills` **once per repo** before first use — the engineering
skills assume it has set up the issue tracker, triage labels and domain doc layout.

Note the upstream install warning: the plugin route and the `npx skills@latest` route
are exclusive. Installing both leaves every skill twice. This harness uses the plugin
route only. The plugin is pinned by sha in the official marketplace, so releases
arrive when that pin moves, not when upstream tags.

### obra/superpowers — the process layer

15 model-invoked skills and one `SessionStart` hook, installed from the official
marketplace. It is the only entry here that changes *how* a session runs rather than
adding capability to it: the hook injects the whole `superpowers:using-superpowers`
skill into every session it fires on (`startup|clear|compact`), and that skill makes
skill invocation mandatory before any response. It is the same mechanism as
`i-have-adhd` below, deliberately left switched on.

**It costs more than `claude plugin details` says.** That command reports
**~559 tok always-on** and annotates the hook `(harness-only — no model context
cost)`. The annotation is wrong for this section's arithmetic: the hook's
`hookSpecificOutput.additionalContext` is 3,405 characters, **~851 tok**, injected on
every startup, `/clear` and `/compact`. Measured 2026-09-22 by running
`hooks/session-start` with `CLAUDE_PLUGIN_ROOT` set and reading the JSON it emits.

| Component | Measurement | ~Tokens |
|---|---|---|
| 15 skill descriptions, all far under the 1,536-char cap | 2,302 chars | ~559 |
| `SessionStart` hook — full `using-superpowers` injected | 3,405 chars | ~851 |
| **Always-on total** | | **~1,410** |

The set goes from ~1,312 to **~2,720 tokens**, 2.8% of the 98,304 window. Skill bodies
(168,882 bytes across the 15 files; the largest is `subagent-driven-development` at
32,577 bytes) load only on invocation and are not in this figure. For scale, this is
under a third of ECC's rejected `rules/common/` tax.

**It clears the bars §6/§7 set.** It installs namespaced (`superpowers:`), so unlike
ECC's flat `~/.claude/skills/<name>/**` it cannot shadow a built-in; it is pinned by
sha in the official marketplace, the same supply-chain property mattpocock has; and it
declares no MCP servers, so §2's "MCP tool search is off" constraint never bites. Its
hook is pure local bash — no network calls in `hooks/` or `scripts/`. The README's
telemetry note covers only a logo pixel on brainstorming's optional visual companion,
carrying the version string and nothing else, disableable with
`SUPERPOWERS_DISABLE_TELEMETRY`.

**It overlaps mattpocock/skills, and outranks it.** `test-driven-development`,
`systematic-debugging` and `requesting-code-review`/`receiving-code-review` cover
ground mattpocock's `tdd`, `diagnosing-bugs` and `code-review` already hold. Because
the hook fires every session and asserts priority, superpowers is the process layer
and mattpocock is the specialist — which is the intended split, so nothing in
mattpocock is disabled to remove the redundancy. `code-review` is still the one that
reads T'Day's `docs/CODING_STANDARDS.md`.

**The subagent question, settled.** Superpowers' core loop is
`subagent-driven-development` — its largest skill, and the README describes agents
working through engineering tasks. Nothing in this harness pins a subagent model, so
the concern was that Task dispatches inherit the `haiku` slot, which on Path B is
`gemma3:1b` (§2) — a 1B model documented for "session titles, taglines, small
background calls". **Verified 2026-09-22: they do not.** A `deepseek --local` session
was made to dispatch a subagent, and every request in the window routed to
`qwen38-coder:q4-128k` in `~/.cache/deepseek-router.log`; `gemma3:1b` appears zero
times. The skill is safe to use on this route.

### cloudflare/security-audit-skill

No `.claude-plugin/` manifest upstream, so it cannot be installed as a plugin. The
installer clones it to `~/.local/share/claude-harness/vendor/security-audit-skill`
and symlinks `~/.claude/skills/security-audit` at the skill directory within.

It has two modes and gates them itself: **guidance mode** (the default, for focused
questions and diff review) and **full audit mode**, which runs six phases and fans
out into many sub-agents. Full mode is the expensive one and is not what the
pre-PR gate uses.

The two zero-dependency Node validators are the useful part for a pipeline, because
they are deterministic and speak exit codes:

```sh
node ~/.claude/skills/security-audit/validate-findings.cjs        <output-dir>/findings.json
node ~/.claude/skills/security-audit/validate-coverage-ledger.cjs <output-dir>/coverage-ledger.json
```

Both print `PASS: <n> ...` and exit 0, or print `ERROR:`/`FAIL:` lines and exit 1.
Verified on Node 20.20.2 against `[]` (pass) and malformed input (exit 1).

#### The pairing policy

Claude Code also ships its own `security-review`, which reviews the pending changes on
the current branch. The two are not alternatives — `security-review` is diff-scoped,
`security-audit` is codebase-scoped — so `rules/security-audit-policy.md` tells the
agent to run **both** on an explicit audit or pen-test request, with `security-audit`
forced into full audit mode rather than its default guidance mode.

This is the only rule in `~/.claude/rules/`, and it is installed precisely because
rules without `paths:` frontmatter are **unconditional** — 703 bytes, ~175 tokens into
every session in every repo, whether or not a security audit is ever run. That is the
tax §6 rejected ECC for, chosen deliberately here because the trigger is a request type
rather than a file path, so there is nothing to scope `paths:` to. The rule's carve-out
does the real work: a focused security question or single-finding triage stays in
guidance mode and does **not** fan out, which is what keeps the cost from being paid
back in runaway audits.

The alternative considered was a user-invoked `/audit-security` command, which would
cost nothing always-on. It was rejected because it only fires when typed, and the
request was for this to apply *whenever* an audit is asked for.

Verified 2026-09-22 that rules do load on this setup: with the file in place, a fresh
`deepseek -p` session quoted the phrase "all six phases, writing" back verbatim.

### blader/humanizer and ayghri/i-have-adhd

Humanizer is model-triggered — it fires on prose edits without being asked, which is
the point, and costs ~116 tokens to keep available.

`i-have-adhd` is user-invoked only and costs ~94 tokens. It reshapes every response
for the rest of the session, so it is **installed but never switched on**: invoke it
with `/i-have-adhd` when you want it, and do not create
`~/.claude/.i-have-adhd-always`, which is the flag file that makes its SessionStart
hook inject the ruleset into every session.

## 4. The hook

`hooks/git-guardrails.py` is wired to `PreToolUse` with matcher `Bash`, and exits 2
with a reason on stderr, which Claude Code surfaces to Claude as a denial.

It blocks:

| Command | Why |
|---|---|
| `git push --force`, `git push -f` | rewrites published history |
| `git push` naming `master`/`main`/`develop`/`production` | landing straight on a protected branch |
| `git reset --hard` | discards uncommitted work |
| `git clean -f` / `-fd` / `-fdx` | deletes untracked files |
| `git branch -D` | deletes a branch without checking it is merged |
| `git checkout .`, `git restore .` | reverts every working-tree change |

It allows `git push origin <feature>`, `git push --force-with-lease`, `git stash`,
`git reset --soft`, `git branch -d`, and `git checkout -- <path>`.

**It is deliberately narrower than the version everyone copies.** The
`git-guardrails-claude-code` skill in mattpocock/skills ships
`block-dangerous-git.sh`, which blocks on the substring `git push` — every push, to
any branch. That is wrong on this box: T'Day's `AGENTS.md` instructs the agent to
push the active branch when a PR is requested, so the upstream hook would break the
documented workflow on the first PR. A guardrail that has to be disabled protects
nothing, so this one blocks the destructive forms and lets the ordinary ones through.

The hook exits 0 on anything it cannot parse. A guardrail that fires because its own
parser broke is worse than one that occasionally misses.

22 cases were run against it — 11 that must block, 11 that must pass — with no false
positives in either direction. If you change a pattern, re-run that table before
trusting it.

## 5. The pre-PR pipeline

`/pr-ready [fixed-point]` runs four stages: resolve the diff range and confirm it is
non-empty, review Standards and Spec, run the security pass in guidance mode, then
draft a PR body. It **prints** the body and does not open the PR.

It reads the fixed point as a three-dot range (`git diff BASE...HEAD`), so it reviews
what the branch introduced rather than what `master` has moved on to.

## 6. Why ECC is not installed

`affaan-m/ECC` is a serious project — 292 skills, 68 agents, 94 commands, 24 hook
registrations, MIT, no telemetry, no account. It is also the wrong shape for this
harness, and the reasons are measurable rather than aesthetic.

**It installs skills flat.** `--target claude` writes `skills/**` to
`~/.claude/skills/<name>/**` with no namespace, and ECC ships a `security-review`
skill plus a `code-review` command that collide with Claude Code's built-ins. Plugin
installs namespace to `ecc:`; home installs shadow. Which one you get depends on the
install path, which is not a property you want in your editor.

**Its rules would be a permanent tax.** The tempting cherry-pick is `rules/` — 20
language packs, MIT, and the language packs genuinely do carry `paths:` frontmatter
(`rules/kotlin/*.md` is scoped to `**/*.kt`). But every language pack extends
`rules/common/`, and **`common/` has no `paths` frontmatter at all**. Under the
documented rule behaviour — no `paths` means "loaded at launch" — those 10 files,
18,377 bytes, roughly 4,600 tokens, would load **unconditionally into every session
in every repo**, carrying hard mandates like "Minimum Test Coverage: 80%" that no
project here agreed to. Taking the language packs without `common/` leaves dangling
`../common/` links; taking `common/` pays 4,600 tokens a session for generic advice
that would compete with each repo's own `docs/CODING_STANDARDS.md`, which is the
document mattpocock's `code-review` actually reads.

**It runs a second memory system, and a third.** ECC ships `ecc memory` +
`.ecc/memory/` + `ecc-memory-mcp` alongside its own instincts alongside Claude Code's
native auto-memory — which this box actively uses. Its own
`docs/MCP-CONNECTOR-POLICY.md` dropped the `memory` MCP connector precisely because
native memory plus its instincts had absorbed the job.

**Its continuous-learning path bills this account.** The observer shells out to the
local `claude` CLI with `haiku` to mine "instincts" from observations. On this box
plain `claude` is Path A — `https://api.deepseek.com/anthropic`, billed per token. The
observer is off by default, so this is a trap rather than a tax: enable the feature and
a background process starts spending money. It would route through the relay only if
the `deepseek` launcher's environment were inherited, which is not guaranteed for a
daemon.

**Its Stop hooks are heavy.** `stop:format-typecheck` carries a 300-second timeout and
runs on every Stop; PostToolUse registers both a sync and an async dispatcher.

If you want one piece of ECC later, take a single language pack and rewrite its
`../common/` links rather than installing the module. Measure it with
`claude plugin details` and `/skill-doctor` before it stays.

## 7. Why Artemis is deferred

`google/artemis` is a Python 3.12 package that is simultaneously a CLI, a FastAPI
service and an MCP server, exposing five tools (`mobile_run_task`,
`mobile_manage_task`, `mobile_get_device_state`, `mobile_inspect_trace`,
`mobile_diagnose`). It is Apache-2.0 and genuinely useful for Android end-to-end work.

It is not installed globally because MCP tool search is off here (§2), so its five
full tool schemas would load into every session in every repo, forever, for a
capability only T'Day's Android client can use.

Two blockers to clear first, neither of them friction:

1. **It cannot use the credentials on this box.** `.env.example` accepts a Gemini,
   OpenAI, Anthropic, OpenRouter or xAI key and exposes no base-URL override. It
   depends on `langchain-anthropic`, which reads `ANTHROPIC_API_KEY` and calls
   `api.anthropic.com` — but the value in `~/.bashrc` is an `ANTHROPIC_AUTH_TOKEN`
   for DeepSeek, so it would authenticate a DeepSeek token against Anthropic and 401.
   It needs its own key from a provider it supports.
2. It needs ADB and a connected device or emulator.

When both are true, wire it per-project rather than globally:

```sh
cd ~/StudioProjects/Tday
claude mcp add --scope project artemis -- /path/to/artemis/.venv/bin/python -m mcp_server
```

That writes `.mcp.json` at the repo root, which is version-controllable and prompts
for approval per machine. Do **not** use `artemis mcp --install claude` here: it
rewrites `~/.claude.json` directly, and that file holds ~80KB of interleaved state
(`projects`, `oauthStatus`, feature-flag caches) that Claude Code also rewrites while
running.

Its claim that Claude Code auto-loads `~/.claude/rules/*.md` is **correct** — verified
against the 2.1.278 binary, not taken on trust.

## 8. Install

```bash
cd ai-stack/harness/claude
./install.sh
```

Preconditions: `python3`, `node` (18+), `git`, and `claude` on `PATH`. The installer
checks each and says which is missing rather than failing at request time.

It backs up `settings.json` to `settings.json.bak-<timestamp>` before merging, and the
merge is additive — it replaces only its own `PreToolUse` entry, matched on the
`git-guardrails` path, so re-running is idempotent and never stacks a second copy.

`./install.sh --uninstall` removes the plugins, the marketplaces, the three symlinks
and the settings entry. It leaves the vendored checkout in
`~/.local/share/claude-harness/vendor` for you to delete by hand.

**Restart Claude Code** afterwards — skills, commands and rules are read at startup.

## 9. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `/pr-ready` or `security-audit` missing after install | skills load at startup | restart Claude Code |
| A skill you installed is not listed | `~/.claude/skills/` did not exist before, so the session's skill scan predates it | restart; the installer creates the directory |
| `PreToolUse:Bash hook error: [$HOME/...]` in the transcript | the hook blocked the command — this is the intended message, not a bug | read the reason; it names the pattern |
| The hook never fires | hooks are disabled (`disableAllHooks`), or the session predates the settings merge | check `disableAllHooks` in settings; restart |
| A rule you symlinked from this repo does not load | rules symlinked into `~/.claude/rules/` **do** load — verified — but a symlink inside a *project's* `.claude/rules/` pointing outside the working directory is treated as an external import and needs approval | keep user rules in `~/.claude/rules/` |
| Context feels tight on `deepseek --local` | that route advertises 98304, and ~25K is baseline | `/skill-doctor` for per-skill cost; the adopted set is ~2,895 tokens |
| Local turns fail with a context-size 400 after a plugin install | more always-on context than the window allows | see `../deepseek/README.md` §4 before adding more skills |
