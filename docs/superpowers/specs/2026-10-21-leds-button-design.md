# radar-cam-recorder — status LEDs and button

Date: 2026-10-21. Status: draft for approval. Builds on
`2026-10-20-radar-cam-recorder-design.md` (branch `feat/recorder-v1`).

Marks: **[user]** = decided by the user; **[call]** = my call, open to change;
**[measured]** = observed on the target Pi on 2026-10-21; **[verify]** = believed true,
must be confirmed on the Pi before the plan depends on it.

## 1. Intent

The recorder runs headless by a road. Today the only way to know what it is doing is
SSH. The user wants to see the recorder's state at a glance from two small LEDs, and
control it with one push button, all wired to the Pi's GPIO header **[user]**.

The button also changes how the recorder is used. The user wants to power on the Pi,
then choose when to record, close a recording to copy it over the network without
stopping, and shut the Pi down safely, all without a laptop **[user]**.

Success: standing next to the Pi, the user can tell recording / idle / something wrong
from the lights alone, start and stop recording, split a session, and power off
safely with the button. It is a failure, even if it "works", if the lights say
"recording" while no data is being saved, if a long press can't shut the Pi down
because the recorder is stuck, or if a press loses recorded data **[call, inferred]**.

## 2. Goals, non-goals, anti-goals

Goals:

1. A green and a red LED that show the recorder state with blink patterns (section 5)
   **[user]**.
2. One button **[user]**:
   - single press: close the current session (safe to copy) and keep recording in a
     new session **[user; "keep recording" is my call, see 4]**;
   - double press: start / stop recording **[user]**;
   - long press: safe poweroff **[user]**.
3. A new **idle** mode: running and healthy, not recording **[user]**.
4. The recorder **starts idle**, on every start: power-on, crash restart and watchdog
   restart alike **[user]** ("idle would be simpler"). A config switch restores
   recording at start for unattended use; default idle **[call]**.
5. After a start that followed a problem (crash, kill, watchdog, power cut while
   recording), idle shows an extra warning so a stopped recording is not missed
   **[call]**.

Non-goals:

- A web interface. Analysed and deferred: it complements the LEDs but needs a phone
  on the same network, and there is no network by the road. A camera preview with
  overlay needs Picamera2 instead of `rpicam-vid` **[user]**.
- LED feedback of radar targets (aiming aid): declined **[user]**.
- LCD, more buttons, changing settings from the button **[call]**.

Anti-goals:

- **The green LED must never show "recording" while no data is reaching the disk**,
  including when the recorder process is frozen or dead **[call]**.
- **A long press must power off even if the recorder is hung or dead** **[call]**.
- No button action may lose or corrupt recorded data; every recording-ending action
  goes through the existing clean stop **[call]**.
- No Python GPIO library and nothing compiled on the Zero; the LEDs and the button
  are kernel devices (section 3) **[call]**.
- No added CPU load worth measuring: blinking runs in the kernel, not in a Python
  loop **[call]**.

## 3. Hardware and kernel setup

Facts **[measured]**:

- In use: GPIO 14/15 (UART, radar), GPIO 28/29 (camera I2C). GPIO 0/1 are the HAT
  ID pins. Everything else is free. GPIO 17 and 27 read low at power-on.
- Kernel 6.12.25+rpt-rpi-v6; overlays `gpio-led`, `gpio-key`, `gpio-shutdown`
  present; LED trigger modules `ledtrig-pattern`, `ledtrig-transient`, `timer`,
  `heartbeat` present.
- systemd 252 with `HandlePowerKeyLongPress=` in `logind.conf`.
- The service user `bmendonca` is in groups `gpio` and `input`.
- The recorder's venv does not see system packages (gpiozero, lgpio, libgpiod are
  installed system-wide only).
- `i2c_arm` is not enabled in `config.txt` (so GPIO 3 is free).

Wiring **[call]**:

| Part | GPIO | Header pin | Wiring |
|---|---|---|---|
| Green LED | 17 | 11 | GPIO → 330 Ω → LED anode, cathode → GND (pin 9) |
| Red LED | 27 | 13 | GPIO → 330 Ω → LED anode, cathode → GND (pin 14) |
| Button | 3 | 5 | GPIO 3 → button → GND (pin 6) |

GPIO 3 is chosen because grounding it **wakes a halted Pi** **[verify]**, so the same
button powers the Pi back on after a long-press shutdown. It has a fixed on-board
pull-up, so no extra parts are needed. Consequence: `i2c_arm` (I2C bus 1, GPIO 2/3)
must stay disabled. The RTC the user may add later normally sits on that bus; it can
instead use a software I2C bus (`i2c-gpio` overlay) on two other free pins, which is
fast enough for an RTC **[call]**.

Verification and fallbacks **[call]**: the plan's first task is a spike on the Pi
that checks every **[verify]** item before code depends on it.

