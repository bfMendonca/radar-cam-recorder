# Status LEDs and Button Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Two GPIO LEDs show the recorder's state and one GPIO button starts/stops recording, splits sessions and powers the Pi off, with a new idle mode the recorder starts in.

**Architecture:** The LEDs and the button are kernel devices declared in `config.txt` (`gpio-led`, `gpio-key`); the recorder writes LED patterns to sysfs and reads key events from `/dev/input` with the standard library. The supervisor gains `State.IDLE` and a command queue (`split`, `toggle`) fed by the button and by `SIGUSR1`/`SIGUSR2`. systemd adds the long-press poweroff (logind), a watchdog (`Type=notify`) and a crash display (`ExecStopPost`).

**Tech Stack:** Python ≥3.9 stdlib only (struct, select, socket, threading), Linux LED class (`ledtrig-pattern`), evdev, systemd 252.

**Spec:** `docs/superpowers/specs/2026-10-21-leds-button-design.md`

## Global Constraints

- No new runtime dependency; nothing compiled on the Pi. Stdlib only (spec 2, anti-goals).
- Code targets Python 3.9 (`from __future__ import annotations`), passes `ruff check`, `ruff format --check` and `mypy --strict` as today.
- Tests use fakes and the manual boot clock (`now = [0]` pattern in `tests/test_supervisor.py`); no test needs GPIO hardware.
- LEDs and button are optional hardware: a missing or unwritable device is logged once and ignored; it never stops recording.
- The green LED must never show recording progress unless data reached the disk in the last 5 s.
- Every recording-ending action goes through the existing `_close_session` path.
- Pins: green GPIO 17, red GPIO 27, button GPIO 3 (active low). LED names `rcr-green`, `rcr-red`.
- Button timings (defaults): double-press window 0.4 s, max short press 1.0 s, long press 5.0 s; debounce 0.03 s.
- On the Pi: `UV_PYTHON=/usr/bin/python3 uv sync`; the service is restarted with `sudo systemctl restart radar-cam-recorder`.

## Review Focus

1. A press arriving while a split/start/stop is still executing (seconds on the Zero) must be rejected, not run afterwards: a delayed toggle would silently stop a recording. Test in Task 5.
2. Contact bounce (several down/up transitions within milliseconds) must not become a double press. Test in Task 3.
3. LED sysfs files missing or not writable (`EACCES`, LEDs not declared) must not raise into the status bus or the supervisor. Test in Task 4.
4. Double press while `disk_full`/`storage_missing`: the user's intent becomes idle and the recorder must not resume by itself when space returns. Test in Task 5.
5. Status file absent, empty or corrupt at start: no warning, no crash. Test in Task 6.

---

### Task 1: Kernel setup files and Pi spike

Verifies every **[verify]** item that does not need wired parts, and produces the deploy files later tasks rely on.

**Files:**
- Create: `deploy/config.txt.snippet`, `deploy/99-rcr-leds.rules`, `deploy/rcr-leds.conf` (modules-load.d), `deploy/logind-rcr-button.conf`
- Create: `docs/leds-button-spike.md` (findings)

**Interfaces:**
- Produces: the LED sysfs layout and pattern semantics (`pattern`, `repeat` attributes) used by Task 4; the input device name of the button used by Task 3/6 as `[control] button_name` default.

- [ ] **Step 1: Write the deploy files**

`config.txt.snippet`: `dtoverlay=gpio-led,gpio=17,label=rcr-green,trigger=timer`, `dtoverlay=gpio-led,gpio=27,label=rcr-red`, `dtoverlay=gpio-key,gpio=3,active_low=1,gpio_pull=up,keycode=116,label=rcr-button`. `rcr-leds.conf`: `ledtrig-pattern`. Udev rule: on `SUBSYSTEM=="leds", KERNEL=="rcr-*"`, `chgrp gpio` and `chmod g+w` the attributes `trigger brightness pattern repeat` (re-applied on trigger change, since the kernel creates `pattern`/`repeat` when the trigger is set). Logind drop-in (`/etc/systemd/logind.conf.d/`): `HandlePowerKey=ignore`, `HandlePowerKeyLongPress=poweroff`.

- [ ] **Step 2: Install on the Pi and reboot**

Append the snippet to `/boot/firmware/config.txt`, install the other three files, `sudo reboot`. The recorder restarts by itself.

