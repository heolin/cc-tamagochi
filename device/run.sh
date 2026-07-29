#!/usr/bin/env bash
# Run the buddy in the board's RAM. Writes nothing to flash.
# Stop with Ctrl-C.
#
# Override the port:  M5_PORT=/dev/ttyACM1 ./run.sh

set -euo pipefail

PORT="${M5_PORT:-/dev/ttyACM0}"
cd "$(dirname "$0")"

if [[ ! -e "$PORT" ]]; then
    echo "No such port: $PORT - is the stick plugged in?" >&2
    exit 1
fi

exec mpremote connect "$PORT" resume run main.py
