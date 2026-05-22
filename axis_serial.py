"""
axis_serial.py
==============
ASCII telegram exchange over a serial connection for axis control systems.

Outgoing telegram format (comma-delimited, newline-terminated) contains all data fields:
    <name>,<status>,<target_pos>,<current_pos>,<velocity>,<limit_status>\n

Example:
    x_axis,idle,12.500,12.491,0.5,0\n

Incoming telegram format (comma-delimited, newline-terminated) contains only axis name
and a single parameter/ value pair:
    <name>,<parameter>,<value>

Valid parameters:
    target: new target position
    velocity: movement velocity
    stop: special parameter, any value stops movement on the given axis

Example:
    Y_AXIS,target,-1.2\n
    ROT_Z,stop,1\n
    Z_AXIS,velocity,0.2\n

Usage:
    python axis_serial.py --port /dev/ttyUSB0 --baud 115200
"""

import argparse
import logging
import threading
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Optional

import serial

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("axis_serial")


# ---------------------------------------------------------------------------
# Limit status enum
# ---------------------------------------------------------------------------
class LimitStatus(IntEnum):
    """Bit-field style limit flags."""

    NONE = 0  # No limit active
    LOWER_LIMIT = 1  # At lower (negative) end-stop
    UPPER_LIMIT = 2  # At upper (positive) end-stop

    @classmethod
    def from_int(cls, value: int) -> "LimitStatus":
        try:
            return cls(value)
        except ValueError:
            logger.warning("Unknown limit status value %d, defaulting to NONE.", value)
            return cls.NONE


# ---------------------------------------------------------------------------
# Axis data model
# ---------------------------------------------------------------------------
@dataclass
class Axis:
    """Represents one motion axis tracked by the system."""

    name: str
    status: str = "IDLE"
    target_position: float = 0.0
    current_position: float = 0.0
    velocity: float = 1.0
    limit_status: LimitStatus = LimitStatus.NONE

    # Internal bookkeeping
    last_seen: float = field(default_factory=time.monotonic, repr=False)

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------
    def to_telegram(self) -> str:
        """Encode this axis as an ASCII telegram string (without newline)."""
        return (
            f"{self.name},"
            f"{self.status},"
            f"{self.target_position:.6f},"
            f"{self.current_position:.6f},"
            f"{self.velocity:.6f},"
            f"{int(self.limit_status)}"
        )

    def stop(self) -> None:
        """Send stop command to hardware."""
        # TODO: tango
        self.target_position = self.current_position

    def move(self, target: float) -> None:
        """Set new target position."""
        # TODO: tango
        self.target_position = float(target)

    def set_velocity(self, velocity: float) -> None:
        """Set new movement velocity."""
        # TODO: tango
        self.velocity = float(velocity)

    def __str__(self) -> str:
        return (
            f"Axis({self.name!r}  status={self.status}  "
            f"target={self.target_position:.3f}  "
            f"pos={self.current_position:.3f}  "
            f"velocity={self.velocity:.3f}  "
            f"limits={self.limit_status.name})"
        )


# ---------------------------------------------------------------------------
# Axis manager (in-memory store)
# ---------------------------------------------------------------------------
class AxisManager:
    """Thread-safe registry for an arbitrary number of Axis objects."""

    def __init__(self) -> None:
        self._axes: dict[str, Axis] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def add_axis(self, axis: Axis) -> None:
        """Insert an axis."""
        axis.last_seen = time.monotonic()
        with self._lock:
            self._axes[axis.name] = axis
        logger.debug("Updated %s", axis)

    def get(self, name: str) -> Optional[Axis]:
        """Return the axis with the given name, or None."""
        with self._lock:
            return self._axes.get(name)

    def all_axes(self) -> list[Axis]:
        """Return a snapshot of all tracked axes."""
        with self._lock:
            return list(self._axes.values())

    def remove(self, name: str) -> bool:
        """Remove an axis by name. Returns True if it existed."""
        with self._lock:
            return self._axes.pop(name, None) is not None

    def print_table(self) -> None:
        """Pretty-print all axes to stdout."""
        axes = self.all_axes()
        if not axes:
            print("  (no axes registered)")
            return

        header = (
            f"  {'NAME':<20} "
            f"{'STATUS':<12} "
            f"{'TARGET':>12} "
            f"{'CURRENT':>12} "
            f"{'VELOCITY':>12} "
            f"{'LIMITS':<15}"
        )
        print(header)
        print("  " + "-" * (len(header) - 2))
        for ax in sorted(axes, key=lambda a: a.name):
            print(
                f"  {ax.name:<20} "
                f"{ax.status:<12} "
                f"{ax.target_position:>12.4f} "
                f"{ax.current_position:>12.4f} "
                f"{ax.velocity:>12.4f} "
                f"{ax.limit_status.name:<15}"
            )


