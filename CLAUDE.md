# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this repo is

A Tamagotchi split across two machines. `device/main.py` is a single MicroPython
file running on a physical **M5StickC S3**; `bridge/` is a long-lived Python
daemon on the host that watches Claude Code and pushes state to the stick over
BLE. `tools/` holds host-side utilities — sprite conversion, screen renders,
hardware probes.

The **bridge owns the game**. The device is a view with two buttons: it draws
what it is sent and reports taps and tilt back. Nothing about hunger, levels or
the calendar lives on the stick, because the stick's clock starts at 1970 and
`run.sh` restarts it constantly.

## Language convention

**All files are written in English** — code, comments, identifiers, docstrings,
commit messages, documentation. Conversation with the user is in Polish.

## Hardware and firmware — verified on the device, do not re-guess

| Property | Value | How it was verified |
|---|---|---|
| Board | M5STACK StickS3, board id `26` | `M5.getBoard()` |
| SoC | ESP32-S3-PICO-1 | `os.uname().machine` |
| Screen | 135 x 240 px, portrait | `Lcd.width()`, `Lcd.height()` |
| Firmware | UIFlow2 **v2.4.5** | boot banner |
| Runtime | MicroPython **1.27.0**, build `M5STACK_StickS3` | `sys.implementation` |
| USB id | `303a:832b` | `lsusb` |
| Serial port | `/dev/ttyACM0` | native USB-CDC of the ESP32-S3 |
| BLE address | `98:88:E0:0E:8C:CE` | `bridge/ble.py`, `DEFAULT_ADDRESS` |

The board is **`ttyACM`, not `ttyUSB`** — the S3 speaks USB natively, there is no
CP2102/CH340 bridge. The port disappears whenever the chip resets.

## Toolchain

`mpremote` (installed via `uv tool install mpremote`). There is **no compiler and
no build step** — the firmware already contains a MicroPython interpreter, and we
only ship `.py` files to it. `mpremote` talks to the *running* interpreter; it
cannot flash firmware.

| Command | What it does |
|---|---|
| `./m5.sh <anything>` | `mpremote` with the port and `resume` supplied |
| `device/run.sh` | ship `main.py` to RAM and run it — the development cycle |
| `device/deploy_app.sh` | write `main.py` into flash, for the desk |
| `device/deploy_sprites.sh` | upload `device/sprites/*.spr` |
| `./deploy.sh [sprites\|bridge\|config\|app\|service]` | set up either half from a clean checkout |
| `bridge/configure.py` | interactive `buddy_config.json`: name, appetite, difficulty |
| `bridge/smoke.py` | the whole host-side test suite. No hardware needed |
| `tools/screenshot.py` | regenerate `docs/screens/*.png` from `device/main.py` |

`m5.sh` still carries `identity` and `mode` subcommands from the TRIKI capsule
work this stick was doing before. They write an unrelated NVS namespace and do
nothing for the buddy — leftovers, not features.

The bridge runs as a systemd **user** unit (`./deploy.sh service`). After
changing anything under `bridge/`, it needs `systemctl --user restart
cc-tamagochi`; `./deploy.sh service` re-installs the unit but will not restart a
service that is already active.

## Two platforms, one branch

Linux is what this was built on; macOS is supported and **unverified on
hardware**. Everything that differs lives in `bridge/host.py` (process liveness,
runtime directory, serial port names) and `port.sh` (the same for the shell
scripts). Do not add `sys.platform` or `uname` anywhere else - the point of
those two files is that the list of differences is readable in one sitting.

The pattern to follow when adding one: put the *logic* in a pure function that
turns text into a value, and let only the *decision* between them look at the
platform. A branch that never runs here is a branch that rots, so `smoke.py`
feeds both parsers fixture strings and checks both regardless of the host OS.
`runtime_dir()` and `serial_patterns()` take a `macos=` argument for the same
reason.

Two things genuinely cannot be settled from Linux, and both are marked
**UNVERIFIED** in the code: whether BSD `ps -o lstart=` prints what the parser
expects, and what Claude Code writes into `procStart` on macOS. If sessions
count as zero on a Mac, start there.