- [ ] **Step 3: Verify and record in `docs/leds-button-spike.md`**

Each with the command and its output:
- `/sys/class/leds/rcr-green` and `rcr-red` exist; green shows `[timer]` after boot.
- As `bmendonca` (no sudo): `echo pattern > trigger`, then writing `pattern` and `repeat` works; `pinctrl get 17` shows the level following the pattern. Record: does writing `pattern` restart the cycle; does writing `repeat` restart it; what `repeat` value means "forever"; LED state when a finite pattern ends.
- `brightness` 0/1 with trigger `none` sets the pin.
- The input device for the button: its `/sys/class/input/event*/device/name` (expected `rcr-button` or similar), readable by group `input`.
- logind's long-press threshold: from the systemd 252 source or docs for `HandlePowerKeyLongPress` (expected 5 s).
- Not checkable without parts, left to Task 7: wake from halt on GPIO 3, LEDs dark at halt.

- [ ] **Step 4: Commit**

```bash
git add deploy/ docs/leds-button-spike.md
git commit -m "deploy: kernel LED/button setup and spike findings"
```

---

### Task 2: Config tables `[control]` and `[leds]`

**Files:**
- Modify: `radar_cam_recorder/config.py`, `deploy/config.example.toml`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `ControlConfig(start_mode: str, button_name: str, double_press_s: float, max_press_s: float, long_press_s: float, debounce_s: float)`, `LedsConfig(enabled: bool, green: str, red: str)`, new fields `Config.control`, `Config.leds`; both in `__all__` and in `config_to_dict`.

- [ ] **Step 1: Write the failing tests**

```python
def test_control_defaults() -> None:
    cfg = parse_config(minimal())
    assert cfg.control == ControlConfig("idle", "<name from Task 1>", 0.4, 1.0, 5.0, 0.03)
    assert cfg.leds == LedsConfig(True, "rcr-green", "rcr-red")

def test_start_mode_record() -> None:
    assert parse_config(minimal(control={"start_mode": "record"})).control.start_mode == "record"

@pytest.mark.parametrize("bad", [
    {"start_mode": "always"},
    {"double_press_s": 0},
    {"long_press_s": 1.0, "max_press_s": 1.0},   # long must exceed max short press
    {"debounce_s": 0.5},                          # debounce must be below double_press_s
])
def test_control_rejects(bad: dict[str, Any]) -> None:
    with pytest.raises(ConfigError):
        parse_config(minimal(control=bad))
```

