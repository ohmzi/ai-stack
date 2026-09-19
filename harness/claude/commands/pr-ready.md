---
description: Pre-PR gate. Reviews the diff against repo standards and spec, runs a security pass, then drafts the PR body.
argument-hint: "[fixed-point: commit, branch, or tag — defaults to the merge-base with the default branch]"
allowed-tools: Bash(git *), Read, Grep, Glob, Task
---

Run the pre-PR gate for this repo. Work through the four stages in order and do not
skip one because an earlier one was clean — they look for different things.

## 0. Fix the range

Resolve the default branch (`git symbolic-ref refs/remotes/origin/HEAD`, falling back
to `master` then `main`) and set `BASE` to the argument if one was given, otherwise to
`git merge-base HEAD <default>`. Run `git diff --stat "$BASE"...HEAD` and confirm the
range is non-empty and the ref resolves before spending anything on review. If the diff
is empty, stop and say so — an empty range means the fixed point is wrong, not that the
change is clean.

Report the range and the file count before continuing, so the result is attributable to
a specific revision.

## 1. Code review — Standards and Spec

Invoke the `code-review` skill with the fixed point from stage 0. It reviews along two
axes; if the skill is unavailable, do both by hand:

- **Standards** — does the diff follow this repo's own documented standards? Read them
  rather than assuming: `AGENTS.md` / `CLAUDE.md` and anything they point at (T'Day, for
  instance, keeps `docs/CODING_STANDARDS.md`, `docs/TESTING.md`, and a cross-platform
  parity rule for mobile changes).
- **Spec** — does the diff do what the originating issue or spec asked, and nothing else?

Report both side by side. A standards finding is a judgement call; say when it is one.

## 2. Security pass

Invoke the `security-audit` skill in **guidance mode**, scoped to the diff. Guidance mode
is the default and is the right one here — do not run the full six-phase audit unless the
user asked for a codebase audit, because it fans out into many sub-agents and is expensive
on this harness. Match the attack classes to what is actually in the diff: auth and
session handling, user input reaching a query or a shell, secrets, deserialization, and
anything crossing a trust boundary. If the repo has local attack surface (T'Day runs a
Ktor backend, a Vite SPA, and two native mobile clients), say which surfaces the diff
touches and which it does not.

Report findings with file and line. Distinguish what you confirmed from what you suspect.

## 3. Draft the PR body

Only if stages 1 and 2 did not turn up something that needs a decision first. Use this
shape:

```
## Summary

What changed and why, in the fewest sentences that carry it. A diff sketch or tree if
the change is structural.

## Evidence

What was actually run — test commands with their result, a build, a screenshot. Name the
keyboard command, not "tests pass". If nothing was run, say that plainly.

## Merge Danger

- **Door**: one-way or two-way. Can this be reverted by a revert commit, or does it
  migrate data, publish an image, or ship a store build?
- **Blast radius**: what breaks if it is wrong, and who notices.
```

Do not create the PR. Print the body and stop — opening it is the user's call, and on
this repo a push to `master` or `develop` is blocked by a ruleset anyway.

## Report

End with: the range reviewed, the counts from each stage, anything that needs a human
decision, and the one next action.
