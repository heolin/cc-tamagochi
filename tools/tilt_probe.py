"""Which accelerometer axis responds to which tilt?

    ./m5.sh run tools/tilt_probe.py

Three live bars, one per axis, centred at rest. Hold the stick the way you
would hold the buddy, make the gesture you want to use, and read which bar
swings. That is the axis; whether it swings the way you expect is the sign.

This exists because the buddy's petting mini-game had its axis picked by
reasoning about screen orientation, twice, and was wrong both times. Sections 9
and 14 of CLAUDE.md record the same lesson from the TRIKI work: on this board an
axis is something you measure, not something you derive.

Read-only. Ctrl-C to stop.
"""

import time

import M5
from M5 import Lcd
from micropython import const

BLACK = 0x000000
WHITE = 0xFFFFFF
GREY = 0x888888
DARK = 0x333333

AXES = (
    ("X", 0xFF3B5C),
    ("Y", 0x4CD964),
    ("Z", 0x4CA8FF),
)

BAR_X = const(30)
BAR_W = const(96)
BAR_H = const(16)
FIRST_Y = const(60)
STEP_Y = const(46)

# Full bar at this much acceleration. 1 g would need the stick on its side; a
# lower ceiling makes an ordinary lean visible.
FULL_G = 0.6


def draw_axis(index, label, colour, value):
    y = FIRST_Y + index * STEP_Y

    Lcd.setTextSize(1)
    Lcd.setTextColor(colour, BLACK)
    Lcd.drawString(label, 8, y + 4)

    Lcd.setTextColor(WHITE, BLACK)
    Lcd.fillRect(BAR_X, y + BAR_H + 4, BAR_W, 10, BLACK)
    Lcd.drawString("%+.2f g" % value, BAR_X, y + BAR_H + 4)

    # drawRoundRect rather than drawRect: the latter is not on the verified
    # list in CLAUDE.md, and this is a diagnostic - it should not be the thing
    # that fails.
    Lcd.drawRoundRect(BAR_X, y, BAR_W, BAR_H, 2, DARK)

    # Centre line: rest position, so a deflection is obvious either way.
    middle = BAR_X + BAR_W // 2
    Lcd.fillRect(BAR_X + 1, y + 1, BAR_W - 2, BAR_H - 2, BLACK)
    Lcd.drawLine(middle, y, middle, y + BAR_H - 1, GREY)

    span = int(max(-1.0, min(1.0, value / FULL_G)) * (BAR_W // 2 - 2))
    if span:
        left = middle if span > 0 else middle + span
        Lcd.fillRect(left, y + 2, abs(span), BAR_H - 4, colour)


def main():
    M5.begin()
    Lcd.setBrightness(90)
    Lcd.fillScreen(BLACK)

    if not M5.Imu.isEnabled():
        Lcd.setTextColor(0xFF3B5C, BLACK)
        Lcd.drawCenterString("no IMU", Lcd.width() // 2, 100)
        print("tilt: IMU not available")
        return

    Lcd.setTextSize(1)
    Lcd.setTextColor(WHITE, BLACK)
    Lcd.drawCenterString("TILT PROBE", Lcd.width() // 2, 12)
    Lcd.setTextColor(GREY, BLACK)
    Lcd.drawCenterString("tilt and watch", Lcd.width() // 2, 28)
    Lcd.drawCenterString("which bar moves", Lcd.width() // 2, 40)

    print("tilt: X Y Z in g, ten per second")
    last_print = time.ticks_ms()

    try:
        while True:
            M5.update()
            values = M5.Imu.getAccel()

            for index, (label, colour) in enumerate(AXES):
                draw_axis(index, label, colour, values[index])

            now = time.ticks_ms()
            if time.ticks_diff(now, last_print) >= 500:
                print("X %+.3f   Y %+.3f   Z %+.3f" % values)
                last_print = now

            time.sleep_ms(60)

    except KeyboardInterrupt:
        print("tilt: stopped via Ctrl-C")


main()
