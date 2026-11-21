# Gantry Field Lab

A local, browser-based Python-like coding environment for a two-axis gantry. It
starts in simulation mode: programs can be written and tested without connecting
to hardware. When configured, the Python API sends commands to an Arduino over
USB serial. The field view shows the current position, recent movement path,
crop growth states, and commanded destination. **Test code** runs the program on
an isolated simulator and does not send Arduino commands.

## Run it

Requires Python 3.10 or later. From this directory:

```bash
python Main.py
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000). No packages are needed for
simulation. Run the tests with:

```bash
python -m unittest discover -s tests
```

## Programming API

The editor accepts a restricted Python-like language, not arbitrary Python. It
supports variables, user-defined functions, `if`/`else`, `for ... in range(...)`,
`while`, `return`, and basic arithmetic and comparisons. Imports, file and
network access, and other Python runtime features are not available.

| Function | Description |
| --- | --- |
| `move(dx, dy)` | Move by integer plot steps relative to the current plot, including diagonally. Hardware must be homed first. |
| `speed(percent)` | Set movement speed to a whole-number percentage from 1–100. The simulator animates at this pace; hardware needs firmware support for `SPEED percent`. |
| `home()` | Home the hardware initially; after homing, return directly to the center plot. |
| `plant()` | Plant at the current position and start its growth timer. |
| `harvest()` | Harvest at the current position only after it has been planted and matured. |
| `get_position()` | Return the current `(column, row)` plot position; home is `(2, 2)`. |
| `wait(seconds)` | Pause for 0–5 seconds; elapsed time counts toward growth. |
| `log(value)` | Print a value in the run console. |

Each position has its own crop. `plant()` starts a real-time growth timer at the
current gantry position. `harvest()` fails if there is no crop there or if its
timer has not expired; the error says how much longer to wait. Time spent moving
or between program runs counts, and `wait(seconds)` also counts. The default
minimum is 10 seconds, configurable with `GANTRY_GROWTH_SECONDS`. Crop state is
kept in memory, so restarting the app clears all planted crops.

The editor example plants four plot centers inside the default safe travel.
After the configured growth period, run a harvest program such as:

```python
home()
harvest()
move(1, 0)
harvest()
move(0, 1)
harvest()
move(-1, 0)
harvest()
home()
```

To visit and plant all nine plots, first configure limits for the measured,
mechanically safe travel across the full bed. This route uses only one-axis
steps and returns to home:

```python
home()
plant()
move(1, 0)
plant()
move(0, 1)
plant()
move(-1, 0)
plant()
move(-1, 0)
plant()
move(0, -1)
plant()
move(0, -1)
plant()
move(1, 0)
plant()
move(1, 0)
plant()
home()
```

Programs are limited to 5,000 interpreter steps, 1,000 loop iterations, 20
function-call levels, and 30 seconds of runtime.

## Field view and test mode

The field view is a 3×3 bed; each plot is 20 cm × 20 cm, for a total area of
60 cm × 60 cm. `move(dx, dy)` uses integer plot steps from the current plot;
column 1 is leftmost and row 1 is bottommost. The center plot `(2, 2)` is home.
Movement coordinates become signed millimetre offsets from the center plot for
the Arduino, while soft limits remain physical millimetre values. Moves can travel
diagonally directly to the destination plot. Hardware firmware must coordinate
both axes to perform diagonal moves. Simulation defaults cover the full bed
(`-200–200` mm on both axes). Hardware defaults remain X `0–300` mm and Y `0–200` mm, which do
not permit moves left or below home. Set hardware travel limits to measured,
mechanically safe values only; negative limits are needed for those plots, but
do not configure them unless the hardware can safely travel there.

Click **Test code** to run the editor program against a fresh simulator. The
map switches to a clearly marked simulation preview showing the planned path
and simulated crop states; it never sends commands to the Arduino and does not
change the real controller position or crops. Click **Run program** to execute
on the configured controller (with hardware confirmation when connected).

## Arduino connection

Install the optional serial dependency and configure the Arduino's serial port:

```bash
python -m pip install -r requirements.txt
GANTRY_SERIAL_PORT=/dev/ttyACM0 python Main.py
```

On Windows, use the port name assigned to the Arduino, such as `COM3`. The
default baud rate is `115200`; override it with `GANTRY_BAUD_RATE`. The Arduino
must implement the newline-delimited command/response protocol below. This
project does not assume a motor driver, pinout, endstop layout, or tool design;
those must be implemented and tested for your specific hardware.

| Command from app | Required Arduino response |
| --- | --- |
| `MOVE_TO x y` | `OK x y` after the move completes, or `ERR reason` |
| `SPEED percent` | `OK` after applying a 1–100% speed, or `ERR reason` |
| `HOME` | `OK 0 0` after homing completes, or `ERR reason` |
| `PLANT` | `OK` after the planting action completes, or `ERR reason` |
| `HARVEST` | `OK` after the tool action completes, or `ERR reason` |
| `STOP` | Stop request; no response is read |

Arduino coordinates and soft limits are in millimetres; one plot spans 200 mm
from the center home. The editor exposes only `move(dx, dy)`, using integer
plot steps; `MOVE_TO x y` is an internal Arduino protocol command, not an
editor function.
The Arduino's `HOME` routine must physically home at the center plot and reply
`OK 0 0`. Hardware defaults are X `0–300` mm and Y `0–200` mm. Set
`GANTRY_X_MIN`, `GANTRY_X_MAX`, `GANTRY_Y_MIN`, and
`GANTRY_Y_MAX` to match the measured, safe travel of your machine. The app
rejects moves outside these limits; they are not a substitute for endstops,
mechanical limits, a physical emergency stop, or safe firmware.
In hardware mode, call `home()` successfully before any move, planting, or
harvest action. The first call runs the Arduino's homing routine; later calls
return directly to center, including diagonally when needed.
To use `speed(percent)` on hardware, the Arduino firmware must implement the
`SPEED percent` command and apply the requested speed before replying `OK`.
Programs that do not call `speed()` continue using the firmware's existing
default speed.

Before each hardware run, the UI requires confirmation that the work area is
clear. The Stop button requests cancellation and sends `STOP`, but whether it
interrupts an in-progress movement depends on the Arduino firmware. Provide and
test a separate physical emergency stop. Until motion has been tested safely,
leave `GANTRY_SERIAL_PORT` unset and use simulation.

The web server binds to `127.0.0.1` by default. Keep it local, especially in
hardware mode; the prototype API does not include user authentication.

## HTTP API

- `GET /api/status` — mode, current position, configured travel limits, and growth duration.
- `GET /api/preview` — current isolated test-simulation state.
- `GET /api/docs` — the functions available to editor programs.
- `POST /api/run` — JSON body `{"code":"...", "confirm_hardware":true}`.
- `POST /api/test` — JSON body `{"code":"..."}`; run in simulation only.
- `POST /api/stop` — cancel the active program and request a hardware stop.
- `POST /api/test/stop` — cancel a test simulation only.