- GPIO 3 does not wake the Pi: keep the button on GPIO 3 anyway; power-on goes back
  to replugging. Nothing else changes.
- logind's long-press threshold is not 5 s: the classifier's long-press threshold is
  a config value; set its default to whatever logind uses.
- LEDs don't go dark at halt: pick another unambiguous end signal (section 5).

Kernel configuration (`/boot/firmware/config.txt`) **[call]**:

- Two `gpio-led` overlays: labels `rcr-green` (GPIO 17) and `rcr-red` (GPIO 27). The
  green LED's boot trigger is a fast `timer` blink, so "booting" shows from kernel
  start, before the recorder runs.
- One `gpio-key` overlay on GPIO 3, active low with pull-up, emitting `KEY_POWER`.
- A logind drop-in: `HandlePowerKey=ignore`, `HandlePowerKeyLongPress=poweroff`.
  systemd then does the long-press poweroff by itself, with no recorder involvement.
  logind's long-press threshold is fixed at 5 s **[verify]**, which replaces the 3 s
  floated in conversation.
- A udev rule letting group `gpio` write the `rcr-*` LED attributes (trigger,
  brightness, pattern, repeat, and the transient attributes).

These go in `deploy/` with install steps in the README, like the service unit. A
`check-config`-style command or the acceptance doc verifies they are in place
**[call]**.

## 4. Behaviour

### Recording modes

The supervisor gains `State.IDLE` ("idle"). Transitions **[user]**:

- start → idle (or recording, if `start_mode = "record"`);
- idle + double press → recording (through the existing storage and device checks; it
  may land in waiting_for_devices, degraded, disk_full or storage_missing as today);
- any recording-side state + double press → clean stop of the session → idle;
- recording or degraded + single press → close the **session** (not just the segment)
  cleanly and open a new session immediately. The user asked for a recording that is
  "safe to copy"; only a `complete` session is skipped-for-in-progress-safe by the
  README's copy command **[user intent; mechanism my call]**. This is a new path: the
  full session stop (camera stopped, frames CSV, fsync, `complete`) followed by a
  session open, not `_rotate`. Its gap may exceed the 3–4 s rotation gap; it is
  measured in acceptance and logged as a `segment gap`-style incident across the two
  sessions **[call]**;
- disk_full / storage_missing: the user's intent is "recording"; the recorder resumes
  by itself as today when storage recovers. Double press cancels that intent → idle.

Presses that do nothing in the current state get a "rejected" acknowledgement
(section 5) **[call]**:

| State | Single | Double |
|---|---|---|
| idle | rejected | start |
| recording, degraded | split | stop → idle |
| waiting_for_devices, disk_full, storage_missing | rejected (no session open) | stop → idle |
| starting, stopping, error | rejected | rejected |
| a split, start or stop still in progress | rejected | rejected |

A third press within the double-press window is ignored; a sequence ends at "double"
**[call]**.

Commands from the button go through a thread-safe queue that `tick()` drains, so they
never run concurrently with a tick, the same rule as signals today **[call]**.

For SSH use and testing, the same commands are available as signals: `SIGUSR1` =
single press (split), `SIGUSR2` = double press (toggle), so
`sudo systemctl kill -s USR1 radar-cam-recorder` closes the current session from the
network **[call]**.

### Button classification

The recorder reads the `gpio-key` input device with the standard library (struct
over `/dev/input/event*`; the device is found by its name, overridable in config)
**[call]**. A pure classifier, testable with a manual clock, turns key down/up events
into:

- **single**: one press shorter than 1 s, and no second press within 400 ms of release;
- **double**: a second press starting within 400 ms of the first release;
- **long**: held 5 s. The recorder takes no action (logind powers off); it only shows
  the "power off armed" pattern once the hold passes the threshold. Holds between 1 s
  and 5 s are ignored **[call]**.

Timings live in config with these defaults **[call]**.

### Start after a problem

At start, the recorder decides whether the previous run ended badly **[call]**:

- an interrupted session was recovered at startup (crash, kill, or power cut while
  recording), or
- the status file kept in `/run` across service restarts shows a last state other than
  stopped (crash or kill while idle).

If so, idle shows the warning pattern until the next button press.

Clarifications **[call]**: a clean stop (`systemctl stop`/`restart`, poweroff) ends
in `stopped`, so it never warns. A crash during `starting` warns (last state is not
`stopped`). A power cut while idle leaves no trace (`/run` is cleared, no session
open) and does not warn; this is accepted, since nothing was being recorded.

### Liveness

