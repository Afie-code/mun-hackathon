"""Gantry API with a safe simulator and an optional line-protocol serial link."""

from __future__ import annotations

import math
import os
import threading
import time
from typing import Protocol


PLOT_SIZE_MM = 200
PLOT_COUNT = 3


class GantryError(Exception):
    """A rejected or failed gantry operation."""


class Transport(Protocol):
    def command(self, command: str) -> str: ...

    def stop(self) -> None: ...

    def close(self) -> None: ...


class SerialTransport:
    """USB serial adapter. The Arduino replies with one `OK ...` or `ERR ...` line."""

    def __init__(self, port: str, baud_rate: int):
        try:
            import serial
        except ImportError as exc:
            raise RuntimeError(
                "pyserial is required for hardware mode; install it with `python -m pip install -r requirements.txt`"
            ) from exc
        try:
            self._serial = serial.Serial(port, baud_rate, timeout=5, write_timeout=2)
            time.sleep(2)
            self._serial.reset_input_buffer()
        except Exception as exc:
            raise RuntimeError(f"Could not open Arduino serial port {port}: {exc}") from exc
        self._write_lock = threading.Lock()

    def command(self, command: str) -> str:
        with self._write_lock:
            self._serial.write((command + "\n").encode("ascii"))
        raw_reply = self._serial.readline()
        if not raw_reply:
            raise GantryError(f"Arduino did not respond to {command.split()[0]}")
        reply = raw_reply.decode("ascii", errors="replace").strip()
        if reply.startswith("ERR"):
            raise GantryError(f"Arduino rejected the command: {reply}")
        if not reply.startswith("OK"):
            raise GantryError(f"Unexpected Arduino response: {reply!r}")
        return reply

    def stop(self) -> None:
        with self._write_lock:
            self._serial.write(b"STOP\n")

    def close(self) -> None:
        self._serial.close()


