#!/usr/bin/env bash
# Copy the converted sprites to the board's flash, once.
#
# main.py is still shipped from RAM by run.sh - only the artwork lives on the
# board, so changing code stays a one-command cycle. Re-run this after
# tools/sprite_convert.py regenerates sprites/.
#
# Override the port:  M5_PORT=/dev/ttyACM1 ./deploy_sprites.sh

set -euo pipefail

cd "$(dirname "$0")"
source ../port.sh

if ! PORT="$(find_port)"; then
    no_port
    exit 1
fi

if [[ ! -d sprites ]] || ! ls sprites/*.spr >/dev/null 2>&1; then
    echo "No sprites/*.spr - run this first:" >&2
    echo "    python3 ../tools/sprite_convert.py --write device/sprites" >&2
    exit 1
fi

MP=(mpremote connect "$PORT" resume)

echo "== Sprites to upload =="
ls -1 sprites/*.spr | wc -l | xargs printf "%s files, "
du -sh sprites | cut -f1

# mkdir fails if the directory is already there, which is fine and not an error
# worth stopping for.
"${MP[@]}" fs mkdir :buddy 2>/dev/null || true

for file in sprites/*.spr; do
    name="$(basename "$file")"
    printf "  %-28s" "$name"
    "${MP[@]}" fs cp "$file" ":buddy/$name"
done

echo
echo "== On the board =="
"${MP[@]}" fs ls :buddy

echo
echo "Done. Now: ./run.sh"
