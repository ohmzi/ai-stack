#!/usr/bin/env bash
#
# Install the Claude Code harness from this repo into the running system.
#
# Same contract as harness/deepseek/install.sh: this repo is the source of truth,
# and the pieces are SYMLINKED into place rather than copied, so editing the
# checkout changes what runs and `git status` shows drift instead of hiding it.
#
#   ./install.sh                 install/refresh everything, then verify
#   ./install.sh --uninstall     remove symlinks, plugins and the settings fragment
#
# Two things this writes that the deepseek harness does not: it runs
# `claude plugin install` (network), and it MERGES a hooks block into
# ~/.claude/settings.json. The merge is additive, is written atomically, and
# takes a timestamped backup of settings.json before touching it.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDE_DIR="${HOME}/.claude"
HOOKS_DIR="${CLAUDE_DIR}/hooks"
COMMANDS_DIR="${CLAUDE_DIR}/commands"
RULES_DIR="${CLAUDE_DIR}/rules"
SKILLS_DIR="${CLAUDE_DIR}/skills"
VENDOR_DIR="${HOME}/.local/share/claude-harness/vendor"
SETTINGS="${CLAUDE_DIR}/settings.json"

MARKETPLACES=(
  "anthropics/claude-plugins-official"
  "blader/humanizer"
  "ayghri/i-have-adhd"
)
PLUGINS=(
  "mattpocock-skills@claude-plugins-official"
  "humanizer@humanizer"
  "i-have-adhd@i-have-adhd"
)
SECURITY_AUDIT_REPO="https://github.com/cloudflare/security-audit-skill.git"
SECURITY_AUDIT_DIR="${VENDOR_DIR}/security-audit-skill/skills/security-audit"

die()  { printf 'install.sh: %s\n' "$*" >&2; exit 1; }
warn() { printf 'install.sh: warning: %s\n' "$*" >&2; }
have() { command -v "$1" >/dev/null 2>&1; }

for arg in "$@"; do
  case "$arg" in
    --uninstall) UNINSTALL=1 ;;
    -h|--help)   sed -n '3,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)           die "unknown argument: $arg" ;;
  esac
done

# -- uninstall ----------------------------------------------------------------

if [[ "${UNINSTALL:-0}" == "1" ]]; then
  for p in "${PLUGINS[@]}"; do
    claude plugin uninstall "$p" >/dev/null 2>&1 || true
  done
  for m in "${MARKETPLACES[@]}"; do
    claude plugin marketplace remove "$(basename "$m")" >/dev/null 2>&1 || true
  done
  rm -f "${HOOKS_DIR}/git-guardrails.py"
  rm -f "${COMMANDS_DIR}/pr-ready.md"
  rm -f "${SKILLS_DIR}/security-audit"
  python3 - "$SETTINGS" <<'PY' || warn "could not rewrite settings.json; remove the hooks block by hand"
import json, os, sys
path = sys.argv[1]
try:
    with open(path) as fh:
        settings = json.load(fh)
except FileNotFoundError:
    sys.exit(0)
hooks = settings.get("hooks", {})
pre = [g for g in hooks.get("PreToolUse", [])
       if not any("git-guardrails" in h.get("command", "") for h in g.get("hooks", []))]
if pre:
    hooks["PreToolUse"] = pre
else:
    hooks.pop("PreToolUse", None)
if not hooks:
    settings.pop("hooks", None)
tmp = path + ".tmp"
with open(tmp, "w") as fh:
    json.dump(settings, fh, indent=2)
    fh.write("\n")
os.replace(tmp, path)
PY
  echo "removed plugins, symlinks and the PreToolUse hook."
  echo "The vendored security-audit checkout stays at ${VENDOR_DIR}; delete it by hand."
  exit 0
fi

# -- preconditions ------------------------------------------------------------
# Each check maps to a failure this harness actually produces, so a fresh machine
# reports what is missing here instead of 401-ing or silently skipping a stage.

for tool in python3 node git claude; do
  have "$tool" || die "missing '$tool' on PATH"
done

NODE_MAJOR="$(node -p 'process.versions.node.split(".")[0]')"
[[ "$NODE_MAJOR" -ge 18 ]] || warn "node ${NODE_MAJOR} found; the security-audit validators want 18+"

[[ -r "${HERE}/hooks/git-guardrails.py" ]] || die "missing ${HERE}/hooks/git-guardrails.py"
[[ -r "${HERE}/commands/pr-ready.md" ]]    || die "missing ${HERE}/commands/pr-ready.md"

# -- plugins ------------------------------------------------------------------

