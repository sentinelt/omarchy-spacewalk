# Spacewalk

This drives my treadmill — a **Urevo SpaceWalk 3S** under my desk — from the
[Omarchy](https://omarchy.org) bar, in place of the vendor's phone app.
Written for my own setup, in the malleable computing spirit. Steps taken or remaining toward the goal
in the bar; calories, time, distance, a history grid and
speed / incline / belt control in the panel.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/hero-dark.webp">
  <img src="docs/hero-light.webp" alt="The Omarchy bar with the step counter pill and the Spacewalk panel open: today's numbers, a history grid, and speed and incline controls; the picture cycles through a few Omarchy themes" width="100%">
</picture>

Verified only on my pad, but the treadmill side is standard Bluetooth FTMS
(`0x1826`), so others may largely work. Steps are the one vendor-specific
part ([docs/gatt-dump.md](docs/gatt-dump.md)); `strideMeters` derives them
from distance elsewhere. Got a **KingSmith WalkingPad**? Use
[msegoviadev/omarchy-walkingpad](https://github.com/msegoviadev/omarchy-walkingpad)
or
[shllg/omarchy-walkingpad-control](https://github.com/shllg/omarchy-walkingpad-control)
instead — both good.

## Install

```bash
sudo pacman -S python-bleak     # the only dependency beyond base Omarchy
omarchy plugin add https://github.com/ncr/omarchy-spacewalk.git --enable
```

Add the **Spacewalk** widget to the bar (Setup > Plugins) and power-cycle the
treadmill — it advertises only briefly, and it takes one connection at a
time, so keep the Urevo phone app closed. `./probe.py` finds its address;
put it in the widget settings to skip scanning on every connect.

Remove with `omarchy plugin remove io.github.ncr.spacewalk`; the day history
lives in `~/.local/state/omarchy-spacewalk/`.

The plugin starts its own transient **user** service on first activation.
No root access, service installation, or enable command is needed. Removing
the plugin automatically disconnects Bluetooth and stops the service within
about eight seconds; systemd then discards its registration. A brief plugin
update or UI reload does not stop it. Step history is retained. At the next
login the enabled plugin starts the service again. Installing the existing
`python-bleak` system dependency still uses the package manager as shown above.

## Use

Click the pill for the panel; middle-click starts or stops the belt. Only
midnight resets the day counter — the bridge sums increments across walks, so
the treadmill clearing its own counters on stop changes nothing. With the
belt stopped, the panel shows the values to apply on start.

### Heart rate

Put on a Bluetooth chest strap and the panel grows a heart rate chart for the
day, above the history grid. Any strap with the standard Heart Rate service
(`0x180D`) should do; mine is a Magene. No pairing — the bridge looks for a
strap while the treadmill is connected and takes the first one it hears. The
strap accepts one connection, so a watch or phone app holding it keeps it
from the computer.

The bridge knows the belt's speed and incline, so it can tell a rise that
follows a faster belt from one that came out of nowhere. It pins notes to the
chart — hover a marker to read it:

- **jump / fall** — 20 bpm or more within 20 s, held for 5 s, with speed and
  incline unchanged for the last 90 s
- **high** — above `heartLimit` for 30 s
- **drift** — 10 bpm or more above minutes 5–10 of a steady stretch, after
  20 min at the same speed and incline
- **recovery** — how far the rate fell in the minute after the belt stopped
- a small dot — the belt started or stopped; a ring — speed or incline
  changed during the walk

The mouse wheel zooms the chart, anchored to the right edge: the newest point
stays put and the wheel sets how many minutes back from it fit the width (two
minutes at the closest; all the way out is the whole day). The clock times
under the chart follow the zoom too, from hours apart down to 10 s apart,
always at round moments. The closer the
zoom, the finer the line: zoomed out, a spot on it is the mean of up to a
minute of readings; zoomed in, every second gets its own and the line moves
once a second.

The line is dimmed while the belt stands, takes the accent colour while you
walk, and the urgent colour while the belt runs with nobody on it (no steps
for a few seconds), so the walks show at a glance.

The thresholds are first guesses (`HeartNotes` in `spacewalk-bridge.py`). This
is a training aid, not a medical device: the strap sends an averaged rate, so
single irregular beats never show up, and its RR field is just 60000 / bpm.
Points (one a second) and notes are kept in
`~/.local/state/omarchy-spacewalk/heart-YYYY-MM-DD.jsonl`.

Settings: `heartAddress` (empty = any strap, `off` = none), `heartLimit`
(150 bpm, 0 = off), `address`, `dailyGoal` (10000), `startSpeed` (2.5 km/h),
`startIncline` (3%), `strideMeters` (0 = steps from the treadmill),
`phonePort` (0 = Apple Health sync off; set it — say 8787 — and walks flow to
an iPhone over Tailscale via Shortcuts, see
[docs/apple-health.md](docs/apple-health.md)).

## When it sulks

The Bluetooth bridge runs under `omarchy-spacewalk.service`, independently of
the bar. Reloading any plugin only replaces the UI client. A private Unix
socket carries events and commands; one locked bridge owns the connection.
Each incoming counter update is written atomically and synced before the UI
receives the new total, including a treadmill counter resetting to zero.

The bridge rescans and reconnects by itself. If it gets stuck after sleep or
a Bluetooth adapter reconnect, open the Spacewalk panel and press **R** to
restart its connection service. This preserves saved steps and does not start
or stop the belt. Inspect it with:

```bash
systemctl --user status omarchy-spacewalk.service
journalctl --user -u omarchy-spacewalk.service -f
tail -F ~/.local/state/omarchy-spacewalk/bridge.log
omarchy-shell spacewalk dump
```

`bridge.log` is capped: past 5 MB it becomes `bridge.log.1` (replacing the
previous one) and a new file starts, hence `tail -F`. On startup the bridge
also removes `*.tmp` leftovers of interrupted state writes older than an hour.

If necessary, `systemctl --user restart omarchy-spacewalk.service` reconnects
Bluetooth without sending a belt start/stop command. UI files hot-reload;
restart the service after editing Python backend code. Avoid power-cycling a
running treadmill to repair a connection: it can erase counters the computer
has not received yet. Already persisted steps survive either restart.

Regression checks: `python3 -m unittest -v test_spacewalk.py` (uses a fake
bridge, temporary state and a local Unix socket; does not control the belt).

## On D-Bus, for other desktops

The same bridge can also run as a systemd user service on the D-Bus session
bus, for desktops whose widgets talk D-Bus rather than a Unix socket — KDE
Plasma's QML among them. `kde/install-service.sh` sets it up;
[docs/dbus.md](docs/dbus.md) describes the interface and how to use it from
QML.

## Who this is for

Honestly — I built this for my own desk and don't expect anyone else to run
it, though maybe one or two people with the same pad will turn up. If that's
you, or you just enjoy this kind of thing, follow me on
[X](https://x.com/JacekBecela) — more of it coming.

## License

[MIT](LICENSE)
