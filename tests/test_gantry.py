import unittest
import threading
from unittest.mock import patch

from gantry import GantryController, GantryError
from safe_language import ProgramError, run_program


class FakeTransport:
    def __init__(self):
        self.commands = []

    def command(self, command):
        self.commands.append(command)
        if command.startswith("MOVE_TO"):
            return f"OK {command.split()[1]} {command.split()[2]}"
        if command == "HOME":
            return "OK 0 0"
        return "OK"

    def stop(self):
        self.commands.append("STOP")

    def close(self):
        pass


class GantryTests(unittest.TestCase):
    def test_program_can_define_functions_and_move_in_simulation(self):
        controller = GantryController(x_limits=(-200, 200), y_limits=(-200, 200))
        logs = []
        run_program(
            "def step(dx, dy):\n"
            "    move(dx, dy)\n"
            "    log(get_position())\n"
            "step(1, 0)\n"
            "step(0, 1)\n"
            "step(-1, 0)\n",
            controller,
            logs.append,
        )
        self.assertEqual(controller.position(), (2, 3))
        self.assertEqual(len(logs), 3)

    def test_speed_function_updates_preview_and_hardware(self):
        simulation = GantryController()
        run_program("speed(45)", simulation, lambda _: None)
        self.assertEqual(simulation.status()["speed_percent"], 45)

        transport = FakeTransport()
        hardware = GantryController(transport)
        self.assertIsNone(hardware.status()["speed_percent"])
        hardware.home()
        run_program("speed(60)\nmove(1, 0)", hardware, lambda _: None)
        self.assertEqual(hardware.status()["speed_percent"], 60)
        self.assertEqual(
            transport.commands,
            ["HOME", "SPEED 60", "MOVE_TO 200.000000 0.000000"],
        )

    def test_speed_function_rejects_values_outside_whole_percent_range(self):
        controller = GantryController()
        for value in ("0", "101", "1.5", "True"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ProgramError, "whole number from 1 to 100"):
                    run_program(f"speed({value})", controller, lambda _: None)
        self.assertEqual(controller.status()["speed_percent"], 100)

    def test_program_rejects_code_outside_the_language(self):
        with self.assertRaisesRegex(ProgramError, "Import"):
            run_program("import os", GantryController(), lambda _: None)
        with self.assertRaisesRegex(ProgramError, "not supported"):
            run_program("move_to.__call__(1, 2)", GantryController(), lambda _: None)
        with self.assertRaisesRegex(ProgramError, "Unknown function: move_to"):
            run_program("move_to(1, 2)", GantryController(), lambda _: None)

    def test_moves_outside_configured_limits_are_rejected(self):
        with self.assertRaisesRegex(GantryError, "outside the configured"):
            GantryController().move(-1, 0)

    def test_environment_defaults_cover_field_in_simulation_but_keep_hardware_limits(self):
        with patch.dict("os.environ", {}, clear=True):
            simulation = GantryController.from_environment()
        self.assertEqual(simulation.x_limits, (-200, 200))
        self.assertEqual(simulation.y_limits, (-200, 200))

        with patch.dict("os.environ", {"GANTRY_SERIAL_PORT": "mock"}, clear=True):
            with patch("gantry.SerialTransport", return_value=FakeTransport()):
                hardware = GantryController.from_environment()
        self.assertEqual(hardware.x_limits, (0, 300))
        self.assertEqual(hardware.y_limits, (0, 200))

    def test_center_plot_is_home_and_movement_requires_integer_steps(self):
        controller = GantryController(x_limits=(-600, 600), y_limits=(-600, 600))
        self.assertEqual(controller.get_position(), (2, 2))
        controller.move(-1, -1)
        self.assertEqual(controller.get_position(), (1, 1))
        controller.move(2, 1)
        self.assertEqual(controller.get_position(), (3, 2))
        with self.assertRaisesRegex(GantryError, "integer plot index from 1 to 3"):
            controller.move(-3, 0)
        with self.assertRaisesRegex(GantryError, "integer plot index from 1 to 3"):
            controller.move(0, 2)
        with self.assertRaisesRegex(GantryError, "integer number of plots"):
            controller.move(0.5, 0)
        with self.assertRaisesRegex(ProgramError, "integer number of plots"):
            run_program("move(1.0, 0)", controller, lambda _: None)
        controller.home()
        controller.move(0, 0)
        self.assertEqual(controller.get_position(), (2, 2))

    def test_status_includes_field_trail_and_crop_state(self):
        controller = GantryController(x_limits=(-300, 300), y_limits=(-300, 300))
        controller.move(-1, -1)
        controller.plant()
        status = controller.status()
        self.assertEqual(
            status["trail"],
            [(2.0, 2.0), (1.0, 1.0)],
        )
        self.assertEqual(status["crop_count"], 1)
        self.assertEqual(status["growing_count"], 1)
        self.assertEqual(status["crops"][0]["x"], 1)
        self.assertEqual(status["activity"], "Idle")

    def test_status_exposes_confirmed_position_and_destination_while_moving(self):
        started = threading.Event()
        finish = threading.Event()

        class SlowTransport(FakeTransport):
            def command(self, command):
                if command.startswith("MOVE_TO"):
                    self.commands.append(command)
                    started.set()
                    if not finish.wait(timeout=2):
                        raise TimeoutError("test movement timed out")
                    return f"OK {command.split()[1]} {command.split()[2]}"
                return super().command(command)

        controller = GantryController(SlowTransport())
        controller.home()
        worker = threading.Thread(target=controller.move, args=(1, 1))
        worker.start()
        try:
            self.assertTrue(started.wait(timeout=2))
            status = controller.status()
            self.assertEqual(status["activity"], "Moving")
            self.assertEqual(status["position"], (2.0, 2.0))
            self.assertEqual(status["target_position"], (3, 3))
        finally:
            finish.set()
            worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(controller.position(), (3.0, 3.0))

    def test_hardware_requires_homing_before_movement(self):
        controller = GantryController(FakeTransport())
        with self.assertRaisesRegex(GantryError, "Run home"):
            controller.move(1, 1)

    def test_hardware_controller_sends_protocol_commands(self):
        transport = FakeTransport()
        controller = GantryController(transport)
        with patch("gantry.time.monotonic", side_effect=[100, 101, 105, 111]):
            controller.home()
            controller.plant()
            controller.move(1, 1)
            controller.plant()
            with self.assertRaisesRegex(GantryError, "still growing"):
                controller.harvest()
            controller.harvest()
        self.assertEqual(controller.position(), (3.0, 3.0))
        self.assertEqual(
            transport.commands,
            [
                "HOME",
                "PLANT",
                "MOVE_TO 200.000000 200.000000",
                "PLANT",
                "HARVEST",
            ],
        )

    def test_hardware_plot_coordinates_are_signed_offsets_from_center_home(self):
        transport = FakeTransport()
        controller = GantryController(
            transport, x_limits=(-300, 300), y_limits=(-300, 300)
        )
        controller.home()
        controller.move(-1, -1)
        self.assertEqual(controller.position(), (1.0, 1.0))
        self.assertEqual(
            transport.commands,
            [
                "HOME",
                "MOVE_TO -200.000000 -200.000000",
            ],
        )

    def test_diagonal_move_and_home_use_direct_paths(self):
        transport = FakeTransport()
        controller = GantryController(
            transport, x_limits=(-300, 300), y_limits=(-300, 300)
        )
        controller.home()
        controller.move(1, 1)
        controller.home()

        self.assertEqual(controller.position(), (2.0, 2.0))
        self.assertEqual(
            transport.commands,
            [
                "HOME",
                "MOVE_TO 200.000000 200.000000",
                "MOVE_TO 0.000000 0.000000",
            ],
        )
        trail = controller.status()["trail"]
        self.assertEqual(trail, [(2.0, 2.0), (3.0, 3.0), (2.0, 2.0)])

    def test_harvest_requires_a_mature_crop_at_the_current_position(self):
        controller = GantryController(
            x_limits=(-300, 300), y_limits=(-300, 300), growth_seconds=10
        )
        with self.assertRaisesRegex(GantryError, "Nothing is planted"):
            controller.harvest()
        with patch("gantry.time.monotonic", return_value=100):
            controller.plant()
        with patch("gantry.time.monotonic", return_value=101):
            with self.assertRaisesRegex(GantryError, "wait 9.0 more seconds"):
                controller.harvest()
        controller.move(-1, -1)
        with patch("gantry.time.monotonic", return_value=111):
            with self.assertRaisesRegex(GantryError, "Nothing is planted"):
                controller.harvest()
        controller.home()
        with patch("gantry.time.monotonic", return_value=111):
            controller.harvest()
        with self.assertRaisesRegex(GantryError, "Nothing is planted"):
            controller.harvest()

    def test_plant_and_harvest_are_exposed_to_user_programs(self):
        controller = GantryController(growth_seconds=10)
        with patch("gantry.time.monotonic", return_value=100):
            run_program("plant()", controller, lambda _: None)
        with patch("gantry.time.monotonic", return_value=111):
            run_program("harvest()", controller, lambda _: None)
        self.assertEqual(controller.position(), (2, 2))


if __name__ == "__main__":
    unittest.main()
