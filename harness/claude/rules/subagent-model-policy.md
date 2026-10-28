# Subagent model policy

Always dispatch subagents on **haiku**, in every session and every project.

- Pass `model: "haiku"` explicitly on every Agent dispatch.
- In Workflow scripts, set `model: 'haiku'` on every `agent()` call (or on each
  `meta.phases` entry) instead of relying on the session default.
- Never use sonnet, opus, fable, or the inherited session model for a subagent —
  including reviewers, researchers, and one-off helpers.
- If a delegated task genuinely fails on haiku, report the failure and ask the
  user before escalating; never switch models silently.
