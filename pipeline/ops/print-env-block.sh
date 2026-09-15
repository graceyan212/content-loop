#!/bin/bash
# Print the /etc/dani/dani.env block to paste into an SSM session on the box.
#
# RUN THIS YOURSELF in Terminal — not through Claude. It prints your real tokens,
# and anything run through the assistant ends up in the transcript.
#
#   bash ops/print-env-block.sh
#
# Then: aws ssm start-session --target i-03edf0fbf2e4d601a
#       ...and paste the output.

set -euo pipefail
SRC="$HOME/.dani-metricool.env"

[ -f "$SRC" ] || { echo "missing $SRC" >&2; exit 1; }
# shellcheck disable=SC1090
set -a; . "$SRC"; set +a

# The TrueFoundry gateway vars live in ~/.claude/settings.json under "env", not in
# any shell profile — Claude Code injects them into its own subshell. So a plain
# Terminal has never seen them and neither does this script unless we read the file.
if [ -z "${ANTHROPIC_BASE_URL:-}" ] || [ -z "${ANTHROPIC_AUTH_TOKEN:-}" ]; then
  for cfg in "$HOME/.claude/settings.local.json" "$HOME/.claude/settings.json"; do
    [ -f "$cfg" ] || continue
    eval "$(python3 - "$cfg" <<'PYEOF'
import json, shlex, sys
try:
    env = (json.load(open(sys.argv[1])).get("env") or {})
except Exception:
    sys.exit(0)
for k in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"):
    v = env.get(k)
    if v:
        print(f"export {k}={shlex.quote(str(v))}")
PYEOF
)"
  done
fi

missing=()
for v in METRICOOL_TOKEN METRICOOL_USER_ID METRICOOL_BLOG_ID; do
  [ -n "${!v:-}" ] || missing+=("$v (from $SRC)")
done
for v in ANTHROPIC_BASE_URL ANTHROPIC_AUTH_TOKEN; do
  [ -n "${!v:-}" ] || missing+=("$v (not in shell env or ~/.claude/settings.json)")
done
if [ ${#missing[@]} -gt 0 ]; then
  printf 'missing:\n' >&2
  printf '  - %s\n' "${missing[@]}" >&2
  exit 1
fi

cat <<BLOCK
sudo tee /etc/dani/dani.env >/dev/null <<'ENV'
METRICOOL_TOKEN=${METRICOOL_TOKEN}
METRICOOL_USER_ID=${METRICOOL_USER_ID}
METRICOOL_BLOG_ID=${METRICOOL_BLOG_ID}
ANTHROPIC_BASE_URL=${ANTHROPIC_BASE_URL}
ANTHROPIC_AUTH_TOKEN=${ANTHROPIC_AUTH_TOKEN}
ENV
sudo chown root:dani /etc/dani/dani.env
sudo chmod 640 /etc/dani/dani.env
echo "written:"; sudo ls -l /etc/dani/dani.env
BLOCK
