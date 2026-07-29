"""Is there any indicator on this stick besides the screen?

    ./m5.sh run tools/indicator_probe.py

M5.Led drives nothing here - getCount() is 0, so every setAllColor() is a
no-op that returns cleanly. Two candidates are left, and both need eyes and
fingers rather than a return value:

  M5.Power.setLed        an indicator on the power-management chip
  M5.Power.setVibration  a motor, if this board revision has one

The screen says what *should* be happening at each moment, so a flash that
lines up with the label is real and one that does not is imagination. Six slow
cycles, because a single 900 ms blink in the middle of a sequence is easy to
miss - that is exactly what happened on the first pass.

Nothing here writes to flash. `setVibration` is tried at a low value first.
"""

import time

import M5
from M5 import Lcd
from micropython import const

BLACK = 0x000000
WHITE = 0xFFFFFF
GREEN = 0x00FF88
GREY = 0x888888

CYCLES = const(6)
ON_MS = const(700)
OFF_MS = const(700)


def say(line1, line2="", colour=WHITE):
    Lcd.fillScreen(BLACK)
    Lcd.setTextColor(colour, BLACK)
    Lcd.setTextSize(2)
    Lcd.drawCenterString(line1, Lcd.width() // 2, 90)
    Lcd.setTextSize(1)
    Lcd.setTextColor(GREY, BLACK)
    Lcd.drawCenterString(line2, Lcd.width() // 2, 130)


def probe_power_led():
    print("\n-- M5.Power.setLed: %d slow cycles --" % CYCLES)
    setter = getattr(M5.Power, "setLed", None)
    if setter is None:
        print("  absent")
        return

    print("  Watch the stick. The screen says ON or OFF; does anything follow it?")

    for i in range(CYCLES):
        say("LED ON", "cycle %d/%d" % (i + 1, CYCLES), GREEN)
        try:
            setter(1)
        except Exception as exc:  # noqa: BLE001
            print("  setLed(1) failed: %s" % exc)
            return
        time.sleep_ms(ON_MS)

        say("LED OFF", "cycle %d/%d" % (i + 1, CYCLES), GREY)
        setter(0)
        time.sleep_ms(OFF_MS)

    print("  done - did anything blink in time with the screen?")


def probe_vibration():
    print("\n-- M5.Power.setVibration --")
    setter = getattr(M5.Power, "setVibration", None)
    if setter is None:
        print("  absent")
        return

    print("  Hold the stick loosely. Low value first.")

    # The argument scale is undocumented here: it may be 0-255, 0-100, or a
    # bare on/off. Start gentle and climb, so an unexpected full-power motor
    # does not launch the stick off the desk.
    for value in (64, 128, 255):
        say("BUZZ %d" % value, "feel anything?")
        try:
            setter(value)
            print("  setVibration(%d) accepted" % value)
        except Exception as exc:  # noqa: BLE001
            print("  setVibration(%d) failed: %s" % (value, exc))
            break
        time.sleep_ms(600)
        try:
            setter(0)
        except Exception:  # noqa: BLE001
            pass
        time.sleep_ms(500)

    print("  done - any buzz?")


def main():
    M5.begin()
    Lcd.setBrightness(80)
    say("INDICATOR", "watch and hold")
    time.sleep_ms(1200)

    probe_power_led()
    probe_vibration()

    say("done")
    print("\nTwo answers needed, both from you:")
    print("  1. did anything light up in time with LED ON / LED OFF?")
    print("  2. did you feel any vibration?")


main()