Do not name a module after a stdlib one. `bridge/host.py` was called
`platform.py` for ten minutes, and since every script puts its own directory
first on `sys.path`, it shadowed the real `platform` for the whole process -
`import bleak` died inside `uuid` with `module 'platform' has no attribute
'system'`.

## Critical gotchas

### 1. Always pass `resume`

```bash
mpremote connect /dev/ttyACM0 resume <command>
```

Without `resume`, mpremote sends a soft reset (Ctrl-D) before entering the raw
REPL. That reboots the board, the UIFlow2 app starts up and grabs stdin, and
mpremote fails with `could not enter raw repl`. `./m5.sh` supplies it.

### 2. `could not exec command (response: b'R\x01')` is a race, not a fault

The raw-REPL handshake occasionally slips when the board is busy. **Retry the
same command** — it succeeds. Do not start debugging the hardware over this.

### 3. Serial port permissions

`/dev/ttyACM0` is `root:dialout`. Group membership is fixed at login, so a fresh
`usermod` does not apply to a running session; `newgrp dialout` fixes one
terminal, a logout fixes it for good. `chmod a+rw` survives only until the node
is recreated, and any reset re-enumerates USB.

When mpremote says `failed to access /dev/ttyACM0 (it may be in use by another
program)`, suspect permissions first — it prints that for any failed open,
`EACCES` included.

There is **no passwordless sudo** on this machine and the user does not want it.
Anything needing root is handed to the user to run.

### 4. `M5.begin()` and `M5.update()`

`M5.begin()` must be the first call — it powers the screen and configures pins.
`M5.update()` must run every loop iteration or no button input is ever
registered; there is no OS to deliver events.

### 5. `/main.py` on the board is the user slot — safe to overwrite

The stock `/main.py` is a 10-byte placeholder. UIFlow2's real startup is
`/boot.py`, which runs first and must not be touched. `deploy_app.sh` backs the
original up once, to `device/backup/`, and never overwrites that backup.

A deployed `/main.py` runs at boot and keeps the REPL busy. That is fine because
the main loop catches `KeyboardInterrupt`, so `./m5.sh repl` plus Ctrl-C can
always regain control.

### 6. `boot_option` decides whether `/main.py` ever runs

Read out of the board's own `/boot.py`:

| Value | Meaning |
|---|---|
| `0` | Run `main.py` directly |
| `1` | Show startup menu and network setup (**factory default**) |
| `2` | Only network setup |

At the default `1`, `/boot.py` hands control to UIFlow2's cloud loop, which never
returns — the file sits in flash and never gets a turn. `deploy_app.sh
--autostart` sets it to `0`; `commit()` is required or the write may not survive
a reboot. At `0` the board no longer joins WiFi or the UIFlow2 cloud.

**Recovery without a computer:** hold BtnA and reset until the startup menu
appears. The setting cannot lock you out.

### 7. No built-in font has Polish diacritics

Verified on the device: every `DejaVu*`, `Montserrat*` and CJK face renders
`ą ć ę ł ń ó ś ź ż` as a replacement box. The one hand-edited string that can
contain them is the pet's name in `buddy_config.json`, so `main.py` folds them to
ASCII — wrong spelling is at least readable.

Custom `.vlw` fonts do work and are the firmware's intended route. Generate at
https://vlw-font-creator.m5stack.com/ with **both** the `0x20-0x7F` range and the
Polish symbols; symbols alone yield a font where only the diacritics render. Load
with `Lcd.loadFont("/flash/res/font/<name>.vlw")`, free with `Lcd.unloadFont()`.

There is also no heart, apple or smiley glyph to reach for — every icon on the
screens is drawn with primitives or a bitmap for this reason.

Do not detect missing glyphs by width: LovyanGFX substitutes a fixed-width box
rather than reporting zero, and a narrow letter can match that width by
coincidence.

### 8. Rate limits exist in exactly one place

The 5-hour and 7-day windows arrive as HTTP response headers, live in memory and
never reach the JSONL transcripts. The status line's stdin is the one documented
way out, which is why `statusline.py` exists and why it must stay registered in
`~/.claude/settings.json`. Nothing else can recover them.

