# The bridge on D-Bus

`spacewalk-service.py --host --dbus` serves the treadmill on the D-Bus
session bus, next to the Unix socket the Omarchy panel uses. It is meant for
desktops whose widgets talk D-Bus — KDE Plasma's QML can reach the session
bus but not a socket. Both carry the same bridge; only one host runs at a time.

| | |
|---|---|
| Bus name | `io.github.ncr.Spacewalk` |
| Object | `/io/github/ncr/Spacewalk` |
| Interface | `io.github.ncr.Spacewalk1` |

## Install as a user service

```bash
sudo apt install python3-bleak      # or: sudo pacman -S python-bleak
kde/install-service.sh              # unit + D-Bus activation, enabled and started
kde/install-service.sh uninstall
```

This installs `spacewalk-widget.service` in `~/.config/systemd/user`
(`Type=dbus`, started with the graphical session) and a D-Bus activation file
in `~/.local/share/dbus-1/services`, so the first call to the bus name starts
the service too. The service runs the code from this checkout.

With no saved settings the host starts a bridge with defaults (any FTMS
treadmill, any heart rate strap), so steps count before a widget configures
it. Settings from `Configure` are saved and survive restarts.

```bash
systemctl --user status spacewalk-widget.service
journalctl --user -u spacewalk-widget.service -f
tail -F ~/.local/state/omarchy-spacewalk/bridge.log
```

## Properties

All read-only; every change goes out in
`org.freedesktop.DBus.Properties.PropertiesChanged`, carrying only the values
that changed — about one signal a second while walking.

| Property | Type | Meaning |
|---|---|---|
| `LinkState` | s | `starting`, `releasing`, `scanning`, `found`, `connecting`, `connected`, `disconnected`, `not_found`, `stopping` |
| `Device` | s | the treadmill's name, once found |
| `BeltState` | s | `running`, `paused`, `stopped` — from the treadmill's status messages, so a belt already running when the bridge connected reads `stopped` until it changes; tell walking by `Speed` |
| `Speed`, `Incline` | d | the last reading, km/h and %; current only while `LinkState` is `connected` |
| `TargetSpeed`, `TargetIncline` | d | what start and the arrows ask for; NaN while the bridge has none |
| `SessionElapsedS` | u | the treadmill's own clock for this walk |
| `Day` | s | `YYYY-MM-DD` the totals below belong to |
| `DaySteps`, `DayDistanceM`, `DayKcal`, `DayElapsedS` | u | today's totals, summed across walks |
| `History` | s | JSON `{"2026-09-01": {"steps", "distance_m", "kcal", "elapsed_s"}, ...}` as of the bridge's start; patch today in from the totals |
| `Phase`, `PhaseText` | s | start progress: `sending`, `control`, `starting`, `unconfirmed`, `running`, `stopped`, `error`, ... |
| `LastError` | s | the latest error, for the panel |
| `HeartState` | s | `off`, `idle`, `scanning`, `connecting`, `connected` |
| `HeartBpm` | u | 0 without a reading |
| `HeartDevice` | s | the strap's name |
| `HeartBattery` | i | %, -1 unknown |

## Methods

| Method | Does |
|---|---|
| `Start()` | starts or resumes the belt; speed and incline follow the targets |
| `Stop()`, `Pause()` | as on the console; a paused belt resumes with `Start` |
| `SetSpeed(d kmh)`, `SetIncline(d percent)` | a stopped belt keeps them as targets for the next start |
| `Configure(as args)` | the bridge's command line: `--address`, `--heart-address`, `--heart-limit`, `--speed`, `--incline`, `--stride`, `--serve`, `--steps-uuid`; a change restarts the bridge, the same arguments do nothing |
| `Reconnect()` | a fresh bridge and Bluetooth link with the same settings; saved steps stay, the belt is neither started nor stopped |
| `GetHeartSeries() → s` | today's chart, JSON `{"points": [[unix time, bpm, speed, incline, walking], ...], "notes": [{"at", "kind", "text", "bpm"}, ...]}` |

Belt commands fail with `io.github.ncr.Spacewalk1.Error.NotRunning` while no
bridge runs; non-finite or negative values and unknown `Configure` options
with `org.freedesktop.DBus.Error.InvalidArgs`.

## Signals

| Signal | Carries |
|---|---|
| `HeartPoint(s)` | one new chart point, JSON as in `GetHeartSeries` |
| `HeartNote(s)` | one new note, JSON |
| `HeartSeriesChanged()` | the chart was reloaded; fetch it with `GetHeartSeries` |

## From the command line

```bash
busctl --user introspect io.github.ncr.Spacewalk /io/github/ncr/Spacewalk
busctl --user get-property io.github.ncr.Spacewalk /io/github/ncr/Spacewalk io.github.ncr.Spacewalk1 DaySteps
busctl --user call io.github.ncr.Spacewalk /io/github/ncr/Spacewalk io.github.ncr.Spacewalk1 SetSpeed d 3.0
# `--` keeps busctl from taking the bridge's options for its own:
busctl --user call io.github.ncr.Spacewalk /io/github/ncr/Spacewalk io.github.ncr.Spacewalk1 \
    Configure as 2 -- --address 54:50:00:0D:E6:5A
gdbus monitor --session --dest io.github.ncr.Spacewalk
```

## From QML in KDE Plasma

Plasma 6 ships `org.kde.plasma.workspace.dbus`. It is not a frozen public
API, but on Plasma 6.6 this works (checked against the service):

```qml
import org.kde.plasma.workspace.dbus as DBus

DBus.Properties {
    id: treadmill
    busType: DBus.BusType.Session
    service: "io.github.ncr.Spacewalk"
    path: "/io/github/ncr/Spacewalk"
    iface: "io.github.ncr.Spacewalk1"
}
// Values come wrapped: take .value, or `+ 1` concatenates strings.
// Bindings follow PropertiesChanged; a value is undefined until the first read.
readonly property int steps: treadmill.properties.DaySteps ? treadmill.properties.DaySteps.value : 0

DBus.SignalWatcher {
    busType: DBus.BusType.Session
    service: "io.github.ncr.Spacewalk"
    path: "/io/github/ncr/Spacewalk"
    iface: "io.github.ncr.Spacewalk1"
    // A signal calls the function named "dbus" + its name.
    function dbusHeartPoint(point) { var p = JSON.parse(point) }
    function dbusHeartSeriesChanged() { /* GetHeartSeries again */ }
}

function call(member, args) {
    var message = { service: "io.github.ncr.Spacewalk", path: "/io/github/ncr/Spacewalk",
                    iface: "io.github.ncr.Spacewalk1", member: member }
    // Pass `arguments` but no `signature` — with one set, the call goes out
    // without its arguments.
    if (args) message.arguments = args
    DBus.SessionBus.asyncCall(message,
        function(reply) { /* reply.value */ },
        function(reply) { console.warn(reply.error.name, reply.error.message) })
}
// call("SetSpeed", [new DBus.double(3.0)]); call("Configure", [["--address", "..."]])
```
