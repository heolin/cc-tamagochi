#!/usr/bin/env bash
# Set up both halves of the Claude buddy from a clean checkout.
#
#   ./deploy.sh              # sprites, host environment, tests
#   ./deploy.sh sprites      # convert and upload artwork only
#   ./deploy.sh bridge       # host venv and tests only
#   ./deploy.sh config       # name the pet and set the difficulty, interactively
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
    # The converted .spr files are committed and the source PNGs are not - the
    # mascot pack is licensed to whoever downloaded it (device/raw_images/
    # README.md). So conversion is the optional half: without the artwork this
    # uploads what is already in the repo, which is the common case.
    if ls "$BUDDY"/raw_images/*.png >/dev/null 2>&1; then
        say "Converting sprites"
        if ! python3 -c "import PIL" 2>/dev/null; then
            warn "Pillow missing. Install it with:  uv pip install --system pillow"
            warn "or run:  cd $BRIDGE && uv pip install pillow"
            return 1
        fi
        python3 "$REPO/tools/sprite_convert.py" --write "$BUDDY/sprites" | tail -3
    else
        say "Using the committed sprites"
        echo "No source artwork in device/raw_images - see its README.md."
        echo "Nothing to convert; $(ls -1 "$BUDDY"/sprites/*.spr | wc -l) .spr files are already in the repo."
    fi

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

# --- settings --------------------------------------------------------------

do_config() {
    say "Naming the pet and setting the difficulty"
    # System python on purpose: configure.py is stdlib-only so it works before
    # ./deploy.sh bridge has built the venv - naming your pet should not be
    # gated on a dependency resolver.
    python3 "$BRIDGE/configure.py"
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
#
# Two service managers, one job: run bridge.py as you, at login, and restart it
# if it dies. Neither half needs root - a systemd *user* unit and a launchd
# *user agent* both run inside your own session, which is also where the
# Bluetooth permission and $XDG_RUNTIME_DIR/$TMPDIR live.

UNIT_DIR="$HOME/.config/systemd/user"
UNIT="cc-tamagochi.service"

AGENT_DIR="$HOME/Library/LaunchAgents"
AGENT="cc-tamagochi.plist"

do_service() {
    if [[ ! -x "$BRIDGE/.venv/bin/python" ]]; then
        warn "no venv yet - run ./deploy.sh bridge first"
        return 1
    fi

    if [[ "$(uname -s)" == "Darwin" ]]; then
        do_service_launchd
    else
        do_service_systemd
    fi
}

do_service_launchd() {
    say "Installing the launchd user agent"
    warn "UNVERIFIED: this path has never been run on a Mac - see the plist"

    mkdir -p "$AGENT_DIR" "$HOME/Library/Logs"
    sed -e "s|@BRIDGE@|$BRIDGE|g" -e "s|@HOME@|$HOME|g" \
        "$BRIDGE/$AGENT.in" > "$AGENT_DIR/$AGENT"
    echo "wrote $AGENT_DIR/$AGENT"

    # bootout first so a re-run replaces the agent rather than failing on an
    # already-loaded label. It fails when nothing is loaded, which is fine.
    launchctl bootout "gui/$UID/cc-tamagochi" 2>/dev/null || true
    launchctl bootstrap "gui/$UID" "$AGENT_DIR/$AGENT"
    launchctl enable "gui/$UID/cc-tamagochi"

    cat <<EOF

  launchctl print gui/$UID/cc-tamagochi     what it is doing
  tail -f ~/Library/Logs/cc-tamagochi.log   follow the log
  launchctl kickstart -k gui/$UID/cc-tamagochi    restart after a change
  launchctl bootout gui/$UID/cc-tamagochi         stop and unload

The first Bluetooth scan raises a permission dialog. A background agent has no
window to ask from, so approve it once by running the bridge in a terminal:

  cd $BRIDGE && .venv/bin/python bridge.py --log-level DEBUG

EOF
}

do_service_systemd() {
    say "Installing the systemd user unit"

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
    config)  do_config ;;
    app)     do_app "$@" ;;
    service) do_service ;;
    all)     do_sprites; do_bridge; check_statusline ;;
    *)       echo "usage: $0 [all|sprites|bridge|config|app|service]" >&2; exit 2 ;;
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