- **Data-driven green.** In the recording states, the green pattern runs only while
  data reaches the disk: the recorder re-arms a kernel-timed, finite pattern (for
  example `ledtrig-pattern` with a finite `repeat`, or `ledtrig-transient`) each time
  either stream makes progress: new camera frames or new radar bytes. So in
  `degraded` with the camera missing, green follows the radar. With no progress from
  either (as in `waiting_for_devices`), green is dark. If the re-arming stops, green
  goes dark within 5 s on its own **[call]**. The mechanism is the builder's, the 5 s
  bound is not.
- **Watchdog.** The unit becomes `Type=notify` with `WatchdogSec=` (30 s **[call]**).
  The recorder sends `READY=1` and per-tick `WATCHDOG=1` over `NOTIFY_SOCKET` with the
  standard library. A hung main loop is killed and restarted by systemd.
  `Restart=always` stays; `TimeoutStopSec=15` stays.
- **Crash display.** `ExecStopPost=` sets red solid when the service did not exit
  cleanly (`$SERVICE_RESULT` ≠ success). On a clean exit it leaves the "shutting
  down" pattern running when the system is powering off, and turns both LEDs off
  otherwise. It must work when the recorder was SIGKILLed.
- **Long press with a hung recorder.** systemd stops it with SIGTERM, then SIGKILL
  after 15 s. The open segment's tail may be lost and the session is marked
  `interrupted` at the next start, as with any crash. The "no data lost" anti-goal
  applies to a working recorder; with a hung one, powering off safely wins
  **[call]**.

## 5. LED patterns

States map to patterns **[user approved the table; exact timings are the builder's]**:

| Meaning (states) | Green | Red |
|---|---|---|
| Booting / starting (kernel boot, `starting`) | fast blink | off |
| Idle, ready (`idle`) | steady on | off |
| Idle after a problem (`idle` + warning) | steady on | slow blink |
| Recording, all OK (`recording`) | data-driven pulse | off |
| Recording, a device missing (`degraded`) | data-driven pulse | 1 blink per cycle = camera missing, 2 = radar missing |
| Waiting for both devices (`waiting_for_devices`) | off | 1 + 2 blinks alternating |
| Storage full or missing (`disk_full`, `storage_missing`) | slow blink, alternating with red | slow blink |
| Broken, needs a laptop (`error`, crash) | off | solid |
| Shutting down (`stopping`, and during poweroff) | fast blink | fast blink |
| Off: safe to unplug | off | off |

Events overlaid briefly on the current pattern, then the state pattern resumes
**[call]**:

- press accepted: both LEDs flash once;
- press rejected: red flashes twice;
- long press past 5 s: red fast blink until power-off.

"Off = safe to unplug" depends on the `gpio-leds` driver turning the LEDs off at
kernel shutdown, after the recorder's stop **[verify]**. If it doesn't, the builder
picks another unambiguous end signal.

## 6. Code shape

**[call]** Within the existing modular layout:

- `leds.py`: an `LedOutput` protocol (set a pattern / re-arm the pulse / off), a
  sysfs implementation, a fake for tests, and an `LedSink` (a `StatusSink`) that maps
  status events to patterns. A missing LED device is logged once and ignored: LEDs
  are optional hardware, never a reason to stop recording.
- `button.py`: the evdev reader thread, the pure classifier, a fake event source. A
  missing button device is logged once and ignored.
- Supervisor: `State.IDLE`, the command queue (`split`, `toggle`), the `start_mode`
  setting, the "previous run ended badly" flag, and a progress signal for the green
  pulse (it already tracks frame progress for the stall watchdog).
- `cli.py`: wire the sinks, the button thread, `SIGUSR1`/`SIGUSR2` and `sd_notify`.
- Config: a `[control]` table (`start_mode`, button device name and timings) and an
  `[leds]` table (enable, LED names).
- `deploy/`: unit file changes, config.txt snippet, logind drop-in, udev rule,
  `ExecStopPost` helper.

Tests: classifier timings, state→pattern mapping, command handling in every state,
start-mode and problem-flag logic, all with fakes and the manual clock as today.

## 7. Acceptance on the Pi

Added to `docs/pi-acceptance.md` **[call]**:

1. Boot: green fast blink from power-on, then green steady (idle), red off.
2. Double press: recording starts; green pulses; a session appears. Double press
   again: session `complete`, back to idle.
3. Single press while recording: the session closes `complete` and a new one opens;
   `rsync` of the closed session works while recording continues.
4. Long press 5 s with the recorder SIGSTOPped: the Pi still powers off; the LEDs go
   dark; a press on the button powers it back on.
5. SIGSTOP the recorder while recording: green goes dark within 5 s; the watchdog
   restarts the recorder; it comes back idle with the red warning blink.
6. SIGKILL the recorder: red solid, then the restart shows idle + warning.
7. Unplug the LEDs or button: the recorder keeps working and logs it once.

## 8. Left to the builder

Exact blink timings, sysfs attribute details per trigger, device discovery, module
and class names beyond those above, and how `ExecStopPost` reaches the LEDs.
