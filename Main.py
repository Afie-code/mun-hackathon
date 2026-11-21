"""Local web app and HTTP API for the two-axis gantry."""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from gantry import GantryController, GantryError
from safe_language import ProgramError, run_program


WEB_DIR = Path(__file__).with_name("web")
MAX_REQUEST_BYTES = 32_768


def json_bytes(value: object) -> bytes:
    return json.dumps(value, allow_nan=False).encode("utf-8")


class AppServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], controller: GantryController):
        super().__init__(address, RequestHandler)
        self.controller = controller
        self.preview_controller = GantryController(
            x_limits=controller.x_limits,
            y_limits=controller.y_limits,
            growth_seconds=controller.growth_seconds,
        )
        self.execution_lock = threading.Lock()


class RequestHandler(BaseHTTPRequestHandler):
    server: AppServer

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/status":
            self._send_json(200, self.server.controller.status())
            return
        if path == "/api/preview":
            self._send_json(200, self.server.preview_controller.status())
            return
        if path == "/api/docs":
            self._send_json(200, {"functions": self.server.controller.api_docs()})
            return
        static_files = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/app.js": ("app.js", "text/javascript; charset=utf-8"),
            "/styles.css": ("styles.css", "text/css; charset=utf-8"),
        }
        asset = static_files.get(path)
        if asset is None:
            self._send_json(404, {"error": "Not found"})
            return
        filename, content_type = asset
        try:
            content = (WEB_DIR / filename).read_bytes()
        except OSError:
            self._send_json(500, {"error": "The web app files could not be read"})
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(content)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/test/stop":
            self.server.preview_controller.stop()
            self._send_json(200, {"ok": True, "message": "Simulation stop requested"})
            return
        if path == "/api/stop":
            try:
                self.server.controller.stop()
                self._send_json(200, {"ok": True, "message": "Stop requested"})
            except GantryError as exc:
                self._send_json(502, {"error": str(exc)})
            return
        if path not in ("/api/run", "/api/test"):
            self._send_json(404, {"error": "Not found"})
            return
        try:
            payload = self._read_json()
            if path == "/api/test":
                self._test(payload)
            else:
                self._run(payload)
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Invalid Content-Length") from exc
        if length <= 0 or length > MAX_REQUEST_BYTES:
            raise ValueError(f"Request body must be between 1 and {MAX_REQUEST_BYTES} bytes")
        try:
            value = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("Request body must be valid JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("Request body must be a JSON object")
        return value

    def _run(self, payload: dict) -> None:
        code = payload.get("code")
        if not isinstance(code, str) or not code.strip():
            self._send_json(400, {"error": "Enter a program before running it"})
            return
        if len(code) > MAX_REQUEST_BYTES:
            self._send_json(400, {"error": "Program is too large"})
            return
        if self.server.controller.is_hardware and payload.get("confirm_hardware") is not True:
            self._send_json(
                400,
                {"error": "Confirm the work area is clear before moving the gantry"},
            )
            return
        if not self.server.execution_lock.acquire(blocking=False):
            self._send_json(409, {"error": "A program is already running"})
            return
        if not self.server.controller.begin_run():
            self.server.execution_lock.release()
            self._send_json(409, {"error": "A program is already running"})
            return
        logs: list[str] = []
        try:
            run_program(code, self.server.controller, logs.append)
            self._send_json(
                200,
                {"ok": True, "logs": logs, "position": self.server.controller.position()},
            )
        except ProgramError as exc:
            self._send_json(
                400,
                {"error": str(exc), "logs": logs, "position": self.server.controller.position()},
            )
        except Exception as exc:
            self.log_error("Gantry command failed: %s", exc)
            self._send_json(
                502,
                {
                    "error": "The gantry command failed. Check the Arduino connection and firmware protocol.",
                    "logs": logs,
                    "position": self.server.controller.position(),
                },
            )
        finally:
            self.server.controller.end_run()
            self.server.execution_lock.release()

    def _test(self, payload: dict) -> None:
        code = payload.get("code")
        if not isinstance(code, str) or not code.strip():
            self._send_json(400, {"error": "Enter a program before testing it"})
            return
        if len(code) > MAX_REQUEST_BYTES:
            self._send_json(400, {"error": "Program is too large"})
            return
        if not self.server.execution_lock.acquire(blocking=False):
            self._send_json(409, {"error": "A program is already running"})
            return
        simulation = GantryController(
            x_limits=self.server.controller.x_limits,
            y_limits=self.server.controller.y_limits,
            growth_seconds=self.server.controller.growth_seconds,
        )
        self.server.preview_controller = simulation
        if not simulation.begin_run():
            self.server.execution_lock.release()
            self._send_json(409, {"error": "A simulation is already running"})
            return
        logs: list[str] = []
        try:
            run_program(code, simulation, logs.append)
            self._send_json(
                200,
                {"ok": True, "logs": logs, "status": simulation.status()},
            )
        except ProgramError as exc:
            self._send_json(
                400,
                {"error": str(exc), "logs": logs, "status": simulation.status()},
            )
        except Exception as exc:
            self.log_error("Simulation failed: %s", exc)
            self._send_json(
                500,
                {
                    "error": "The simulation failed unexpectedly.",
                    "logs": logs,
                    "status": simulation.status(),
                },
            )
        finally:
            simulation.end_run()
            self.server.execution_lock.release()

    def _send_json(self, status: int, value: object) -> None:
        body = json_bytes(value)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[http] {self.address_string()} - {fmt % args}")


def main() -> None:
    controller = GantryController.from_environment()
    host = os.environ.get("GANTRY_HOST", "127.0.0.1")
    port = int(os.environ.get("GANTRY_WEB_PORT", "8000"))
    server = AppServer((host, port), controller)
    print(f"Gantry coding environment: http://{host}:{port}")
    print(f"Mode: {controller.mode_description}")
    if controller.is_hardware:
        print("Hardware motion is enabled; verify the mechanics and emergency stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down")
    finally:
        server.server_close()
        controller.close()


if __name__ == "__main__":
    main()