They are also absent for non-subscription accounts and until the first API
response of a session, and each window can be missing on its own. **Unknown must
stay unknown** — the pet shows `--`, never a confident `0`.

The status line runs on every redraw of the prompt, so its socket write is best
effort with a 0.15 s timeout, and every failure path still prints a line. It uses
the system interpreter and stdlib only: a broken venv must not be able to break
the prompt.

### 9. An axis is measured, not derived

The IMU's frame and the case's orientation are set independently, so nothing
about the screen being portrait tells you which of `ax`/`ay`/`az` a gesture lands
on, or which sign it carries. The joystick work on this stick found the same
thing the hard way — its X grew towards the *left*, and its rest position drifted
16 counts between readings, so even "centred" was a measurement rather than a
constant.

See §14 for the tool that answers this in ten seconds.

### 10. Session files outlive their process

`~/.claude/sessions/<pid>.json` is one file per session, and a crashed or
SIGKILLed session leaves its file behind. Liveness has to be checked, not
assumed, or the pet shows a permanent crowd of sessions that ended days ago.

`sessions.py` checks two things: that the pid exists, and that field 22 of
`/proc/<pid>/stat` matches the recorded `procStart` — pids are reused, and the
start time is what makes the check trustworthy. A file that has not been touched
in 15 minutes is treated as gone regardless; a wedged process should not read as
working.

### 11. `total_output_tokens` is a gauge, not a counter

It counts what is in the context window *right now*, so it collapses whenever the
conversation is compacted. Read directly it produced a token count that jumped
around and shrank — 1k, 8, 2k. `state.py` turns each reading into growth instead:
the difference from the last one, or the whole value after a drop.

For the same reason, usage for finished sessions is **kept** until it ages out
(26 h), and the screens show `tokens_today` from the game rather than the raw
cross-session sum. A number that falls when you close a terminal reads as work
being undone.

### 12. A wedged peripheral does not recover by being asked more nicely

The precedent is the ESP32's I2C controller: after one failed transaction every
subsequent read returned `OSError: 259` whether or not the device was still
plugged in, and it read exactly like hardware that had vanished. Retrying never
recovered it — only constructing a new bus did.

`ble.py` is built on that lesson: after `WRITE_FAIL_LIMIT` consecutive write
failures the `BleakClient` is torn down and rebuilt rather than retried.

### 13. Everything on the BLE wire is a JSON line, and it fragments

The stick is the peripheral and GATT server; the bridge is the central. Both
directions carry UTF-8 JSON, one object per line, over the Nordic UART Service.
Notifications fragment at the MTU, so incoming bytes are accumulated until a
newline and outgoing writes are chunked — 20 bytes when the backend will not
report the negotiated MTU, which is what fits the 23-byte ATT default.

The bridge pushes a full snapshot on a 3 s heartbeat and immediately after
anything interesting, so the device never computes state it could be told. It
also sends the wall clock, because `machine.RTC` starts at 1970 — the clock
jumping to the real time is the proof the link works.

Adding a field to the snapshot costs nothing on the device: `on_message` copies
every key it is sent. An earlier version iterated over the existing keys and
silently dropped everything added later.

### 14. Tilt axis: measure it, every time

Held upright with the screen in portrait, a **forward/backward tilt lands on
`ax`, and downward motion is negative**. Confirmed by hand while building the
petting mini-game — after three wrong guesses, each a plausible argument from the
screen's orientation.

`tools/tilt_probe.py` answers it in ten seconds: three live bars, one per axis,
centred at rest. Hold the device the way the feature will be held, make the
gesture, and read which bar swings and which way. Both the axis and the sign come
out of one look.

### 15. Half the output APIs drive nothing, and say so by succeeding

Measured with `tools/buddy_probe.py`, `tools/led_probe.py` and
`tools/indicator_probe.py`. The board exposes a full `dir(M5)`, but this revision
does not carry the hardware behind several of those names — and the calls
**return cleanly anyway**:

| API | present | behaviour |
|---|---|---|
| `M5.Led.setAllColor()` / `setColor()` | **no** | `getCount()` is `0`; writes to an empty strip return fine |
| `M5.Power.setLed()` | **no** | accepted, nothing lights up |
| `M5.Power.setVibration()` | **no** | accepted, no motor |
| `M5.Als.getLightSensorData()` | **no** | returns a constant `0`, in any lighting |
| `M5.Als.getProximitySensorData()` | yes | `0` … `1792`, responds to a hand |
| `M5.Speaker.tone()` | yes | audible |
| `M5.Imu`, `M5.Power` battery methods | yes | used by the buddy |

**A call that does not raise is not evidence of hardware.** `getCount() == 0` was
the real test for the LED; 30 samples with no variance was the real test for the
light sensor. So: **the only output channels are the screen and the speaker.**

`M5.Rtc` does not exist either; use `machine.RTC`.

**Buttons debounce themselves.** `M5.BtnA` exposes `wasClicked`, `wasPressed`,
`wasDoubleClicked`, `wasHold`, `isHolding`, `getClickCount`, `setDebounceThresh`
and `setHoldThresh`. Hand-rolled debounce classes elsewhere on this stick predate
checking that, and are not the pattern to copy.

### 16. The screenshots are renders, not photographs

`tools/screenshot.py` stubs `M5`, `Lcd` and `bluetooth`, imports the real drawing
functions out of `device/main.py`, and feeds them made-up state. Layout, colours,
sprites and every coordinate are therefore exactly what the device does — a
screenshot cannot drift from the code, because it is produced by the code.

The one approximation is the font: Pillow's default bitmap face stands in, while
`fontHeight()` and `textWidth()` return the device's real metrics (8 px and 6 px
per character at size 1). Letterforms are close, not identical; descenders can
look clipped in the PNG and are fine on the hardware.

Re-run it after any layout change and commit the PNGs — the README embeds them.

## Verified API surface

`dir(M5)` → `begin`, `update`, `end`, `getBoard`, `Lcd`, `Display`, `Displays`,
`Widgets`, `BtnA`, `BtnB`, `BtnC`, `BtnEXT`, `BtnPWR`, `Imu`, `Power`, `Led`,
`Speaker`, `Mic`, `Als`, `Touch`, `UserDisplay`.

`dir(M5.Lcd)` → drawing primitives (`fillScreen`, `fillRect`, `drawLine`,
`drawCircle`, `fillCircle`, `drawTriangle`, `drawRoundRect`, `drawArc`, `drawQR`,
`drawPng`, `drawJpg`, `drawBmp`), text (`drawString`, `drawCenterString`,
`drawRightString`, `print`, `printf`, `setCursor`, `setTextColor`, `setTextSize`,
`setFont`, `textWidth`, `fontHeight`, `loadFont`), and display control
(`setBrightness`, `setRotation`, `setColorDepth`, `newCanvas`, `powerSaveOn`).

Colours are plain `0xRRGGBB` integers.

Anything not on that list is unverified rather than known-good — `fillRoundRect`
was never confirmed present, so the drawing code sticks to `fillRect` and
`fillCircle`. An `AttributeError` on the board surfaces at runtime, in the middle
of a loop, not at deploy time.

Nothing can be `pip install`ed onto the board; what is frozen into the firmware
is all there is. Check with `help('modules')`.

## Working style for this repo

The user is learning embedded development and wants to **run every command that
touches the stick themselves** — `m5.sh`, `run.sh`, the deploy scripts,
`systemctl`. Propose them with an explanation of what each part does; do not
execute them. Host-side work that touches no hardware — editing files, running
`smoke.py`, regenerating screenshots — is yours to run.

Prefer probing the board over recalling API details from memory: `dir()` and
`help('modules')` are cheap, read-only and authoritative. Every hard-won fact
above came from a measurement, and none from reasoning about what ought to be
true.

Before changing a screen, look at `docs/screens/*.png` or render them — the
layout is 135 px wide and the difference between "fits" and "overlaps" is not
visible in the source.
