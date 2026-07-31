"""The BLE half: talk to the stick over the Nordic UART Service.

The stick is the peripheral and the GATT server; this process is the central
and the client. Confusing but standard - "server" in BLE means the side holding
the characteristics, not the side that initiates.

Everything on the wire is UTF-8 JSON, one object per line. Notifications
fragment at the MTU, so incoming bytes are accumulated until a newline; the
same is true in the other direction, which is why writes are chunked.

## What is on that wire, and who else could hear it

Plain Nordic UART with no pairing and no bonding, which is what every hobby BLE
peripheral does and is worth stating rather than implying. Two consequences,
both accepted deliberately:

* Anyone within a few metres could connect to the stick while this daemon is
  not, and read the same snapshot the screen shows - hunger, hearts, level,
  token counts, how many sessions are open. That is the entire vocabulary
  (`state.snapshot()`); there are no paths, names or content in it, so the worst
  case is a stranger learning that somebody nearby had a busy afternoon.
* Anything in range could equally *send* to the stick. The device acts on two
  events, `input`/`exp` and `input`/`pet`, which award the pet a point and a
  stroke. The stick has no other capability to abuse: it cannot reach this
  daemon's machine except by sending those two lines back up.

If that trade is wrong for you, the fix is not in this file - stop the bridge,
or unplug the stick. Encrypting the link would need pairing support on both
ends and would protect numbers that are already on a screen on your desk.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

from bleak import BleakClient

log = logging.getLogger(__name__)

NUS_RX_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"  # we write here
NUS_TX_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"  # the stick notifies here

# The stick's address is known, so discovery is skipped entirely.
DEFAULT_ADDRESS = "98:88:E0:0E:8C:CE"

HEARTBEAT_INTERVAL = 3.0

# A wedged BleakClient does not recover by being asked more nicely. After this
# many consecutive write failures the link is torn down and rebuilt - the same
# lesson as the I2C peripheral in CLAUDE.md section 12.
WRITE_FAIL_LIMIT = 5

RECONNECT_DELAY = 3.0

# Fallback when the backend will not tell us the negotiated MTU. 20 is what
# fits in the 23-byte ATT default, so it is always safe if pessimistic.
MIN_CHUNK = 20

MAX_LINE = 8192

LineHandler = Callable[[dict], Awaitable[None]]


class BuddyLink:
    """Keeps a connection to the stick up, and lines flowing both ways."""

    def __init__(
        self,
        on_line: LineHandler,
        snapshot: Callable[[], dict],
        address: str = DEFAULT_ADDRESS,
    ) -> None:
        self._on_line = on_line
        self._snapshot = snapshot
        self._address = address

        self._client: BleakClient | None = None
        self._rx = bytearray()
        self._write_failures = 0
        self._stop = asyncio.Event()

        # Set when a heartbeat should go out now rather than at the next tick,
        # so a prompt reaches the screen immediately instead of up to 3 s late.
        self._flush = asyncio.Event()

    @property
    def connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    # -- lifecycle ---------------------------------------------------------

    async def run(self) -> None:
        """Connect, serve, reconnect. Returns only when stop() is called."""
        while not self._stop.is_set():
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - any BLE error is retryable
                log.warning("link failed: %s", exc)

            self._client = None
            self._rx = bytearray()

            if self._stop.is_set():
                return

            log.info("reconnecting in %.0fs", RECONNECT_DELAY)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=RECONNECT_DELAY)
            except asyncio.TimeoutError:
                pass

    async def _session(self) -> None:
        log.info("connecting to %s", self._address)

        async with BleakClient(self._address) as client:
            self._client = client
            self._write_failures = 0
            log.info("connected, mtu=%s", getattr(client, "mtu_size", "?"))

            await client.start_notify(NUS_TX_UUID, self._on_notify)

            # The stick's machine.RTC starts at 1970 and it has no other clock,
            # so the first thing it hears must be the time.
            await self._send_time()
            self._flush.set()

            await self._heartbeat_loop()

    async def stop(self) -> None:
        self._stop.set()
        self._flush.set()

    def flush_soon(self) -> None:
        """Push the current snapshot on the next loop pass."""
        self._flush.set()

    # -- outgoing ----------------------------------------------------------

    def _chunk_size(self) -> int:
        mtu = getattr(self._client, "mtu_size", 0) or 0
        return max(MIN_CHUNK, mtu - 3)

    async def send(self, obj: dict) -> bool:
        """One JSON line, split across writes at the MTU."""
        client = self._client
        if client is None or not client.is_connected:
            return False

        data = (json.dumps(obj, separators=(",", ":")) + "\n").encode()
        size = self._chunk_size()

        try:
            for start in range(0, len(data), size):
                await client.write_gatt_char(
                    NUS_RX_UUID, data[start : start + size], response=False
                )
        except Exception as exc:  # noqa: BLE001 - disconnect races land here
            self._write_failures += 1
            log.warning("write failed (%d/%d): %s", self._write_failures, WRITE_FAIL_LIMIT, exc)
            return False

        self._write_failures = 0
        return True

    async def _send_time(self) -> None:
        import time as _time

        now = int(_time.time())
        offset = -_time.timezone if not _time.daylight else -_time.altzone
        await self.send({"time": [now, offset]})

    async def _heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            await self.send(self._snapshot())

            if self._write_failures >= WRITE_FAIL_LIMIT:
                raise RuntimeError("link wedged: %d consecutive write failures" % self._write_failures)

            self._flush.clear()
            try:
                await asyncio.wait_for(self._flush.wait(), timeout=HEARTBEAT_INTERVAL)
            except asyncio.TimeoutError:
                pass

            client = self._client
            if client is None or not client.is_connected:
                return

    # -- incoming ----------------------------------------------------------

    def _on_notify(self, _sender, data: bytearray) -> None:
        """Notifications arrive mid-line; accumulate until a newline."""
        self._rx.extend(data)

        while True:
            index = self._rx.find(b"\n")
            if index < 0:
                break
            line = bytes(self._rx[:index])
            del self._rx[: index + 1]
            self._dispatch(line)

        if len(self._rx) > MAX_LINE:
            log.warning("dropping %d bytes of unterminated input", len(self._rx))
            self._rx = bytearray()

    def _dispatch(self, line: bytes) -> None:
        line = line.strip()
        if not line:
            return

        try:
            obj = json.loads(line)
        except ValueError:
            log.warning("bad JSON from stick: %r", line[:120])
            return

        if isinstance(obj, dict):
            # start_notify's callback is sync; hand the work to the loop.
            asyncio.create_task(self._on_line(obj))
