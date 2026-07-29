#!/usr/bin/env bash
# Set up both halves of the Claude buddy from a clean checkout.
#
#   ./deploy.sh              # sprites, host environment, tests
#   ./deploy.sh sprites      # convert and upload artwork only
#   ./deploy.sh bridge       # host venv and tests only
#   ./deploy.sh service      # run the bridge as a systemd user service
#   ./deploy.sh app          # install main.py into flash so it runs standalone
#
# Note that `all` does NOT install the app into flash. While developing,
# ./run.sh ships main.py to RAM in one command and leaves the board's flash
# alone; writing it to flash is a separate decision, taken once the buddy is
# meant to live on the desk without a terminal.
#
# Idempotent: safe to re-run after changing sprites or code.
#
# Deliberately does NOT edit ~/.claude/settings.json. That file holds your hooks
# and permissions; a deploy script quietly rewriting it is not a trade worth
# making, so this checks and prints what to add.
#
# Override the port:  M5_PORT=/dev/ttyACM1 ./deploy.sh

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$HERE"
BUDDY="$HERE/device"
BRIDGE="$HERE/bridge"
SETTINGS="$HOME/.claude/settings.json"

WHAT="${1:-all}"

say() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }
warn() { printf '\033[33m!! %s\033[0m\n' "$1"; }

# --- sprites ---------------------------------------------------------------

do_sprites() {
    say "Converting sprites"
    if ! python3 -c "import PIL" 2>/dev/null; then
        warn "Pillow missing. Install it with:  uv pip install --system pillow"
        warn "or run:  cd $BRIDGE && uv pip install pillow"
        return 1
    fi
    python3 "$REPO/tools/sprite_convert.py" --write "$BUDDY/sprites" | tail -3

    say "Uploading sprites to the board"
    "$BUDDY/deploy_sprites.sh"
}

# --- bridge ----------------------------------------------------------------

do_bridge() {
    say "Host environment"
    if ! command -v uv >/dev/null; then
        warn "uv not found - install it, or make a venv by hand and pip install bleak"
        return 1
    fi

    cd "$BRIDGE"
    [[ -d .venv ]] || uv venv
    uv pip install -e . | tail -2

    say "Tests (no hardware needed)"
    .venv/bin/python smoke.py | tail -3
}

# --- status line -----------------------------------------------------------

check_statusline() {
    say "Status line"
    if [[ ! -f "$SETTINGS" ]]; then
        warn "$SETTINGS does not exist yet"
    elif python3 -c "
import json, sys
try:
    data = json.load(open('$SETTINGS'))
except Exception:
    sys.exit(2)
sys.exit(0 if data.get('statusLine') else 1)
" 2>/dev/null; then
        echo "registered"
        return 0
    fi

    # Rate limits reach us nowhere else - they are HTTP response headers that
    # live in memory and never touch the transcripts - so without this the
    # hunger bar never moves.
    warn "not registered: the buddy will see no tokens and no rate limits"
    cat <<EOF

Add this top-level key to $SETTINGS, beside "hooks":

  "statusLine": {
    "type": "command",
    "command": "/usr/bin/python3 $BRIDGE/statusline.py"
  }

EOF
}

# --- run -------------------------------------------------------------------

do_app() {
    say "Installing the app into flash"
    shift || true
    "$BUDDY/deploy_app.sh" "$@"
}

# --- service ---------------------------------------------------------------

UNIT_DIR="$HOME/.config/systemd/user"
UNIT="cc-tamagochi.service"

do_service() {
    say "Installing the systemd user unit"

    if [[ ! -x "$BRIDGE/.venv/bin/python" ]]; then
        warn "no venv yet - run ./deploy.sh bridge first"
        return 1
    fi

    mkdir -p "$UNIT_DIR"
    # Paths are substituted rather than hardcoded so moving the repo is a
    # re-run of this script instead of a hand edit of a file in ~/.config.
    sed "s|@BRIDGE@|$BRIDGE|g" "$BRIDGE/$UNIT.in" > "$UNIT_DIR/$UNIT"
    echo "wrote $UNIT_DIR/$UNIT"

    systemctl --user daemon-reload
    systemctl --user enable --now "$UNIT"

    echo
    systemctl --user --no-pager --lines=0 status "$UNIT" || true

    cat <<EOF

  systemctl --user status cc-tamagochi     what it is doing
  journalctl --user -u cc-tamagochi -f     follow the log
  systemctl --user restart cc-tamagochi    after changing bridge code
  systemctl --user disable --now cc-tamagochi

By default a user unit runs only while you are logged in. To keep the buddy
alive across logouts and from boot, enable lingering - it needs root, so run it
yourself:

  sudo loginctl enable-linger $USER

EOF
}

case "$WHAT" in
    sprites) do_sprites ;;
    bridge)  do_bridge ;;
    app)     do_app "$@" ;;
    service) do_service ;;
    all)     do_sprites; do_bridge; check_statusline ;;
    *)       echo "usage: $0 [all|sprites|bridge|app|service]" >&2; exit 2 ;;
esac

if [[ "$WHAT" == "all" ]]; then
    cat <<EOF

$(printf '\033[1m== Ready ==\033[0m')

Two terminals:

  cd $BUDDY  && ./run.sh
  cd $BRIDGE && .venv/bin/python bridge.py --log-level DEBUG

The clock jumping from 1970 to the real time is the proof the link works.
EOF
fi