(`minimal(**tables)` = the smallest valid mapping, as the file's existing helpers do.)

- [ ] **Step 2: Run, expect FAIL** — `uv run pytest tests/test_config.py -q`

- [ ] **Step 3: Implement** with the existing `_table`/`_get`/`_positive` helpers; `start_mode` in `{"idle", "record"}`. Add both tables, commented, to `deploy/config.example.toml`.

- [ ] **Step 4: Run, expect PASS** — `uv run pytest tests/test_config.py -q`

- [ ] **Step 5: Commit** — `git commit -m "config: [control] and [leds] tables"`

---

### Task 3: Button gestures and the evdev reader

**Files:**
- Create: `radar_cam_recorder/button.py`
- Test: `tests/test_button.py`

**Interfaces:**
- Consumes: `ControlConfig` (Task 2).
- Produces:
  - `class Gesture(str, Enum)`: `SINGLE = "single"`, `DOUBLE = "double"`, `LONG_ARMED = "long_armed"`.
  - `class GestureClassifier(cfg: ControlConfig)`: `feed(pressed: bool, t_ns: int) -> list[Gesture]`, `poll(t_ns: int) -> list[Gesture]`, `next_deadline_ns() -> int | None`.
  - `find_button(name: str, sys_input: Path = Path("/sys/class/input")) -> Path | None` returning `/dev/input/eventN`.
  - `class ButtonReader(path: Path, classifier: GestureClassifier, on_gesture: Callable[[Gesture], None], clock: Clock)`: `start() -> None`, `stop() -> None` (daemon thread; `select` with the classifier's deadline; parses `struct input_event` with format `"llHHi"`, `EV_KEY` only, value 1 down / 0 up / 2 repeat ignored).

Rules (spec 4, "Button classification"): transitions closer than `debounce_s` to the previous accepted one are ignored; a press held ≥ `max_press_s` ends the sequence with no gesture, and `LONG_ARMED` is emitted once when it reaches `long_press_s`; `SINGLE` is emitted `double_press_s` after the release of a short press with no second press; `DOUBLE` is emitted at the second press-down if it starts within `double_press_s` of the first release; after `DOUBLE`, presses are ignored until `double_press_s` passes with the button up.

- [ ] **Step 1: Write the failing tests** (`ms = 1_000_000`, `c = GestureClassifier(defaults)`)

```python
def test_single() -> None:
    assert c.feed(True, 0) == [] and c.feed(False, 100*ms) == []
    assert c.poll(499*ms) == [] and c.poll(500*ms) == [Gesture.SINGLE]

def test_double_emitted_on_second_down() -> None:
    c.feed(True, 0); c.feed(False, 100*ms)
    assert c.feed(True, 300*ms) == [Gesture.DOUBLE]
    c.feed(False, 400*ms); assert c.poll(2000*ms) == []

def test_third_press_ignored() -> None:
    for t in (0, 100, 200, 300): c.feed(t % 200 == 0, t*ms)
    assert c.feed(True, 400*ms) == [] and c.poll(3000*ms) == []

def test_bounce_is_not_double() -> None:
    c.feed(True, 0); c.feed(False, 5*ms); c.feed(True, 10*ms); c.feed(False, 120*ms)
    assert c.poll(600*ms) == [Gesture.SINGLE]

def test_medium_hold_ignored() -> None:
    c.feed(True, 0); c.feed(False, 2000*ms); assert c.poll(5000*ms) == []

def test_long_armed_once() -> None:
    c.feed(True, 0)
    assert c.poll(4999*ms) == [] and c.poll(5000*ms) == [Gesture.LONG_ARMED]
    assert c.poll(6000*ms) == [] and c.feed(False, 7000*ms) == []

def test_next_deadline() -> None:
    assert c.next_deadline_ns() is None
    c.feed(True, 0); assert c.next_deadline_ns() == 5000*ms
    c.feed(False, 100*ms); assert c.next_deadline_ns() == 500*ms

def test_reader_parses_events(tmp_path: Path) -> None:
    # A pipe stands in for /dev/input/eventN: write packed ("llHHi") EV_KEY down/up
    # events (plus an EV_SYN and a value-2 repeat, both ignored); with a manual clock
    # advanced past the window, on_gesture receives [Gesture.SINGLE].

def test_find_button(tmp_path: Path) -> None:
    # tmp_path/event3/device/name == "rcr-button\n" -> Path("/dev/input/event3"); unknown name -> None
```

- [ ] **Step 2: Run, expect FAIL** — `uv run pytest tests/test_button.py -q`
- [ ] **Step 3: Implement `button.py`**; the reader logs once and exits its thread on `OSError` (device gone).
- [ ] **Step 4: Run, expect PASS**
- [ ] **Step 5: Commit** — `git commit -m "button: gesture classifier and evdev reader"`

---

### Task 4: LED patterns, sysfs output and the LED sink

**Files:**
- Create: `radar_cam_recorder/leds.py`
- Test: `tests/test_leds.py`

**Interfaces:**
- Consumes: `State`, `StatusEvent` (`status.py`); `State.IDLE` is added here: `IDLE = "idle"` in `State`, between `STARTING` and `WAITING_FOR_DEVICES`. Task 1's pattern semantics.
- Produces:
  - `class Pattern(Enum)`: `OFF, ON, FAST_BLINK, SLOW_BLINK, SLOW_BLINK_INVERTED, BLINK_1, BLINK_2, BLINK_1_2, PULSE`.
  - `patterns_for(event: StatusEvent) -> tuple[Pattern, Pattern]` (green, red), the spec 5 table:
    `starting`→(FAST_BLINK, OFF); `idle`→(ON, OFF), or (ON, SLOW_BLINK) when `event.detail.get("problem")`; `recording`→(PULSE, OFF); `degraded`→(PULSE, BLINK_1) if reason is `"camera missing"`, (PULSE, BLINK_2) if `"radar missing"`; `waiting_for_devices`→(OFF, BLINK_1_2); `disk_full`/`storage_missing`→(SLOW_BLINK_INVERTED, SLOW_BLINK); `error`→(OFF, ON); `stopping`→(FAST_BLINK, FAST_BLINK); `stopped`→(OFF, OFF).
  - `class LedOutput(Protocol)`: `show(pattern: Pattern) -> None`, `pulse() -> None` (arm one finite PULSE cycle).
  - `class SysfsLed(name: str, root: Path = Path("/sys/class/leds"))` implementing `LedOutput`.
  - `class FakeLed` recording calls (`shown: list[Pattern]`, `pulses: int`).
  - `class LedController(green: LedOutput, red: LedOutput, clock: Clock)` and `LedSink(controller)` (a `StatusSink`). Controller methods: `show_state(event: StatusEvent) -> None`, `progress() -> None`, `flash(kind: str) -> None` with kinds `"accepted"`, `"rejected"`, `"long_armed"`.

Behaviour: `progress()` arms the green pulse only while the current green pattern is PULSE and at most once per pulse cycle; a PULSE cycle with its finite repeat lasts ≤ 5 s, so green goes dark on its own when progress stops. `flash` shows the overlay (accepted: both ON 0.2 s; rejected: red two blinks; long_armed: red FAST_BLINK until the process ends), then restores the state patterns (a `threading.Timer`; thread-safe under one lock). `SysfsLed` writes `trigger` then `pattern`/`repeat` (or `none` + `brightness`); the first `OSError` per LED is logged at WARNING, later ones at DEBUG; nothing raises.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.parametrize("state,reason,detail,expected", [...the full table above...])
def test_patterns_for(...) -> None: ...

def test_pulse_only_while_recording() -> None:
    ctl.show_state(event(State.IDLE)); ctl.progress(); assert green.pulses == 0
    ctl.show_state(event(State.RECORDING)); ctl.progress(); assert green.pulses == 1

def test_pulse_at_most_once_per_cycle() -> None:
    # recording; progress() at t=0, 0.5 s, 1.0 s -> 1 pulse; after one cycle -> 2

def test_flash_restores_state(): ...      # accepted flash, then shown[-1] is the state pattern
def test_long_armed_stays(): ...          # no restore after long_armed

def test_sysfs_writes(tmp_path: Path) -> None:
    # tmp_path/rcr-red/{trigger,brightness,pattern,repeat}; show(ON) -> trigger "none", brightness "1";
    # show(SLOW_BLINK) -> trigger "pattern", pattern non-empty, repeat "-1"

def test_sysfs_missing_or_denied(tmp_path, caplog) -> None:
    # no rcr-red dir -> show() does not raise, one WARNING; chmod 0 on trigger -> no raise
```

- [ ] **Step 2: Run, expect FAIL** — `uv run pytest tests/test_leds.py -q`
- [ ] **Step 3: Implement `leds.py`** and `State.IDLE`. Exact timings are the implementer's (spec 8), within: FAST_BLINK ≥ 4 Hz, SLOW_BLINK ≤ 1 Hz, PULSE cycle ≤ 2.5 s with repeat 2.
- [ ] **Step 4: Run, expect PASS**; also the full suite (`State` changed).
- [ ] **Step 5: Commit** — `git commit -m "leds: state patterns, sysfs output, LED sink"`

---

### Task 5: Supervisor idle mode, commands and progress

**Files:**
- Modify: `radar_cam_recorder/supervisor.py`
- Test: `tests/test_supervisor_control.py` (new; reuses `build`/`Rig` from `tests/test_supervisor.py`)

**Interfaces:**
- Consumes: `ControlConfig.start_mode` (Task 2), `State.IDLE` (Task 4).
- Produces:
  - `Supervisor.__init__(..., warn_at_start: bool = False, on_progress: Callable[[], None] | None = None, on_command: Callable[[str, bool], None] | None = None)`.
  - `Supervisor.request(command: str) -> None` with `"split"` or `"toggle"`; safe from signal handlers and other threads (`queue.SimpleQueue` of `(command, arrival_ns)`).
  - `Supervisor.run(stop, tick_s=0.5, heartbeat: Callable[[], None] | None = None)`: `heartbeat()` after every tick.
  - Idle state events: `set_state(State.IDLE, "idle")`, or `set_state(State.IDLE, "idle after a problem", problem=True)`.

Behaviour (spec 4):
- `_want_recording` starts as `start_mode == "record"`. While false, `_step` opens no session; at the storage check it still runs `_recover()` once storage is usable, without changing state; the state is IDLE.
- Problem flag = `warn_at_start` or `_recover()` marked ≥ 1 session. Any command (accepted or rejected) clears it.
- Commands drain at the start of `tick()`, before `_step`. A command whose `arrival_ns` is earlier than the end of the previous command's execution is rejected (Review Focus 1). Acceptance per the spec 4 table; `on_command(command, accepted)` reports each one.
- `toggle` from idle: `_want_recording = True`; the next storage check opens the session (force the check now).
- `toggle` while wanting to record (recording, degraded, waiting_for_devices, disk_full, storage_missing): `set_state(STOPPING, "stopping recording")`, close via `_stop_recording`, then IDLE; `_want_recording = False`, so storage recovery never reopens (Review Focus 4).
- `split` (recording, degraded): close the session `COMPLETE` with reason `"split by button"` and open a new one in the same tick; then a `segment gap` incident for segment 0 of the new session, measured from the old session's last frame (make `_close_session` return the last sensor timestamp; reuse `_PendingGap`).
- `on_progress()` once per tick when a session is open and the camera meta size grew or `self._writer.reads` increased since the previous tick.

- [ ] **Step 1: Write the failing tests**

```python
def test_starts_idle_and_records_nothing(tmp_path) -> None:
    rig = build(tmp_path); rig.sup.tick(); rig.now[0] += 5*S; rig.sup.tick()
    assert rig.bus.current.state is State.IDLE and rig.sup.session is None
    assert not list(tmp_path.glob("s*"))

def test_start_mode_record(tmp_path) -> None:   # cfg control.start_mode="record" -> session after first tick

def test_toggle_starts_and_stops(tmp_path) -> None:
    # request("toggle"); tick -> session open, state RECORDING; request("toggle"); tick ->
    # session.json state "complete", bus states include STOPPING then IDLE; on_command got ("toggle", True) twice

def test_split_closes_and_reopens(tmp_path) -> None:
    # recording; request("split"); tick -> old session "complete" with reason "split by button",
    # new session open with a different id, state RECORDING

def test_split_rejected_when_idle(tmp_path) -> None:   # on_command("split", False); nothing created

def test_command_during_long_command_rejected(tmp_path) -> None:
    # recording; a camera whose stop advances the manual clock by 3 s; request("split"),
    # then request("toggle") with arrival during the split (inject via a clock hook in stop) ->
    # toggle rejected, still recording

def test_toggle_in_disk_full_goes_idle_and_stays(tmp_path) -> None:
    # storage_check returns low free space -> DISK_FULL; toggle -> IDLE; free space back -> still IDLE

def test_problem_flag_from_recovery(tmp_path) -> None:
    # an in_progress session on disk -> first tick: IDLE with reason "idle after a problem", detail problem=True;
    # request("split") (rejected) -> IDLE "idle"

def test_warn_at_start(tmp_path) -> None:   # warn_at_start=True -> "idle after a problem"

def test_progress_only_with_data(tmp_path) -> None:
    # recording with frames flowing -> on_progress called; idle -> never; stalled camera and silent radar -> not called

def test_heartbeat_called_each_tick(tmp_path) -> None:
```

Existing supervisor tests assume recording from the first tick: give `make_cfg` `control={"start_mode": "record"}` so they keep their meaning.

- [ ] **Step 2: Run, expect FAIL** — `uv run pytest tests/test_supervisor_control.py -q`
- [ ] **Step 3: Implement** in `supervisor.py`.
- [ ] **Step 4: Run the full suite, expect PASS** — `uv run pytest -q`
- [ ] **Step 5: Commit** — `git commit -m "supervisor: idle mode, split/toggle commands, progress"`

---

### Task 6: Wiring: CLI, signals, watchdog, unit file

**Files:**
- Create: `radar_cam_recorder/notify.py`, `deploy/rcr-leds-stop.sh`
- Modify: `radar_cam_recorder/cli.py`, `deploy/radar-cam-recorder.service`
- Test: `tests/test_notify.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: Tasks 2–5.
- Produces:
  - `notify(message: str, env: Mapping[str, str] = os.environ) -> bool`: sends to `NOTIFY_SOCKET` (a leading `@` means the abstract namespace) over `AF_UNIX`/`SOCK_DGRAM`; returns False without the variable or on `OSError`.
  - `previous_run_unclean(status_file: Path) -> bool` in `cli.py`: True only when the file holds JSON whose `"state"` is a string other than `"stopped"`.

Wiring in `_run`: read `previous_run_unclean` **before** the `StatusFileSink` is created; build `LedController(SysfsLed(green), SysfsLed(red), clock)` and add `LedSink` when `cfg.leds.enabled`; pass `warn_at_start`, `on_progress=controller.progress`, `on_command=lambda c, ok: controller.flash("accepted" if ok else "rejected")`; start a `ButtonReader` if `find_button(cfg.control.button_name)` finds the device, else log once; gestures map `SINGLE`→`request("split")`, `DOUBLE`→`request("toggle")`, `LONG_ARMED`→`controller.flash("long_armed")`; `SIGUSR1`→`request("split")`, `SIGUSR2`→`request("toggle")`; `notify("READY=1")` before `supervisor.run`, `heartbeat=lambda: notify("WATCHDOG=1")`; stop the reader in `finally`. `--fake` runs keep LEDs and button off.

Unit: `Type=notify`, `NotifyAccess=main`, `WatchdogSec=30`, `ExecStopPost=/home/bmendonca/workspace/radar-cam-recorder/deploy/rcr-leds-stop.sh`; the rest unchanged. The script: `SERVICE_RESULT` ≠ `success` → red `none`+`brightness 1`, green off; else if `systemctl is-system-running` prints `stopping` → leave the LEDs; else both off. Failures to write are ignored (`|| true`).

- [ ] **Step 1: Write the failing tests**

```python
def test_notify_sends(tmp_path) -> None:
    # bind a SOCK_DGRAM socket at tmp_path/"n"; notify("READY=1", {"NOTIFY_SOCKET": str(path)}) is True; recv == b"READY=1"

def test_notify_without_socket() -> None:
    assert notify("READY=1", {}) is False

@pytest.mark.parametrize("content,expected", [
    (None, False), ("", False), ("{bad", False), ('{"state": "stopped"}', False),
    ('{"state": "idle"}', True), ('{"state": "recording"}', True), ('{"state": 3}', False),
])
def test_previous_run_unclean(tmp_path, content, expected) -> None: ...

def test_run_fake_handles_sigusr(tmp_path) -> None:
    # run --fake in a subprocess with start_mode="record"; SIGUSR1 -> two sessions exist, the first "complete";
    # SIGTERM -> exit 0
```

- [ ] **Step 2: Run, expect FAIL** — `uv run pytest tests/test_notify.py tests/test_cli.py -q`
- [ ] **Step 3: Implement** `notify.py`, the CLI wiring, the script (`chmod +x`) and the unit.
- [ ] **Step 4: Run the full suite, ruff and mypy, expect clean** — `uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run mypy`
- [ ] **Step 5: Commit** — `git commit -m "cli: LEDs, button, signals and systemd watchdog"`

---

### Task 7: Docs, deploy and acceptance on the Pi

**Files:**
- Modify: `README.md` (wiring table, install steps for the Task 1 files, button and LED meaning tables, `systemctl kill -s USR1/USR2`), `docs/pi-acceptance.md` (spec 7 checks), `CHANGELOG.md`

- [ ] **Step 1: Write the docs** from spec sections 3, 5 and 7.
- [ ] **Step 2: Deploy** — rsync to the Pi, `UV_PYTHON=/usr/bin/python3 uv sync`, install the unit, `sudo systemctl daemon-reload && sudo systemctl restart radar-cam-recorder`; run the test suite on the Pi.
- [ ] **Step 3: Software-only acceptance** (no parts needed), recorded in `docs/pi-acceptance.md` with outputs:
  - boot → status `idle`; `systemctl kill -s USR2` → `recording`, a session opens; `USR1` → previous session `complete`, a new one open; `USR2` → `idle`;
  - `pinctrl get 17,27` levels follow the patterns in idle and recording; green stops pulsing within 5 s of `kill -STOP` on the recorder; the watchdog restarts it (`journalctl`), status comes back `idle after a problem`;
  - `kill -9` → `pinctrl` shows red high (ExecStopPost), then idle with the warning.
- [ ] **Step 4: Hands-on acceptance**: spec 7 checks 1–7 with the LEDs and button wired. They need the user's hands; if the parts are not wired yet, list them as pending in `docs/pi-acceptance.md`.
- [ ] **Step 5: Commit** — `git commit -m "docs: LEDs and button install and acceptance"`
