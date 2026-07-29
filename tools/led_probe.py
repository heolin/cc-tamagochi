"""Close the two questions buddy_probe.py left open.

    ./m5.sh run tools/led_probe.py

1. **What is M5.Led?** It exposes setColor/setAllColor/getCount/display, which
   is a NeoPixel-shaped API rather than the on/off the first probe asked for.
   This finds out how many LEDs there are, whether colour actually reaches
   them, and whether M5.Power.setLed drives the same one or a different one.

2. **Does M5.Als measure anything?** It answered 0 in a lit room, which reads
   like an absent part. A single reading cannot tell "no sensor" from "dark",
   so this samples for 15 seconds while you cover and uncover it. If the number
   never moves, there is nothing there and auto-brightness is off the table.

Watch the stick, not only the terminal - half the answers are visual.
"""

import time

import M5
from M5 import Lcd
from micropython import const

BLACK = 0x000000
WHITE = 0xFFFFFF
GREY = 0x888888

COLOURS = (
    ("red", 0xFF0000),
    ("green", 0x00FF00),
    ("blue", 0x0000FF),
    ("white", 0xFFFFFF),
)

ALS_SECONDS = const(15)
ALS_INTERVAL_MS = const(500)


def banner(text, y=100, colour=WHITE):
    Lcd.fillRect(0, y - 10, Lcd.width(), 40, BLACK)
    Lcd.setTextColor(colour, BLACK)
    Lcd.drawCenterString(text, Lcd.width() // 2, y)


def probe_led_count():
    print("\n-- M5.Led --")
    led = getattr(M5, "Led", None)
    if led is None:
        print("  M5.Led absent")
        return None

    try:
        count = led.getCount()
        print("  getCount() -> %s" % count)
    except Exception as exc:  # noqa: BLE001
        print("  getCount() failed: %s" % exc)
        count = None

    for name, value in COLOURS:
        banner("LED: %s" % name)
        try:
            # setAllColor avoids guessing the index range; setColor(0, ...) is
            # tried too, since only one of them may be implemented.
            led.setAllColor(value)
            led.display()
            print("  setAllColor(0x%06X) + display() ok - visible?" % value)
        except Exception as exc:  # noqa: BLE001
            print("  setAllColor(0x%06X) failed: %s" % (value, exc))
            try:
                led.setColor(0, value)
                led.display()
                print("  setColor(0, 0x%06X) + display() ok - visible?" % value)
            except Exception as exc2:  # noqa: BLE001
                print("  setColor(0, ...) failed too: %s" % exc2)
        time.sleep_ms(900)

    try:
        led.setAllColor(0x000000)
        led.display()
        print("  turned off")
    except Exception:  # noqa: BLE001
        pass

    return count


def probe_power_led():
    print("\n-- M5.Power.setLed --")
    setter = getattr(M5.Power, "setLed", None)
    if setter is None:
        print("  absent")
        return

    # If this drives a different indicator than M5.Led, exactly one of the two
    # blinks now - which is the thing worth seeing.
    for value, label in ((1, "on"), (0, "off")):
        banner("Power.setLed %s" % label)
        try:
            setter(value)
            print("  setLed(%d) ok - did anything light up?" % value)
        except Exception as exc:  # noqa: BLE001
            print("  setLed(%d) failed: %s" % (value, exc))
        time.sleep_ms(900)


def probe_als():
    print("\n-- M5.Als over %d s --" % ALS_SECONDS)
    als = getattr(M5, "Als", None)
    if als is None:
        print("  M5.Als absent")
        return

    print("  COVER the stick with your hand, then uncover it, while this runs.")
    print("  A number that never changes means there is no sensor behind the API.")

    light = []
    proximity = []
    deadline = time.ticks_add(time.ticks_ms(), ALS_SECONDS * 1000)

    while time.ticks_diff(deadline, time.ticks_ms()) > 0:
        try:
            lux = als.getLightSensorData()
        except Exception as exc:  # noqa: BLE001
            print("  getLightSensorData() failed: %s" % exc)
            return
        light.append(lux)

        try:
            prox = als.getProximitySensorData()
            proximity.append(prox)
        except Exception:  # noqa: BLE001
            prox = None

        banner("ALS %s" % lux, 100)
        banner("prox %s" % prox, 130, GREY)
        time.sleep_ms(ALS_INTERVAL_MS)

    def report(name, values):
        if not values:
            return
        low, high = min(values), max(values)
        verdict = "VARIES - usable" if high != low else "CONSTANT - nothing there"
        print("  %-10s %d samples, min=%s max=%s   %s"
              % (name, len(values), low, high, verdict))

    report("light", light)
    report("proximity", proximity)


def main():
    M5.begin()
    Lcd.setBrightness(80)
    Lcd.fillScreen(BLACK)
    Lcd.setTextColor(WHITE, BLACK)
    Lcd.drawCenterString("LED / ALS", Lcd.width() // 2, 40)

    probe_led_count()
    probe_power_led()
    probe_als()

    banner("done")
    print("\nDone. What lit up, and did the ALS number move?")


main()
