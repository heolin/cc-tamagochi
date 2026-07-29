#!/usr/bin/env bash
# Install main.py into the board's flash so the buddy runs without a terminal.
#
#   ./deploy_app.sh              # copy and reset
#   ./deploy_app.sh --autostart  # also make the board boot straight into it
#   ./deploy_app.sh --restore    # put the original /main.py back
#
# Until this is run, ./run.sh ships main.py to RAM and the buddy dies with the
# terminal. That is the right cycle while developing; this is for the desk.
#
# What it touches, and what it does not:
#
#   /main.py   overwritten - the stock file is a 10-byte placeholder and this
#              is the slot meant for user code. Backed up to backup/ first.
#   /boot.py   never touched. That is UIFlow2's own startup.
#   sprites    not uploaded here; run ./deploy_sprites.sh for those.
#
# Override the port:  M5_PORT=/dev/ttyACM1 ./deploy_app.sh

set -euo pipefail

PORT="${M5_PORT:-/dev/ttyACM0}"
cd "$(dirname "$0")"

M5="../m5.sh"
BACKUP_DIR="backup"
BACKUP_FILE="$BACKUP_DIR/main.py.orig"

MP=(mpremote connect "$PORT" resume)

if [[ ! -e "$PORT" ]]; then
    echo "No such port: $PORT - is the stick plugged in?" >&2
    exit 1
fi

# --- restore ---------------------------------------------------------------

if [[ "${1:-}" == "--restore" ]]; then
    if [[ -f "$BACKUP_DIR/.no-original" ]]; then
        echo "There was no /main.py before us; removing ours."
        "${MP[@]}" fs rm :main.py || true
    elif [[ -f "$BACKUP_FILE" ]]; then
        "${MP[@]}" fs cp "$BACKUP_FILE" :main.py
        echo "Restored the original /main.py"
    else
        echo "No backup in $BACKUP_DIR - nothing to restore." >&2
        exit 1
    fi
    "$M5" reset
    exit 0
fi

# --- deploy ----------------------------------------------------------------

echo "== Currently in the board's flash =="
"${MP[@]}" fs ls

echo
read -r -p "Overwrite /main.py with the buddy? [y/N] " reply
[[ "$reply" == [yY] ]] || { echo "Aborted."; exit 0; }

mkdir -p "$BACKUP_DIR"

# Only ever back up once. A second deploy would otherwise overwrite the pristine
# copy with our own file and destroy the way back.
if [[ ! -f "$BACKUP_FILE" && ! -f "$BACKUP_DIR/.no-original" ]]; then
    if "${MP[@]}" fs cp :main.py "$BACKUP_FILE" 2>/dev/null; then
        echo "Backed up the board's /main.py to $BACKUP_FILE"
    else
        echo "No /main.py on the board - recording that."
        echo "ABSENT" > "$BACKUP_DIR/.no-original"
    fi
else
    echo "Backup already exists, keeping it."
fi

"${MP[@]}" fs cp main.py :main.py
echo "Copied main.py -> board:/main.py"

# --- autostart -------------------------------------------------------------

if [[ "${1:-}" == "--autostart" ]]; then
    echo
    echo "Setting boot_option = 0 (run main.py directly)."
    # At the factory default of 1, boot.py hands control to UIFlow2's cloud
    # loop, which never returns - so /main.py sits in flash and never runs.
    # See section 6 of the root CLAUDE.md, including the recovery path: hold
    # BtnA while resetting to get the startup menu back.
    "${MP[@]}" exec \
        "import esp32; nvs = esp32.NVS('uiflow'); nvs.set_u8('boot_option', 0); nvs.commit()"
    echo "Done. The board will no longer join WiFi or the UIFlow2 cloud."
    echo "Recovery without a computer: hold BtnA and reset until the menu appears."
fi

"$M5" reset
echo
echo "Board reset. The buddy should come up on its own."
echo "Sprites live in flash separately - run ./deploy_sprites.sh if you have not."