# ---------------------------------------------------------------------------
# Serial bus
# ---------------------------------------------------------------------------
class SerialBus:
    """
    Wraps a pyserial connection and provides:
      - send_axis()  – encode and transmit one Axis telegram
      - a background reader thread that decodes incoming telegrams and
        feeds them to an AxisManager
    """

    ENCODING = "ascii"
    TERMINATOR = b"\n"

    def __init__(
        self,
        port: str,
        baud: int,
        manager: AxisManager,
        *,
        timeout: float = 1.0,
        write_timeout: float = 2.0,
        rx_callback=None,
    ) -> None:
        self._port = port
        self._baud = baud
        self._manager = manager
        self._rx_callback = rx_callback  # optional extra hook: fn(Axis) -> None
        self._ser: Optional[serial.Serial] = None
        self._rx_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._write_lock = threading.Lock()
        self._timeout = timeout
        self._write_timeout = write_timeout

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------
    def open(self) -> None:
        """Open the serial port and start the background reader."""
        self._ser = serial.Serial(
            port=self._port,
            baudrate=self._baud,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=self._timeout,
            write_timeout=self._write_timeout,
        )
        self._stop_event.clear()
        self._rx_thread = threading.Thread(
            target=self._reader_loop,
            name="SerialRxThread",
            daemon=True,
        )
        self._rx_thread.start()
        logger.info("Opened %s @ %d baud", self._port, self._baud)

    def close(self) -> None:
        """Signal the reader to stop and close the port."""
        self._stop_event.set()
        if self._rx_thread:
            self._rx_thread.join(timeout=3.0)
        if self._ser and self._ser.is_open:
            self._ser.close()
        logger.info("Serial port closed.")

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *_):
        self.close()

    # ------------------------------------------------------------------
    # Transmit
    # ------------------------------------------------------------------
    def send_axis(self, axis: Axis) -> None:
        """Encode *axis* as a telegram and write it to the serial port."""
        if self._ser is None or not self._ser.is_open:
            raise RuntimeError("Serial port is not open.")
        line = (axis.to_telegram() + "\n").encode(self.ENCODING)
        with self._write_lock:
            self._ser.write(line)
        logger.debug("TX → %s", axis.to_telegram())

    # ------------------------------------------------------------------
    # Background receive loop
    # ------------------------------------------------------------------

    def _update_axis(self, telegram: str) -> None:
        """
        Parse telegram and update the corresponding axis object in AxisManager.
        """
        name, target, velocity, stop = telegram.split(",")
        axis = self._manager.get_axis(name)
        if stop:
            axis.stop()

    def _reader_loop(self) -> None:
        logger.debug("RX thread started.")
        while not self._stop_event.is_set():
            try:
                raw_bytes = self._ser.readline()
            except serial.SerialException as exc:
                logger.error("Serial read error: %s", exc)
                break

            if not raw_bytes:
                # readline() timed out — loop again
                continue

            raw = raw_bytes.decode(self.ENCODING, errors="replace").strip()
            if not raw:
                continue

            logger.debug("RX ← %r", raw)
            try:
                parts = raw.strip().split(",")
                if len(parts) != 3:
                    raise ValueError(f"Expected 3 fields, got {len(parts)}: {raw!r}")
                name, target_pos, velocity = parts
                axis = self._manager.get(name)
                axis.set_velocity(float(velocity))
                axis.move(float(target_pos))
            except ValueError as exc:
                logger.warning("Malformed telegram ignored: %s  (%r)", exc, raw)
                continue

            if self._rx_callback:
                try:
                    self._rx_callback(axis)
                except Exception as exc:  # noqa: BLE001
                    logger.error("rx_callback raised: %s", exc)

        logger.debug("RX thread exiting.")


# ---------------------------------------------------------------------------
# Demo / CLI entry-point
# ---------------------------------------------------------------------------
def _demo_loop(bus: SerialBus, manager: AxisManager, interval: float = 1.0) -> None:
    """
    Transmit all locally registered axes at *interval* seconds,
    and periodically print the current state table.
    """
    print_every = max(1, int(5.0 / interval))
    iteration = 0
    try:
        while True:
            for axis in manager.all_axes():
                try:
                    bus.send_axis(axis)
                except serial.SerialException as exc:
                    logger.error("TX error: %s", exc)

            iteration += 1
            if iteration % print_every == 0:
                print(f"\n--- Axis state @ {time.strftime('%H:%M:%S')} ---")
                manager.print_table()

            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nInterrupted by user.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="ASCII telegram exchange over serial for axis control."
    )
    parser.add_argument(
        "--port", default="/dev/ttyUSB0", help="Serial port (default: /dev/ttyUSB0)"
    )
    parser.add_argument(
        "--baud", type=int, default=115200, help="Baud rate (default: 115200)"
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=3,
        help="TX interval in seconds (default: 3)",
    )
    args = parser.parse_args()

    manager = AxisManager()

    # Register some demo axes
    manager.add_axis(
        Axis(
            "X_AXIS",
            status="MOVING",
            target_position=100.0,
            current_position=42.5,
            limit_status=LimitStatus.NONE,
        )
    )
    manager.add_axis(
        Axis(
            "Y_AXIS",
            status="IDLE",
            target_position=0.0,
            current_position=0.0,
            limit_status=LimitStatus.LOWER_LIMIT,
        )
    )
    manager.add_axis(
        Axis(
            "Z_AXIS",
            status="HOMING",
            target_position=-10.0,
            current_position=-3.2,
            limit_status=LimitStatus.NONE,
        )
    )
    manager.add_axis(
        Axis(
            "ROT_A",
            status="FAULT",
            target_position=90.0,
            current_position=47.1,
            limit_status=LimitStatus.NONE,
        )
    )

    # ----------------------------------------------------------------
    # Dummy loop for now
    # ----------------------------------------------------------------
    def on_receive(axis: Axis) -> None:
        """Called for every successfully decoded incoming telegram."""
        logger.info(f"Received axis update, new state is:\n{axis}")

    with SerialBus(args.port, args.baud, manager, rx_callback=on_receive) as bus:
        print(f"Connected to {args.port} @ {args.baud} baud.")
        print("Press Ctrl-C to quit.\n")
        _demo_loop(bus, manager, interval=args.interval)


if __name__ == "__main__":
    main()