for m in "${MARKETPLACES[@]}"; do
  name="$(basename "$m")"
  if claude plugin marketplace list 2>/dev/null | grep -q "$name"; then
    claude plugin marketplace update "$name" >/dev/null 2>&1 || true
  else
    echo "adding marketplace ${m}"
    claude plugin marketplace add "$m" >/dev/null || die "could not add marketplace ${m}"
  fi
done

for p in "${PLUGINS[@]}"; do
  if claude plugin list 2>/dev/null | grep -q "${p%%@*}"; then
    echo "plugin already installed: ${p}"
  else
    echo "installing plugin ${p}"
    claude plugin install "$p" --scope user >/dev/null || die "could not install ${p}"
  fi
done

# -- the security-audit skill (no plugin manifest upstream) -------------------

mkdir -p "${VENDOR_DIR}"
if [[ -d "${VENDOR_DIR}/security-audit-skill/.git" ]]; then
  git -C "${VENDOR_DIR}/security-audit-skill" fetch --depth 1 origin HEAD >/dev/null 2>&1 &&
    git -C "${VENDOR_DIR}/security-audit-skill" reset --hard FETCH_HEAD >/dev/null 2>&1 ||
    warn "could not refresh the security-audit checkout; using what is on disk"
else
  echo "cloning security-audit-skill"
  git clone --depth 1 "$SECURITY_AUDIT_REPO" "${VENDOR_DIR}/security-audit-skill" >/dev/null ||
    die "could not clone ${SECURITY_AUDIT_REPO}"
fi
[[ -r "${SECURITY_AUDIT_DIR}/SKILL.md" ]] || die "missing ${SECURITY_AUDIT_DIR}/SKILL.md"

# -- symlinks -----------------------------------------------------------------

mkdir -p "$HOOKS_DIR" "$COMMANDS_DIR" "$SKILLS_DIR"
ln -sfn "${HERE}/hooks/git-guardrails.py" "${HOOKS_DIR}/git-guardrails.py"
chmod +x "${HERE}/hooks/git-guardrails.py"
ln -sfn "${HERE}/commands/pr-ready.md" "${COMMANDS_DIR}/pr-ready.md"
ln -sfn "$SECURITY_AUDIT_DIR" "${SKILLS_DIR}/security-audit"

# -- merge the hook into settings.json ----------------------------------------

[[ -f "$SETTINGS" ]] || echo '{}' > "$SETTINGS"
cp -a "$SETTINGS" "${SETTINGS}.bak-$(date +%Y%m%dT%H%M%S)"

python3 - "$SETTINGS" <<'PY'
import json, os, sys

path = sys.argv[1]
with open(path) as fh:
    settings = json.load(fh)

entry = {
    "matcher": "Bash",
    "hooks": [{
        "type": "command",
        "command": "$HOME/.claude/hooks/git-guardrails.py",
        "timeout": 10,
        "statusMessage": "Checking git command...",
    }],
}

hooks = settings.setdefault("hooks", {})
pre = hooks.setdefault("PreToolUse", [])
# Replace any existing entry for this hook so re-running install is idempotent
# and never stacks a second copy of the same guard.
pre[:] = [g for g in pre if not any("git-guardrails" in h.get("command", "") for h in g.get("hooks", []))]
pre.append(entry)

tmp = path + ".tmp"
with open(tmp, "w") as fh:
    json.dump(settings, fh, indent=2)
    fh.write("\n")
os.replace(tmp, path)
print("merged PreToolUse git-guardrails hook into settings.json")
PY

# -- verify -------------------------------------------------------------------

echo
echo "checks"
printf '  %-34s %s\n' "settings.json"        "$(python3 -c '
import json,sys
d=json.load(open(sys.argv[1]))
print("ok, %d hook event(s)" % len(d.get("hooks", {})))' "$SETTINGS")"
printf '  %-34s %s\n' "enabled plugins"      "$(claude plugin list 2>/dev/null | grep -c '@' || echo 0) installed"
printf '  %-34s %s\n' "git-guardrails hook"  "$([[ -x $HOOKS_DIR/git-guardrails.py ]] && echo 'executable' || echo 'NOT EXECUTABLE')"
printf '  %-34s %s\n' "security-audit skill" "$([[ -r $SKILLS_DIR/security-audit/SKILL.md ]] && echo 'linked' || echo 'MISSING')"
printf '  %-34s %s\n' "security-audit node deps" "$(node -e 'process.exit(0)' 2>/dev/null && echo 'node ok' || echo 'NODE BROKEN')"
echo
echo "Restart Claude Code to pick up new skills, commands and hooks."
