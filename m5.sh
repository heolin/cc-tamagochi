#!/usr/bin/env bash
# Thin wrapper around mpremote for this board.
#
# Adds the two things every command needs anyway: the port, and `resume`
# (without which mpremote soft-resets the board and fails to enter the raw REPL).
#
# Anything not listed below is passed straight through to mpremote.
# Override the port with M5_PORT=/dev/ttyACM1 ./m5.sh ...

set -euo pipefail

PORT="${M5_PORT:-/dev/ttyACM0}"
WAIT_TIMEOUT=15

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
BACKUP_DIR="$REPO_ROOT/backup"

usage() {
    cat <<'EOF'
usage: ./m5.sh <command> [args]

  reset               reboot the board, then wait for USB to come back
  wait                wait for the port to appear
  identity [own|clone]
                      show or set which capsule test2/test3 advertise as.
  mode [tilt|yaw]     show or set what the joystick's X axis drives.
                      "yaw" for the official game (it steers on rotation),
                      "tilt" for Kapsel Hub (it steers on the gravity vector).

                      Both are stored in NVS, so they survive a reboot and need
                      no re-deploy. They take effect on the next restart.
  deploy <file.py>    back up the board's /main.py, install <file.py> in its
                      place, and reset. Autostart also needs boot_option = 0
                      (see README.md).
  repl                interactive console (exit with Ctrl-])
  exec "print(1)"     run one line
  run script.py       run a file from RAM
  fs ls               list files in flash
  fs cp a.py :main.py copy into flash
EOF
    exit "${1:-0}"
}

# A reset re-enumerates USB, so the device node disappears and is recreated a
# second or two later. Without this wait the next command races the kernel.
wait_for_port() {
    local waited=0
    while [[ ! -e "$PORT" ]]; do
        if (( waited >= WAIT_TIMEOUT )); then
            echo "Timed out waiting for $PORT" >&2
            return 1
        fi
        sleep 0.5
        waited=$((waited + 1))
    done

    if [[ ! -r "$PORT" || ! -w "$PORT" ]]; then
        echo "Warning: $PORT is not readable/writable by you." >&2
        echo "The node was just recreated as root:dialout - see README.md." >&2
    fi
}

require_port() {
    if [[ ! -e "$PORT" ]]; then
        echo "No such port: $PORT - is the stick plugged in?" >&2
        exit 1
    fi
}

mp() {
    mpremote connect "$PORT" resume "$@"
}

# These must match the IDENTITIES and JOY_X_MODES tuples in test2/main.py and
# test3/main.py - the board stores only the index.
IDENTITY_NAMES=("own" "clone")
MODE_NAMES=("tilt" "yaw")

do_mode() {
    local want="${1:-}"

    require_port

    if [[ -z "$want" ]]; then
        mp exec "
import esp32
try:
    i = esp32.NVS('triki').get_i32('joy_x_mode')
    print('joystick X mode =', i, ('tilt', 'yaw')[i % 2])
except Exception:
    print('joystick X mode = (unset, firmware default applies)')
"
        return
    fi

    local index=-1 i
    for i in "${!MODE_NAMES[@]}"; do
        [[ "${MODE_NAMES[$i]}" == "$want" ]] && index=$i
    done

    if (( index < 0 )); then
        echo "unknown mode: $want (expected: ${MODE_NAMES[*]})" >&2
        exit 1
    fi

    mp exec "
import esp32
nvs = esp32.NVS('triki')
nvs.set_i32('joy_x_mode', $index)
nvs.commit()
print('joystick X mode =', nvs.get_i32('joy_x_mode'), '$want')
"
    echo "Takes effect on the next restart - ./m5.sh reset, or re-run the script."
}

do_identity() {
    require_port

    local want="${1:-}"

    if [[ -z "$want" ]]; then
        mp exec "
import esp32
try:
    i = esp32.NVS('triki').get_i32('identity')
    print('identity =', i, ('own', 'clone')[i % 2])
except Exception:
    print('identity = (unset, firmware default applies)')
"
        return
    fi

    local index=-1
    local i
    for i in "${!IDENTITY_NAMES[@]}"; do
        [[ "${IDENTITY_NAMES[$i]}" == "$want" ]] && index=$i
    done

    if (( index < 0 )); then
        echo "unknown identity: $want (expected: ${IDENTITY_NAMES[*]})" >&2
        exit 1
    fi

    mp exec "
import esp32
nvs = esp32.NVS('triki')
nvs.set_i32('identity', $index)
nvs.commit()
print('identity =', nvs.get_i32('identity'), '$want')
"
    echo "Takes effect on the next restart - ./m5.sh reset, or re-run the script."
}

do_deploy() {
    local src="${1:-}"

    if [[ -z "$src" || ! -f "$src" ]]; then
        echo "usage: ./m5.sh deploy <file.py>" >&2
        exit 1
    fi

    require_port

    echo "== Files currently in the board's flash =="
    mp fs ls
    echo

    read -r -p "Install $src as /main.py on the board? [y/N] " reply
    [[ "$reply" == [yY] ]] || { echo "Aborted."; exit 0; }

    mkdir -p "$BACKUP_DIR"

    # Timestamped, so a deploy never overwrites an earlier backup. The pristine
    # factory placeholder lives in test1/backup/main.py.orig and is not touched.
    local stamp
    stamp="$(date +%Y%m%d-%H%M%S)"
    if mp fs cp :main.py "$BACKUP_DIR/main.py.$stamp" 2>/dev/null; then
        echo "Backed up the board's /main.py to backup/main.py.$stamp"
    else
        echo "No /main.py on the board - nothing to back up."
    fi

    mp fs cp "$src" :main.py
    echo "Installed $src as /main.py"

    mp reset
    wait_for_port
    echo "Board reset, $PORT is back."
}

[[ $# -eq 0 ]] && usage 1
[[ "$1" == "-h" || "$1" == "--help" ]] && usage

case "$1" in
    wait)
        wait_for_port
        ;;
    reset)
        require_port
        mp reset
        wait_for_port
        echo "Board reset, $PORT is back."
        ;;
    deploy)
        shift
        do_deploy "$@"
        ;;
    identity)
        shift
        do_identity "$@"
        ;;
    mode)
        shift
        do_mode "$@"
        ;;
    *)
        require_port
        # Not `exec mp` - mp is a shell function and exec only replaces the
        # process with a program. Spelled out so exec still applies, which
        # keeps Ctrl-C and Ctrl-] going straight to mpremote in `repl`.
        exec mpremote connect "$PORT" resume "$@"
        ;;
esac
