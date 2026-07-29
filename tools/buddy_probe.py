"""
What does this board actually give us for the Claude buddy?

    ./m5.sh run tools/buddy_probe.py

`dir(M5)` lists Led, Speaker, Als and BtnB, but a name in that list only proves
the firmware knows the name - not that this board revision has the hardware
behind it. Nothing in this repo has ever called them: test2 and test3 use only
BtnA, M5.Imu and M5.Power.getBatteryLevel.

The buddy's v1 wants all of them, so each becomes a graceful no-op rather than
a crash if it is missing. This reports which is which.

Nothing here writes to flash or NVS. The one side effect is a short, quiet beep
if the speaker answers - that is the only way to find out whether it does.
"""

import time

import M5
from M5 import Lcd

# Collected as (label, status, detail) and printed as a table at the end.
RESULTS = []

PRESENT = "yes"
ABSENT = "NO"
PARTIAL = "partial"


def record(label, status, detail=""):
    RESULTS.append((label, status, detail))
    print("  %-28s %-8s %s" % (label, status, detail))


def probe(label, fn):
    """Run one probe. Any exception means the feature is absent, not a failure
    of this script - that is the whole question being asked."""
    try:
        detail = fn()
    except Exception as exc:  # noqa: BLE001 - absence is the expected outcome
        record(label, ABSENT, "%s: %s" % (type(exc).__name__, exc))
        return None
    record(label, PRESENT, "" if detail is None else str(detail))
    return detail


def members(obj):
    """Public attribute names, which is what tells us the shape of an API."""
    return sorted(a for a in dir(obj) if not a.startswith("_"))


# --------------------------------------------------------------------------
# Buttons
# --------------------------------------------------------------------------


def probe_buttons():
    print("\n-- buttons --")
    print("  (not pressing anything: this only asks whether the call answers)")

    for name in ("BtnA", "BtnB", "BtnC", "BtnPWR", "BtnEXT"):
        button = getattr(M5, name, None)
        if button is None:
            record(name, ABSENT, "not an attribute of M5")
            continue

        def read(button=button):
            M5.update()
            return "isPressed() -> %s" % button.isPressed()

        probe(name, read)

    # wasPressed/wasClicked would be more convenient than isPressed for edge
    # detection, but test2 and test3 both use isPressed, so confirm what exists
    # before switching the buddy over to anything else.
    btn = getattr(M5, "BtnA", None)
    if btn is not None:
        record("BtnA members", PRESENT, ", ".join(members(btn)))


# --------------------------------------------------------------------------
# Output: LED and speaker
# --------------------------------------------------------------------------


def probe_led():
    print("\n-- LED --")
    led = getattr(M5, "Led", None)
    if led is None:
        record("M5.Led", ABSENT, "not an attribute of M5")
        return

    record("M5.Led members", PRESENT, ", ".join(members(led)))

    # The S3 port of the upstream firmware moved the LED from G10 to G19, so
    # a driver written for the older StickC may address the wrong pin.
    def blink():
        for method in ("on", "off"):
            fn = getattr(led, method, None)
            if fn is None:
                raise AttributeError("no M5.Led.%s()" % method)
        led.on()
        time.sleep_ms(300)
        led.off()
        return "blinked via on()/off() - did you see it?"

    probe("M5.Led on/off", blink)


def probe_speaker():
    print("\n-- speaker --")
    spk = getattr(M5, "Speaker", None)
    if spk is None:
        record("M5.Speaker", ABSENT, "not an attribute of M5")
        return

    record("M5.Speaker members", PRESENT, ", ".join(members(spk)))

    def beep():
        begin = getattr(spk, "begin", None)
        if begin is not None:
            begin()

        volume = getattr(spk, "setVolume", None)
        if volume is not None:
            volume(30)  # quiet: this is a diagnostic, not an alarm

        tone = getattr(spk, "tone", None)
        if tone is None:
            raise AttributeError("no M5.Speaker.tone()")

        tone(2000, 120)
        time.sleep_ms(300)
        return "played 2 kHz for 120 ms - did you hear it?"

    probe("M5.Speaker tone", beep)


# --------------------------------------------------------------------------
# Sensors
# --------------------------------------------------------------------------


