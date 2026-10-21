# LEDs and button spike findings (Task 1)

Pi: homegateway, Raspbian 12, kernel 6.12.25+rpt-rpi-v6, systemd 252.36. No parts wired; LED output checked with `pinctrl get`.
Overlay parameter names in /boot/firmware/overlays/README match the brief (`gpio-led`: gpio, label, trigger; `gpio-key`: gpio, active_low, gpio_pull, label, keycode). No deviations.

Install done: logind drop-in first (+ `systemctl restart systemd-logind`), then modules-load, udev rule, config.txt (backup `/boot/firmware/config.txt.bak-rcr`, snippet appended once, guarded by grep), then `sudo reboot`. The Pi was back over ssh in about 1.5 min; the recorder service was `active` after boot.

## Layout after boot

```
$ ls -l /sys/class/leds
rcr-green -> ../../devices/platform/leds@11/leds/rcr-green
rcr-red   -> ../../devices/platform/leds@1b/leds/rcr-red
$ cat /sys/class/leds/rcr-green/trigger   # bracketed entry
[timer]
$ ls -l /sys/class/leds/rcr-green/{brightness,trigger}
-rw-rw-r-- 1 root gpio ... brightness
-rw-rw-r-- 1 root gpio ... trigger
$ pinctrl get 17     # 6 samples, 0.4 s apart: lo hi lo hi lo hi   (timer blinks, default 500/500)
$ pinctrl get 27     # lo
```
`delay_on` / `delay_off` of the timer trigger stay root-only (not needed). `max_brightness` is 1.

## Pattern trigger (as bmendonca, groups include gpio, no sudo)

- `echo pattern > trigger` works. The kernel then creates `pattern` and `repeat` as root:root 0644; the udev "change" rule fixes them to root:gpio 0664 **asynchronously**. Measured: `pattern` writable after about 60 ms; `repeat` can lag a little more. A write right after switching the trigger can fail with `Permission denied` (it did in the first test). Task 4 must retry/poll (for example up to 1 s) for `os.access(path, os.W_OK)` after setting the trigger.
- Right after switching to `pattern`, `cat pattern` is empty and `cat repeat` is `-1`.
- Format: `"<brightness> <ms> <brightness> <ms> ..."`, brightness 0 or 1 here. LED starts at the first element and holds it for its ms; steps then advance in order (verified with `1 100 0 100 1 100 0 700`: double blink then 0.7 s pause; sampled via pinctrl, whose ~40 ms sampling overhead and Pi Zero load stretch durations by up to ~20%, so ms values are approximate).
- Writing `pattern` restarts the cycle from the first element immediately (rewrite at t=3.0 s of a 2 s/2 s pattern: level went hi at 3.02 s, previously lo from 2.41 s).
- Writing `repeat` also restarts the cycle immediately (same experiment: hi at 3.02 s after the write at 3.01 s). So rewriting the same values resets the phase; write only on state change.
- `repeat` = `-1` means forever (the default). `0` is rejected: `echo 0 > repeat` gives `Invalid argument` (value unchanged). Positive N = N full cycles.
- Finite pattern end: the LED ends at the FIRST element's level of the pattern, not the last. Observed: `1 300 0 300` x3 -> hi lo hi lo hi lo then stays hi; `0 200 1 200` x2 -> ends lo; `1 200 0 200` x1 -> hi lo then hi. So to end dark, start the pattern with 0 or follow with an explicit `brightness`/trigger none.
- Single-step pattern `1 0` did not light the LED (stays lo); for steady on/off use `trigger=none` + `brightness`.

## Brightness

With `trigger=none`: `echo 1 > brightness` -> `pinctrl get 17` hi; `echo 0` -> lo. Works as bmendonca.

## Button

```
$ for d in /sys/class/input/event*; do echo $d $(cat $d/device/name); done
/sys/class/input/event0 button@3
/sys/class/input/event1 vc4-hdmi
/sys/class/input/event2 vc4-hdmi HDMI Jack
$ ls -l /dev/input/event0
crw-rw---- 1 root input 13, 64 ... event0
```
The input device name is **`button@3`** (derived from the gpio-keys node `button@<gpio>`; `label=rcr-button` does NOT become the device name). Use `button@3` as the `[control] button_name` default. Readable by group `input` (bmendonca is a member). `udevadm info` tags it `:power-switch:`, so logind watches it (hence the `HandlePowerKey=ignore` drop-in). Button presses were not tested (no hardware). `evtest` is not installed.

## logind long press

`systemd-analyze cat-config systemd/logind.conf` shows the drop-in active (`HandlePowerKey=ignore`, `HandlePowerKeyLongPress=poweroff`); logind runs without errors. The man page of 252 does not give the threshold. From systemd source memory (`src/login/logind-button.c`, `#define LONG_PRESS_DURATION (5 * USEC_PER_SEC)`): 5 s, hard-coded and not configurable. Confidence: high but not verified on the device or in the source tree this session. Task 7 (wired button) should time a long press.

## Left to Task 7

Wake from halt on GPIO 3, LEDs dark at halt, real button press events, long-press timing.