class GantryController:
    def __init__(
        self,
        transport: Transport | None = None,
        x_limits: tuple[float, float] = (0, 300),
        y_limits: tuple[float, float] = (0, 200),
        growth_seconds: float = 10,
    ):
        growth_seconds = self._number(growth_seconds, "growth_seconds")
        if growth_seconds <= 0:
            raise RuntimeError("growth_seconds must be greater than zero")
        self._transport = transport
        self.x_limits = x_limits
        self.y_limits = y_limits
        self.growth_seconds = growth_seconds
        self._x = 0.0
        self._y = 0.0
        self._homed = transport is None
        self._speed_percent: int | None = 100 if transport is None else None
        self._position_lock = threading.Lock()
        self._plot_position: tuple[int, int] = (2, 2)
        self._crops_lock = threading.Lock()
        self._planted_at: dict[tuple[float, float], float] = {}
        self._visual_lock = threading.Lock()
        self._activity = "Idle"
        self._target_position: tuple[int, int] | None = None
        self._trail: list[tuple[float, float]] = [(0.0, 0.0)]
        self._run_lock = threading.Lock()
        self.cancel_event = threading.Event()

    @classmethod
    def from_environment(cls) -> "GantryController":
        port = os.environ.get("GANTRY_SERIAL_PORT")
        x_defaults = (0, 300) if port else (-PLOT_SIZE_MM, PLOT_SIZE_MM)
        y_defaults = (0, 200) if port else (-PLOT_SIZE_MM, PLOT_SIZE_MM)
        x_limits = cls._read_limits("GANTRY_X_MIN", "GANTRY_X_MAX", x_defaults)
        y_limits = cls._read_limits("GANTRY_Y_MIN", "GANTRY_Y_MAX", y_defaults)
        growth_seconds = cls._read_growth_seconds()
        if port:
            baud_rate = int(os.environ.get("GANTRY_BAUD_RATE", "115200"))
            return cls(SerialTransport(port, baud_rate), x_limits, y_limits, growth_seconds)
        return cls(None, x_limits, y_limits, growth_seconds)

    @staticmethod
    def _read_limits(
        minimum_name: str, maximum_name: str, defaults: tuple[float, float]
    ) -> tuple[float, float]:
        try:
            low = float(os.environ.get(minimum_name, defaults[0]))
            high = float(os.environ.get(maximum_name, defaults[1]))
        except ValueError as exc:
            raise RuntimeError(f"{minimum_name} and {maximum_name} must be numbers") from exc
        if not math.isfinite(low) or not math.isfinite(high) or low >= high:
            raise RuntimeError(f"{minimum_name} must be less than {maximum_name}")
        return low, high

    @staticmethod
    def _read_growth_seconds() -> float:
        try:
            growth_seconds = float(os.environ.get("GANTRY_GROWTH_SECONDS", "10"))
        except ValueError as exc:
            raise RuntimeError("GANTRY_GROWTH_SECONDS must be a number") from exc
        if not math.isfinite(growth_seconds) or growth_seconds <= 0:
            raise RuntimeError("GANTRY_GROWTH_SECONDS must be greater than zero")
        return growth_seconds

    @property
    def is_hardware(self) -> bool:
        return self._transport is not None

    @property
    def mode_description(self) -> str:
        return "Arduino serial" if self.is_hardware else "simulation (no Arduino configured)"

    @property
    def mode(self) -> str:
        return "arduino" if self.is_hardware else "simulation"

    def status(self) -> dict:
        now = time.monotonic()
        with self._crops_lock:
            all_crops = list(self._planted_at.items())
            ready_count = sum(
                now - planted_at >= self.growth_seconds
                for _, planted_at in all_crops
            )
            crops = [
                {
                    "x": position[0],
                    "y": position[1],
                    "ready": now - planted_at >= self.growth_seconds,
                    "remaining_seconds": max(
                        0.0, self.growth_seconds - (now - planted_at)
                    ),
                }
                for position, planted_at in all_crops[-500:]
            ]
        with self._visual_lock:
            activity = self._activity
            target_position = self._target_position
            trail = list(self._trail)
        plot_crops = [
            {
                **crop,
                "x": self._millimetres_to_plot_coordinate(crop["x"]),
                "y": self._millimetres_to_plot_coordinate(crop["y"]),
            }
            for crop in crops
        ]
        return {
            "mode": self.mode,
            "mode_description": self.mode_description,
            "position": self.position(),
            "homed": self._homed,
            "speed_percent": self._speed_percent,
            "limits": {"x": list(self.x_limits), "y": list(self.y_limits)},
            "growth_seconds": self.growth_seconds,
            "running": self._run_lock.locked(),
            "activity": activity,
            "target_position": target_position,
            "trail": [
                tuple(self._millimetres_to_plot_coordinate(value) for value in point)
                for point in trail
            ],
            "crops": plot_crops,
            "crop_count": len(all_crops),
            "ready_count": ready_count,
            "growing_count": len(all_crops) - ready_count,
        }

    def api_docs(self) -> list[dict[str, str]]:
        return [
            {"name": "move(dx, dy)", "description": "Move by integer plot steps, including diagonal moves."},
            {"name": "speed(percent)", "description": "Set speed from 1% to 100%; preview playback follows it. Hardware needs Arduino SPEED support."},
            {"name": "home()", "description": "Home the hardware initially; after homing, return directly to the center plot."},
            {"name": "plant()", "description": f"Plant at the current position. Harvest is locked for {self.growth_seconds:g} seconds."},
            {"name": "harvest()", "description": "Harvest at the current position only after planting and the full growth period."},
            {"name": "get_position()", "description": "Return the current (column, row) plot position; home is (2, 2)."},
            {"name": "wait(seconds)", "description": "Pause for 0–5 seconds; real elapsed time counts toward growth."},
            {"name": "log(value)", "description": "Write a value to the run console."},
        ]

    def begin_run(self) -> bool:
        if not self._run_lock.acquire(blocking=False):
            return False
        self.cancel_event.clear()
        with self._visual_lock:
            self._activity = "Running program"
            self._target_position = None
            self._trail = [self._physical_position()]
        return True

    def end_run(self) -> None:
        with self._visual_lock:
            self._activity = "Idle"
            self._target_position = None
        if self._run_lock.locked():
            self._run_lock.release()

    def stop(self) -> None:
        self.cancel_event.set()
        self._set_activity("Stopping")
        try:
            if self._transport is not None:
                self._transport.stop()
        except Exception as exc:
            raise GantryError(f"Could not send STOP to the Arduino: {exc}") from exc
        finally:
            if not self._run_lock.locked():
                self._set_activity("Idle")

    def check_running(self) -> None:
        if self.cancel_event.is_set():
            raise GantryError("Program stopped")

    def _require_homed(self) -> None:
        if not self._homed:
            raise GantryError("Run home() before moving, planting, or harvesting on hardware")

    def position(self) -> tuple[float, float]:
        x, y = self._physical_position()
        return (
            self._millimetres_to_plot_coordinate(x),
            self._millimetres_to_plot_coordinate(y),
        )

    def _physical_position(self) -> tuple[float, float]:
        with self._position_lock:
            return self._x, self._y

    def get_position(self) -> tuple[float, float]:
        return self.position()

    def _move_to_plot(self, column: int, row: int) -> None:
        x_mm = self._plot_index_to_millimetres(column)
        y_mm = self._plot_index_to_millimetres(row)
        self._check_limit(x_mm, self.x_limits, "x")
        self._check_limit(y_mm, self.y_limits, "y")
        self._set_activity("Moving", (column, row))
        try:
            self._move_to_millimetres(x_mm, y_mm)
            with self._position_lock:
                self._plot_position = (column, row)
        finally:
            self._resume_activity()

    def _move_to_millimetres(self, x: float, y: float) -> None:
        self.check_running()
        self._check_limit(x, self.x_limits, "x")
        self._check_limit(y, self.y_limits, "y")
        if self._transport is not None:
            try:
                reply = self._transport.command(f"MOVE_TO {x:.6f} {y:.6f}")
                self._update_position_from_reply(reply, (x, y))
            except Exception:
                self._homed = False
                raise
        else:
            with self._position_lock:
                self._x, self._y = x, y
        self._add_trail_point(self._physical_position())

    def move(self, dx: object, dy: object) -> None:
        self.check_running()
        self._require_homed()
        dx = self._whole_plot_step(dx, "dx")
        dy = self._whole_plot_step(dy, "dy")
        if dx == 0 and dy == 0:
            return
        with self._position_lock:
            column, row = self._plot_position
        self._move_to_plot(
            self._plot_index(column + dx, "column"),
            self._plot_index(row + dy, "row"),
        )

    def set_speed(self, percent: object) -> None:
        self.check_running()
        if type(percent) is not int or not 1 <= percent <= 100:
            raise GantryError("speed(percent) must be a whole number from 1 to 100")
        if self._transport is not None:
            self._transport.command(f"SPEED {percent}")
        self._speed_percent = percent

    def home(self) -> None:
        self.check_running()
        self._set_activity("Homing", (2, 2))
        try:
            if self._transport is not None and not self._homed:
                reply = self._transport.command("HOME")
                self._update_position_from_reply(reply, (0.0, 0.0))
                with self._position_lock:
                    self._plot_position = (2, 2)
                self._homed = True
                self._add_trail_point(self._physical_position())
            else:
                self._move_to_plot(2, 2)
        finally:
            self._resume_activity()

    def harvest(self) -> None:
        self.check_running()
        self._require_homed()
        position = self._physical_position()
        with self._crops_lock:
            planted_at = self._planted_at.get(position)
            if planted_at is None:
                raise GantryError("Nothing is planted here; call plant() at this position first")
            remaining = self.growth_seconds - (time.monotonic() - planted_at)
            if remaining > 0:
                raise GantryError(
                    f"Crop is still growing; wait {remaining:.1f} more seconds before harvesting"
                )
        self._set_activity("Harvesting")
        try:
            if self._transport is not None:
                self._transport.command("HARVEST")
            with self._crops_lock:
                self._planted_at.pop(position, None)
        finally:
            self._resume_activity()

    def plant(self) -> None:
        self.check_running()
        self._require_homed()
        position = self._physical_position()
        self._set_activity("Planting")
        try:
            if self._transport is not None:
                self._transport.command("PLANT")
            with self._crops_lock:
                self._planted_at[position] = time.monotonic()
        finally:
            self._resume_activity()

    def wait(self, seconds: float) -> None:
        seconds = self._number(seconds, "seconds")
        if seconds < 0 or seconds > 5:
            raise GantryError("wait(seconds) must be between 0 and 5")
        self._set_activity("Waiting")
        try:
            if self.cancel_event.wait(seconds):
                raise GantryError("Program stopped")
        finally:
            self._resume_activity()

    def _set_activity(
        self,
        activity: str,
        target: tuple[int, int] | None = None,
    ) -> None:
        with self._visual_lock:
            self._activity = activity
            self._target_position = target

    def _resume_activity(self) -> None:
        self._set_activity("Running program" if self._run_lock.locked() else "Idle")

    def _add_trail_point(self, position: tuple[float, float]) -> None:
        with self._visual_lock:
            if not self._trail or self._trail[-1] != position:
                self._trail.append(position)
                if len(self._trail) > 250:
                    del self._trail[:-250]

    @staticmethod
    def _number(value: object, name: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise GantryError(f"{name} must be a number")
        number = float(value)
        if not math.isfinite(number):
            raise GantryError(f"{name} must be a finite number")
        return number

    @staticmethod
    def _plot_index(value: object, name: str) -> int:
        if type(value) is not int or not 1 <= value <= PLOT_COUNT:
            raise GantryError(
                f"{name} must be an integer plot index from 1 to {PLOT_COUNT}"
            )

        return value

    @staticmethod
    def _whole_plot_step(value: object, name: str) -> int:
        if type(value) is not int:
            raise GantryError(f"{name} must be an integer number of plots")
        return value

    @staticmethod
    def _plot_index_to_millimetres(index: int) -> float:
        return (index - 2) * PLOT_SIZE_MM

    @staticmethod
    def _millimetres_to_plot_coordinate(value: float) -> float:
        return value / PLOT_SIZE_MM + 2

    @staticmethod
    def _check_limit(value: float, limits: tuple[float, float], axis: str) -> None:
        if not limits[0] <= value <= limits[1]:
            raise GantryError(
                f"{axis.upper()} position {value:g} mm is outside the configured "
                f"{limits[0]:g}–{limits[1]:g} mm limits"
            )

    def _update_position_from_reply(
        self, reply: str, fallback: tuple[float, float]
    ) -> None:
        parts = reply.split()
        try:
            position = (float(parts[1]), float(parts[2])) if len(parts) >= 3 else fallback
        except ValueError as exc:
            raise GantryError(f"Invalid position in Arduino response: {reply!r}") from exc
        self._check_limit(position[0], self.x_limits, "x")
        self._check_limit(position[1], self.y_limits, "y")
        with self._position_lock:
            self._x, self._y = position

    def close(self) -> None:
        if self._transport is not None:
            self._transport.close()
