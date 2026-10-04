# "Audit security" means both skills

When asked to **audit**, pen-test, or run a full/comprehensive security review of code,
run both — they cover different scopes and both are wanted:

1. `security-audit` (cloudflare) in **full audit mode** — all six phases and the report
   artifacts its SKILL.md defines. It defaults to guidance mode, so say explicitly that
   the complete workflow is wanted.
2. `/security-review` (built in) — reviews the pending changes on the current branch.

A focused security question, a triage, or a single-finding investigation is not an
audit: use `security-audit` in guidance mode alone, and do not fan out. `/pr-ready`
keeps its own guidance-mode security stage.
