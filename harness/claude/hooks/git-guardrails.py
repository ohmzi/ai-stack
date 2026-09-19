#!/usr/bin/env python3
"""PreToolUse gate: block Bash commands that destroy uncommitted work or rewrite
shared history.

Wired to `PreToolUse` with matcher `Bash` in ~/.claude/settings.json. Reads the
hook payload on stdin and exits 2 to block, printing the reason to stderr --
Claude Code surfaces stderr to Claude on a PreToolUse denial.

Deliberately narrower than the obvious version of this hook. The widely copied
`block-dangerous-git.sh` from mattpocock/skills blocks *every* `git push`, which
is wrong on this box: T'Day's AGENTS.md tells the agent to push the active branch
when a PR is requested. A guardrail that breaks the documented workflow gets
disabled, and then it protects nothing. So this blocks the destructive forms and
lets the ordinary forms through:

  blocked   git push --force / -f          rewriting published history
            git push <protected branch>    landing straight on master/develop
            git reset --hard               discards uncommitted work
            git clean -f / -fd / -fdx      deletes untracked files
            git branch -D                  deletes a branch without merged check
            git checkout . / git restore . reverts every working-tree change

  allowed   git push origin feature/x      the normal flow
            git push --force-with-lease    the safe force, after a rebase
            git stash, git reset --soft, git branch -d

Exit 0 on anything it cannot parse. A guardrail that fires because its own parser
broke is worse than one that occasionally misses.
"""

import json
import re
import sys

# Branches that must never be pushed to directly. Matches the rulesets protecting
# develop and master on github.com/ohmzi/Tday, plus the conventional names used by
# the other checkouts on this box.
PROTECTED = {"master", "main", "develop", "production", "prod"}


def deny(reason: str) -> None:
    print(f"BLOCKED: {reason}", file=sys.stderr)
    sys.exit(2)


def reads_as_protected_push(command: str) -> str | None:
    """Return the protected branch name if this command pushes to one."""
    if not re.search(r"\bgit\s+push\b", command):
        return None
    # `git push origin master`, `git push origin HEAD:develop`, `git push -u origin main`
    for match in re.finditer(
        r"\bgit\s+push\b([^&|;]*)", command
    ):
        tail = match.group(1)
        # Strip a leading `--force*` style flag so the branch scan sees the refspecs.
        for token in tail.split():
            if token.startswith("-"):
                continue
            # `HEAD:develop` and `src:dst` forms -- the destination is what matters.
            ref = token.split(":")[-1]
            if ref in PROTECTED:
                return ref
    return None


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    command = (payload.get("tool_input") or {}).get("command") or ""
    if not command.strip():
        sys.exit(0)

    if re.search(r"\bgit\s+push\b[^&|;]*(--force\b|(?<![\w-])-f(?![\w-]))", command) and not re.search(
        r"--force-with-lease", command
    ):
        deny(
            "'git push --force' rewrites published history. Use 'git push "
            "--force-with-lease' if the remote ref is known, or ask the user."
        )

    protected = reads_as_protected_push(command)
    if protected:
        deny(
            f"'git push' targets the protected branch '{protected}'. Land the work "
            "on a working branch and open a PR instead."
        )

    destructive = (
        (r"\bgit\s+reset\s+--hard\b", "'git reset --hard' discards uncommitted work"),
        (r"\bgit\s+clean\s+-[a-z]*f", "'git clean -f' deletes untracked files"),
        (r"\bgit\s+branch\s+-D\b", "'git branch -D' deletes a branch without checking it is merged"),
        (r"\bgit\s+checkout\s+\.\s*($|[&|;])", "'git checkout .' reverts every working-tree change"),
        (r"\bgit\s+restore\s+\.\s*($|[&|;])", "'git restore .' reverts every working-tree change"),
    )
    for pattern, reason in destructive:
        if re.search(pattern, command):
            deny(f"{reason}. If that is really intended, ask the user to run it.")

    sys.exit(0)


if __name__ == "__main__":
    main()
