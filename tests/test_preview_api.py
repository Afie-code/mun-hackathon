import http.client
import json
import threading
import unittest

from Main import AppServer
from gantry import GantryController


class NoHardwareTransport:
    def __init__(self):
        self.commands = []

    def command(self, command):
        self.commands.append(command)
        raise AssertionError(f"Preview sent an Arduino command: {command}")

    def stop(self):
        self.commands.append("STOP")
        raise AssertionError("Preview sent an Arduino stop command")

    def close(self):
        pass


class PreviewApiTests(unittest.TestCase):
    def setUp(self):
        self.transport = NoHardwareTransport()
        self.controller = GantryController(
            self.transport, x_limits=(-200, 200), y_limits=(-200, 200)
        )
        self.server = AppServer(("127.0.0.1", 0), self.controller)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request_json(self, method, path, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        encoded_body = json.dumps(body) if body is not None else None
        headers = {"Content-Type": "application/json"} if body is not None else {}
        connection.request(method, path, body=encoded_body, headers=headers)
        response = connection.getresponse()
        data = json.loads(response.read())
        connection.close()
        return response.status, data

    def test_test_endpoint_simulates_without_touching_hardware(self):
        status, result = self.request_json(
            "POST",
            "/api/test",
            {
                "code": (
                    "speed(40)\n"
                    "home()\n"
                    "plant()\n"
                    "move(1, 0)\n"
                    "plant()\n"
                    "move(0, 1)\n"
                    "plant()\n"
                    "move(-1, 0)\n"
                    "plant()\n"
                    "move(-1, 0)\n"
                    "plant()\n"
                    "move(0, -1)\n"
                    "plant()\n"
                    "move(0, -1)\n"
                    "plant()\n"
                    "move(1, 0)\n"
                    "plant()\n"
                    "move(1, 0)\n"
                    "plant()\n"
                    "home()"
                )
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["status"]["position"], [2.0, 2.0])
        self.assertEqual(result["status"]["crop_count"], 9)
        self.assertEqual(result["status"]["speed_percent"], 40)

        status, hardware = self.request_json("GET", "/api/status")
        self.assertEqual(status, 200)
        self.assertEqual(hardware["mode"], "arduino")
        self.assertEqual(hardware["position"], [2.0, 2.0])
        self.assertEqual(self.transport.commands, [])

        status, preview = self.request_json("GET", "/api/preview")
        self.assertEqual(status, 200)
        trail = preview["trail"]
        self.assertEqual(trail[0], [2.0, 2.0])
        self.assertEqual(trail[-1], [2.0, 2.0])
        self.assertEqual(len(preview["crops"]), 9)
        self.assertEqual(preview["speed_percent"], 40)

        status, docs = self.request_json("GET", "/api/docs")
        self.assertEqual(status, 200)
        names = {function["name"] for function in docs["functions"]}
        self.assertIn("move(dx, dy)", names)
        self.assertIn("speed(percent)", names)
        self.assertNotIn("move_to(column, row)", names)

        status, stopped = self.request_json("POST", "/api/test/stop", {})
        self.assertEqual(status, 200)
        self.assertIn("Simulation", stopped["message"])
        self.assertEqual(self.transport.commands, [])


if __name__ == "__main__":
    unittest.main()
