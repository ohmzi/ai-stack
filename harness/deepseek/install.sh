#!/usr/bin/env bash
#
# Install the deepseek harness from this repo into the running system.
#
# This repo is the source of truth for the harness. router.py, router.json and the
# launcher are SYMLINKED out of here rather than copied, so editing the checkout
# changes what runs and `git status` shows drift instead of hiding it. The corollary
# is worth stating plainly: changing branch or editing these files changes the live
# harness immediately. There is no staging step.
#
#   ./install.sh                 install/refresh the symlinks, then verify
#   ./install.sh --with-service  also install and start the systemd user unit
#   ./install.sh --uninstall     remove the symlinks (stops at that; see below)
#
# Nothing here installs an API key. See "The one manual prerequisite" in README.md.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF_DIR="${HOME}/.config/deepseek"
BIN_DIR="${HOME}/.local/bin"
UNIT_NAME="deepseek-router.service"
UNIT_DIR="${HOME}/.config/systemd/user"

FILES=(router.py router.json)
WITH_SERVICE=0

die() { printf 'install.sh: %s\n' "$*" >&2; exit 1; }
warn() { printf 'install.sh: warning: %s\n' "$*" >&2; }

for arg in "$@"; do
  case "$arg" in
    --with-service) WITH_SERVICE=1 ;;
    --uninstall)    UNINSTALL=1 ;;
    -h|--help)      sed -n '3,16p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)              die "unknown argument: $arg" ;;
  esac
done

if [[ "${UNINSTALL:-0}" == "1" ]]; then
  if systemctl --user is-enabled "$UNIT_NAME" >/dev/null 2>&1; then
    systemctl --user disable --now "$UNIT_NAME" || true
  fi
  rm -f "${UNIT_DIR}/${UNIT_NAME}"
  systemctl --user daemon-reload 2>/dev/null || true
  for f in "${FILES[@]}"; do rm -f "${CONF_DIR}/${f}"; done
  rm -f "${BIN_DIR}/deepseek"
  pkill -f "${CONF_DIR}/router.py" 2>/dev/null || true
  echo "removed harness symlinks and unit. Ollama models were not touched;"
  echo "the qwen38-coder:q4-128k tag stays until you 'ollama rm' it."
  exit 0
fi

# -- install ------------------------------------------------------------------

[[ -r "${HERE}/router.py" ]]   || die "missing ${HERE}/router.py"
[[ -r "${HERE}/router.json" ]] || die "missing ${HERE}/router.json"
[[ -r "${HERE}/deepseek" ]]    || die "missing ${HERE}/deepseek"

mkdir -p "$CONF_DIR" && chmod 700 "$CONF_DIR"
mkdir -p "$BIN_DIR"

for f in "${FILES[@]}"; do
  ln -sfn "${HERE}/${f}" "${CONF_DIR}/${f}"
done
ln -sfn "${HERE}/deepseek" "${BIN_DIR}/deepseek"

if [[ "$WITH_SERVICE" == "1" ]]; then
  mkdir -p "$UNIT_DIR"
  ln -sfn "${HERE}/systemd/${UNIT_NAME}" "${UNIT_DIR}/${UNIT_NAME}"
  systemctl --user daemon-reload
  systemctl --user enable --now "$UNIT_NAME"
  echo "systemd user unit enabled: the router now starts at login."
else
  echo "systemd unit NOT enabled (the router starts on demand instead)."
  echo "  to start it at login:  ./install.sh --with-service"
fi

# -- verify -------------------------------------------------------------------
#
# Every check here maps to a failure this harness actually produced, so a fresh
# machine fails loudly at install time rather than mysteriously at 2am.

echo
echo "installed:"
printf '  %s -> %s\n' "${CONF_DIR}/router.py"   "${HERE}/router.py"
printf '  %s -> %s\n' "${CONF_DIR}/router.json" "${HERE}/router.json"
printf '  %s -> %s\n' "${BIN_DIR}/deepseek"     "${HERE}/deepseek"

echo
echo "checks:"
command -v jq >/dev/null \
  && echo "  ok    jq present (the launcher reads router.json with it)" \
  || warn "jq missing -- the launcher cannot read router.json without it"

command -v ollama >/dev/null \
  && echo "  ok    ollama present" \
  || warn "ollama missing -- the local backend will be unreachable"

# The launcher passes this through to the cloud upstream; without it the cloud leg
# 401s. The key itself is deliberately never written to this repo.
if [[ -n "${ANTHROPIC_AUTH_TOKEN:-}${DEEPSEEK_API_TOKEN:-}" ]]; then
  echo "  ok    an API token is present in the environment (cloud leg usable)"
else
  warn "no ANTHROPIC_AUTH_TOKEN / DEEPSEEK_API_TOKEN in this shell."
  warn "  the cloud route will 401 until ~/.bashrc exports one. Local routes are unaffected."
fi

# The stock coder tag is 32768 and unusable as a Claude Code backend; the harness
# needs the 128K tag. See "Why the stock 32K tag is unusable here" in README.md.
if command -v ollama >/dev/null; then
  if ollama list 2>/dev/null | awk 'NR>1{print $1}' | grep -qx "qwen38-coder:q4-128k"; then
    echo "  ok    qwen38-coder:q4-128k present (the local default)"
  else
    warn "qwen38-coder:q4-128k is NOT built. The local routes will 404 until:"
    warn "  ollama create qwen38-coder:q4-128k -f ${HERE}/models/qwen38-coder-128k.Modelfile"
  fi
fi

echo
echo "next:  deepseek --status     (health + routing table)"
