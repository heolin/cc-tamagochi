#!/usr/bin/env bash
# Where the stick is, on whichever system this is. Sourced, not run.
#
#   source "$(dirname "$0")/port.sh"    # sets M5_PATTERN, defines find_port
#
# The board is a native USB-CDC device, so its name is the platform's
# convention rather than anything about the hardware:
#
#   Linux   /dev/ttyACM0        - ACM, not USB: the S3 speaks USB itself, there
#                                 is no CP2102/CH340 bridge to be ttyUSB
#   macOS   /dev/cu.usbmodem*   - and NOT /dev/tty.usbmodem*, which blocks on
#                                 open until carrier detect and hangs forever
#                                 with no error, reading exactly like a dead
#                                 board
#
# The number is not stable on either: it follows enumeration order, and the node
# disappears and comes back on every reset. So this globs rather than hardcodes.
#
# `M5_PORT` overrides everything, which is what a second stick on one desk
# needs. bridge/host.py answers the same question for the Python half.

case "$(uname -s)" in
    Darwin) M5_PATTERN="/dev/cu.usbmodem*" ;;
    *)      M5_PATTERN="/dev/ttyACM*" ;;
esac

# The first port that looks like the stick, or nothing at all. Callers decide
# whether an empty answer is fatal - deploy_sprites.sh needs a port, while a
# --help does not.
find_port() {
    if [[ -n "${M5_PORT:-}" ]]; then
        printf '%s' "$M5_PORT"
        return 0
    fi

    local candidate
    for candidate in $M5_PATTERN; do
        # An unmatched glob comes back as the pattern itself, so test the file.
        [[ -e "$candidate" ]] || continue
        printf '%s' "$candidate"
        return 0
    done
    return 1
}

# One message, in one place, so every script blames the same thing.
no_port() {
    echo "No board found: nothing matches $M5_PATTERN - is the stick plugged in?" >&2
    echo "Override with M5_PORT=/dev/... if it is somewhere else." >&2
}
