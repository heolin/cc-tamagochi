# cc-tamagochi

A Tamagotchi that lives on an M5StickC S3 and feeds on your Claude Code usage.

Spend tokens and it eats. Leave the machine idle overnight and it wakes up
hungry. Pet it once an hour, level it up, and it grows from a bare little
creature into one with a rocket. Neglect it for a few days and it dies — and
only you can bring it back.

<p align="center">
  <img src="docs/screens/main.png" width="200" alt="The main screen: clock, battery, the mascot, name and level, five hearts, hunger and happiness bars">
  &nbsp;&nbsp;
  <img src="docs/screens/level.png" width="200" alt="The level screen: level, progress bar, today's goals and click count">
  &nbsp;&nbsp;
  <img src="docs/screens/claude.png" width="200" alt="The Claude screen: 5-hour and 7-day limit bars, live session counts, tokens today">
</p>

Nothing is flashed over the stock firmware: this is MicroPython running on top
of UIFlow2, so the device stays a normal M5StickC S3 that you can put back to
work at any time.

---

## What you need

- An **M5StickC S3** running UIFlow2 (any recent version; tested on v2.4.5)
- **Linux** with Bluetooth — the bridge uses BlueZ
- **Claude Code**, on a Pro or Max plan if you want the rate-limit bars
- [`uv`](https://docs.astral.sh/uv/) and [`mpremote`](https://docs.micropython.org/en/latest/reference/mpremote.html)
  (`uv tool install mpremote`)

## Install

```bash
git clone https://github.com/<you>/cc-tamagochi
cd cc-tamagochi
./deploy.sh
```

That uploads the artwork to the stick, sets up the host environment and runs the
tests. It finishes by printing a `statusLine` block — **add it to
`~/.claude/settings.json`** as a new top-level key, beside `hooks`:

```json
"statusLine": {
  "type": "command",
  "command": "/usr/bin/python3 /path/to/cc-tamagochi/bridge/statusline.py"
}
```

This is the only way the buddy learns how many tokens you have spent and how
much of your rate limit is left; those numbers exist nowhere else on disk.

Then start the bridge as a background service, and put the app on the stick:

```bash
./deploy.sh service              # runs at login, restarts on failure
cd device && ./deploy_app.sh     # so the stick runs without a terminal
```

`deploy_app.sh --autostart` also makes the board boot straight into the buddy
instead of the UIFlow2 menu. If you would rather keep it as a normal UIFlow2
device, skip that flag and run `./run.sh` from a terminal when you want the pet.

To survive logouts and reboots, allow the service to linger:

```bash
sudo loginctl enable-linger $USER
```

## Using it

**BtnB** — the small button on the side — moves between five screens. The dots
under the title show where you are.

**BtnA** — the big one on the front — taps the buddy for a point of EXP on the
main screen. A hundred taps a day, then it politely declines.

### Buddy

<img src="docs/screens/main.png" width="180" align="right" alt="Main screen">

Clock and battery along the top. The mascot's pose is its mood: working,
hungry, delighted, asleep, or the pose it has earned at its level.

**Five hearts** are its life. **The green bar is hunger**, the yellow one
**happiness**. Bottom corners: how much of your 5-hour limit is gone, and the
tokens you have spent today.

<br clear="right">

### Feeding

<img src="docs/screens/feeding.png" width="180" align="right" alt="Feeding screen">

You do not feed it directly — **you feed it by working**. Every hour adds 2000
tokens to its appetite, and every token you spend in Claude Code pays that down.

`to feed` is how many tokens would fill the bar right now. Eight idle hours empty
it completely, which is why it is always hungry in the morning.

<br clear="right">

### Petting

<img src="docs/screens/petting.png" width="180" align="right" alt="Petting screen">

**Tilt the stick** to bring the paw down onto its head, then lift it away —
three times. The three dots at the top count your strokes.

Petting is always available and the buddy always reacts, but **only the first
petting each hour gives it happiness**. Happiness drains by 2% an hour, so it
wants attention roughly once an hour.

<br clear="right">

### Level

<img src="docs/screens/level.png" width="180" align="right" alt="Level screen">

EXP comes from three places: tokens you spend, each petting, and taps on BtnA.
The mascot's pose on this screen is always the one for its level — it collects a
lightbulb, a keyboard, an API, an app, a server, then agents of its own, and
finally a rocket.

Below: **today's two goals**. Feed it and pet it at least once each day. Both
done at midnight earns half a heart. Missing either costs a whole one.

<br clear="right">

### Claude

<img src="docs/screens/claude.png" width="180" align="right" alt="Claude screen">

Your 5-hour and 7-day limits as bars, how many Claude Code sessions are alive
and how many are busy, and today's tokens. `--` instead of a percentage means
the limit is not known yet: it appears after the first API response of a
session, and only on Pro and Max plans.

<br clear="right">

### When something is wrong

<p>
  <img src="docs/screens/disconnected.png" width="180" alt="Disconnected screen">
  &nbsp;&nbsp;
  <img src="docs/screens/dead.png" width="180" alt="Dead screen">
</p>

If the bridge stops, the buddy says so and the buttons go quiet — every other
screen would be showing numbers that stopped being true.

If it runs out of hearts it dies, and stays dead until you bring it back:

```bash
./bridge/buddyctl.py status    # hearts, hunger, happiness, goals
./bridge/buddyctl.py reset     # start again
```

## The rules, in one table

| | |
|---|---|
| Hunger | +2000 tokens of appetite an hour; spending tokens feeds it. Empty after 8 idle hours |
| Happiness | +30% per petting, once an hour. −2% an hour otherwise |
| Hearts | 5. Both daily goals met at midnight: +½. Either missed: −1 |
| Daily goals | feed it at least once, pet it at least once |
| EXP | 1 per 5000 tokens, 20 per petting, 1 per tap (100 a day) |
| Levels | 100 EXP for the first, 35% more each time after |
| Death | 0 hearts. `buddyctl.py reset` is the only way back |

Every number here lives in [`bridge/buddy_config.json`](bridge/buddy_config.json)
and can be changed without touching code. The pet's name too.

---

## For developers

### How it fits together

```text
statusLine ─┐
            ├─▶ bridge ──BLE / Nordic UART──▶ device
sessions/ ──┘   game + state    ◀──BLE──      screens + buttons
                buddy.json
```

The **bridge owns the game**; the device is a view with two buttons. The stick's
clock starts at 1970, `run.sh` restarts it constantly and nothing survives that
restart, so hourly decay, the midnight rollover and levels all live on the host
and persist to `buddy.json`.

The pet is **purely observational** — no blocking hooks, nothing that can slow a
tool call down. Two read-only sources cover everything:

| what | where from |
|---|---|
| rate limits, tokens, cost, context | the `statusLine` command's stdin |
| live sessions and busy/idle | `~/.claude/sessions/*.json` |
| wall clock | sent by the bridge on connect |

### Layout

| | |
|---|---|
| `device/main.py` | one file: BLE peripheral, sprite renderer, five screens, tilt mini-game |
| `bridge/game.py` | hunger, happiness, hearts, levels, persistence |
| `bridge/state.py` | usage aggregation, mood and pose selection |
| `bridge/statusline.py` | one shot per prompt redraw, standard library only |
| `tools/sprite_convert.py` | PNG → the device's sprite format |
| `tools/screenshot.py` | the images in this README, drawn by the real code |
| `tools/*_probe.py` | what this board can actually do, measured |

### Tests

```bash
cd bridge && python3 smoke.py
```

29 checks, no hardware: the status-line path end to end, usage aggregation, mood
and level selection, the daily rollover, and that every pose the code names has
a sprite on disk. It runs happily alongside a live bridge — set
`CC_BUDDY_SOCKET` and it uses its own socket.

`tools/screenshot.py` is the other useful check: it renders every screen from
`device/main.py` itself, so a layout mistake is visible without a stick.

### Sprites

The mascot art in `device/raw_images/` is pixel art upscaled by a non-integer
factor, which makes two things go wrong if you just resize it. Averaging blurs
2-colour art into mush, so the converter **samples the centre of each cell**
instead; and the grid phase differs per frame, so it fits each file to its own
grid rather than assuming a shared one.

The poses were also not drawn on a common baseline — the mascot sat at heights
spanning eleven pixels — so each frame is anchored on the largest connected blob
of `#D97757`, which is the animal itself and never a prop.

```bash
python3 tools/sprite_convert.py --png /tmp/preview   # look before uploading
python3 tools/sprite_convert.py --write device/sprites
```

`/tmp/preview/_sheet.png` shows every pose with a red feet baseline and a blue
spine. All 29 come to about 8.8 KB.

### The hardware, measured

Worth knowing before adding features, because `dir()` lies by omission —
several of these APIs accept calls and quietly do nothing:

| | |
|---|---|
| LED | **none.** `M5.Led.getCount()` is 0 and `M5.Power.setLed()` lights nothing |
| Vibration | **none** |
| Light sensor | **none** — a constant 0 in any lighting |
| Proximity sensor | works, `0`…`1792` |
| Speaker, IMU, battery | work |
| `M5.Rtc` | absent; `machine.RTC` works but starts at 1970 |
| Buttons | debounce themselves — use `wasClicked()`, not a hand-rolled timer |
| Fonts | nothing outside `0x20-0x7F`; a missing glyph draws as a box |

So the only output channels are the screen and the speaker, and every icon here
is drawn with primitives rather than typed.

Two MicroPython traps worth repeating: the GATT value buffer defaults to **20
bytes and truncates silently**, so `gatts_set_buffer` is not optional; and
`gatts_notify` sends one packet, dropping anything past the MTU rather than
fragmenting it.

Tilt axes are measured, not derived — `tools/tilt_probe.py` shows all three
axes live, which settles both the axis and its sign in one look.
