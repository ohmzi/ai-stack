#!/usr/bin/env bash
#
# Install the deepseek harness from this repo into the running system.
#
# This repo is the source of truth for the harness: router.py, router.json and the
# launcher are symlinked out of here rather than copied, so editing the checkout
# changes what runs and `git status` shows drift instead of hiding it.
#
#   ./install.sh            install/refresh the symlinks
#   ./install.sh --uninstall  remove them (leaves ~/.config/deepseek in place)
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF_DIR="${HOME}/.config/deepseek"
BIN_DIR="${HOME}/.local/bin"

FILES=(router.py router.json)

die() { printf 'install.sh: %s\n' "$*" >&2; exit 1; }

[[ "${1:-}" == "--uninstall" ]] && {
  for f in "${FILES[@]}"; do rm -f "${CONF_DIR}/${f}"; done
  rm -f "${BIN_DIR}/deepseek"
  echo "removed harness symlinks. Ollama models were not touched."
  exit 0
}

mkdir -p "$CONF_DIR" && chmod 700 "$CONF_DIR"
mkdir -p "$BIN_DIR"

for f in "${FILES[@]}"; do
  [[ -r "${HERE}/${f}" ]] || die "missing ${HERE}/${f}"
  ln -sfn "${HERE}/${f}" "${CONF_DIR}/${f}"
done
ln -sfn "${HERE}/deepseek" "${BIN_DIR}/deepseek"

# No API key is installed here on purpose. The router forwards whatever credential
# the client sent to the cloud upstream, so the key lives only in the user's shell
# environment (~/.bashrc) and never enters this repo or a config file.
command -v jq >/dev/null || echo "warning: jq not found; the launcher needs it to read router.json" >&2

echo "installed:"
printf '  %s -> %s\n' "${CONF_DIR}/router.py" "${HERE}/router.py"
printf '  %s -> %s\n' "${CONF_DIR}/router.json" "${HERE}/router.json"
printf '  %s -> %s\n' "${BIN_DIR}/deepseek" "${HERE}/deepseek"
echo
echo "The router starts on demand on the first \`deepseek\` run."
echo "Verify with: deepseek --status"