def probe_als():
    print("\n-- ambient light sensor --")
    als = getattr(M5, "Als", None)
    if als is None:
        record("M5.Als", ABSENT, "not an attribute of M5")
        return

    record("M5.Als members", PRESENT, ", ".join(members(als)))

    # Auto-brightness depends on this one entirely. The StickS3 may simply not
    # carry the part, in which case the attribute exists and every call fails.
    for method in ("getLightSensorData", "getLux", "read", "getValue"):
        fn = getattr(als, method, None)
        if fn is not None:
            probe("M5.Als.%s()" % method, fn)


def probe_imu():
    print("\n-- IMU --")
    # Verified working in test3; confirmed here so shake detection can rely
    # on it without a second thought.
    probe("M5.Imu.isEnabled()", M5.Imu.isEnabled)
    probe("M5.Imu.getType()", M5.Imu.getType)
    probe("M5.Imu.getAccel()", M5.Imu.getAccel)
    probe("M5.Imu.getGyro()", M5.Imu.getGyro)


def probe_power():
    print("\n-- power --")
    power = getattr(M5, "Power", None)
    if power is None:
        record("M5.Power", ABSENT, "not an attribute of M5")
        return

    record("M5.Power members", PRESENT, ", ".join(members(power)))

    # These feed the status ack. REFERENCE.md lets us omit any we do not have,
    # so a miss here costs a field, not the feature.
    # getBatteryLevel is already used at test3/main.py:747.
    for method in (
        "getBatteryLevel",
        "getBatteryVoltage",
        "getBatteryCurrent",
        "isCharging",
        "getType",
    ):
        fn = getattr(power, method, None)
        if fn is None:
            record("M5.Power.%s()" % method, ABSENT, "no such method")
            continue
        probe("M5.Power.%s()" % method, fn)


# --------------------------------------------------------------------------
# Screen and clock
# --------------------------------------------------------------------------


def probe_screen():
    print("\n-- screen --")
    record("size", PRESENT, "%d x %d" % (Lcd.width(), Lcd.height()))

    # Idle dimming needs to read the current level to restore it, not just set
    # a hardcoded one.
    for method in ("getBrightness", "setBrightness"):
        fn = getattr(Lcd, method, None)
        if fn is None:
            record("Lcd.%s()" % method, ABSENT, "no such method")
        elif method == "getBrightness":
            probe("Lcd.getBrightness()", fn)
        else:
            record("Lcd.setBrightness()", PRESENT, "present (not called here)")


def probe_clock():
    print("\n-- clock --")

    # The protocol sends {"time": [epoch, tz_offset]} on connect. Whether we
    # can do anything with it depends on machine.RTC being settable; M5 itself
    # exposes no Rtc attribute on this build.
    def rtc():
        import machine

        clock = machine.RTC()
        return "datetime() -> %s" % (clock.datetime(),)

    probe("machine.RTC()", rtc)
    record("M5.Rtc", PRESENT if hasattr(M5, "Rtc") else ABSENT, "")


def probe_nvs():
    print("\n-- NVS --")

    # Counters and settings survive a restart through this. Read-only probe:
    # a missing key is the expected result on a fresh board.
    def nvs():
        import esp32

        store = esp32.NVS("buddy")
        try:
            value = store.get_i32("approvals")
        except Exception:  # noqa: BLE001 - key absent, which is fine
            value = "(no 'approvals' key yet)"
        return "namespace opened, approvals = %s" % value

    probe("esp32.NVS('buddy')", nvs)


# --------------------------------------------------------------------------


def main():
    M5.begin()
    Lcd.setBrightness(80)
    Lcd.fillScreen(0x000000)
    Lcd.setTextColor(0xFFFFFF, 0x000000)
    Lcd.drawCenterString("PROBE", Lcd.width() // 2, 100)

    print("buddy probe: board %s" % M5.getBoard())

    probe_buttons()
    probe_led()
    probe_speaker()
    probe_als()
    probe_imu()
    probe_power()
    probe_screen()
    probe_clock()
    probe_nvs()

    print("\n== summary ==")
    absent = [r for r in RESULTS if r[1] == ABSENT]
    print("  %d probed, %d absent" % (len(RESULTS), len(absent)))
    for label, _, detail in absent:
        print("  ABSENT  %-26s %s" % (label, detail))

    Lcd.drawCenterString("done", Lcd.width() // 2, 130)


main()
