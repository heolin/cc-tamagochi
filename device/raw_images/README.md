# The mascot artwork goes here

This directory is empty on purpose. The 29 mascot poses come from the
**Claude Mascot Pack** by
[getillustrations.com](https://getillustrations.com/illustration-pack/claude-mascot-pack),
which is not ours to redistribute, so the PNGs are not committed — only the
`.spr` files converted from them, in [`../sprites/`](../sprites).

**You do not need these images to run the buddy.** `device/sprites/*.spr` is
committed and `./deploy.sh sprites` uploads it as-is. Download the pack only if
you want to re-cut the sprites, change the artwork, or add a pose.

## If you do want to convert them yourself

1. Get the pack from the link above and unpack the PNGs into this directory.
2. Name each file `claude_<pose>.png`. The converter strips the `claude_`
   prefix, and `device/main.py` looks the rest up by name:

   ```
   claude_api.png  claude_app.png  claude_children_agents.png  claude_cleaning.png
   claude_coding.png  claude_disconnected.png  claude_dizzy.png  claude_drawer.png
   claude_everything_is_burning.png  claude_fixing.png  claude_food.png
   claude_happy.png  claude_heart_broken.png  claude_idea.png  claude_loading.png
   claude_love.png  claude_normal.png  claude_reading.png  claude_ready_for_work.png
   claude_really_happy.png  claude_refreshing.png  claude_rocket.png
   claude_server.png  claude_sleeping.png  claude_snack.png  claude_talking.png
   claude_waiting.png  claude_warning.png  claude_working_hard.png
   ```

3. Convert and upload:

   ```bash
   python3 tools/sprite_convert.py --ascii              # look before you upload
   python3 tools/sprite_convert.py --write device/sprites
   ./device/deploy_sprites.sh
   ```

The converter samples the centre of each art cell rather than resizing, fits
the pixel grid per file, and anchors every frame on the mascot's body so the
poses do not jump when the mood changes. `tools/sprite_convert.py` explains why
in its own docstring; `bridge/smoke.py` checks that no pose the game can pick is
missing a sprite.

Anything you drop in here stays out of git — `.gitignore` covers `*.png` in this
directory, so your copy of the pack cannot be committed by accident.
