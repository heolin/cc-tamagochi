#!/usr/bin/env bash
# Run the buddy in the board's RAM. Writes nothing to flash.
# Stop with Ctrl-C.
#
# Override the port:  M5_PORT=/dev/ttyACM1 ./run.sh

set -euo pipefail

cd "$(dirname "$0")"
source ../port.sh

if ! PORT="$(find_port)"; then
    no_port
    exit 1
fi

exec mpremote connect "$PORT" resume run main.py
