"""The service host's face on the D-Bus session bus.

For desktops whose widgets cannot keep a Unix socket open: KDE Plasma's QML
reaches the session bus but not sockets. The host runs this next to the
socket; both carry the same bridge.

    bus name   io.github.ncr.Spacewalk
    object     /io/github/ncr/Spacewalk
    interface  io.github.ncr.Spacewalk1

Properties hold what the panel shows and announce every change through
org.freedesktop.DBus.Properties.PropertiesChanged. Methods hand commands to
the bridge the way a socket client does. Structured values (the history, the
heart rate chart) travel as JSON text — QML parses it in one call, and the
chart is too big for a property anyway. docs/dbus.md describes it all.
"""

import asyncio
import json
import math

from dbus_fast import BusType, DBusError, ErrorType, RequestNameReply
from dbus_fast.aio import MessageBus
from dbus_fast.constants import NameFlag
from dbus_fast.service import PropertyAccess, ServiceInterface, dbus_property, method, signal

BUS_NAME = "io.github.ncr.Spacewalk"
OBJECT_PATH = "/io/github/ncr/Spacewalk"
INTERFACE = "io.github.ncr.Spacewalk1"
NOT_RUNNING = INTERFACE + ".Error.NotRunning"

# Six hours, one point a second — what the panel keeps too.
HEART_POINTS_MAX = 21600

# Name, D-Bus type, value until the bridge says otherwise. A target the
# bridge does not know yet is NaN: D-Bus has no null.
PROPERTIES = [
    # The treadmill link: starting | releasing | scanning | found | connecting |
    # connected | disconnected | not_found | stopping
    ("LinkState", "s", "starting"),
    ("Device", "s", ""),                 # the treadmill's name, once found
    ("BeltState", "s", "stopped"),       # running | paused | stopped
    ("Speed", "d", 0.0),                 # km/h, as measured
    ("Incline", "d", 0.0),               # %
    ("TargetSpeed", "d", math.nan),      # what start and the arrows ask for
    ("TargetIncline", "d", math.nan),
    ("SessionElapsedS", "u", 0),         # the treadmill's own clock for this walk
    ("Day", "s", ""),                    # YYYY-MM-DD the totals below belong to
    ("DaySteps", "u", 0),
    ("DayDistanceM", "u", 0),
    ("DayKcal", "u", 0),
    ("DayElapsedS", "u", 0),
    ("History", "s", "{}"),              # JSON: {"2026-09-01": {steps, distance_m, kcal, elapsed_s}}
    ("Phase", "s", ""),                  # start progress: sending | control | starting | running | ...
    ("PhaseText", "s", ""),
    ("LastError", "s", ""),
    ("HeartState", "s", "idle"),         # off | idle | scanning | connecting | connected
    ("HeartBpm", "u", 0),                # 0: no reading
    ("HeartDevice", "s", ""),
    ("HeartBattery", "i", -1),           # %, -1: unknown
]


def _getter(name, signature):
    def get(self):
        return self.state[name]
    get.__name__ = name
    get.__annotations__ = {"return": signature}
    return dbus_property(access=PropertyAccess.READ)(get)


def _count(value):
    try:
        return max(0, int(round(float(value))))
    except (TypeError, ValueError, OverflowError):
        return 0


def _number(value, default=0.0):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number


def _finite(value, what):
    if not math.isfinite(value):
        raise DBusError(ErrorType.INVALID_ARGS, f"{what} must be a finite number")
    return value


class Spacewalk(ServiceInterface):
    def __init__(self, host):
        super().__init__(INTERFACE)
        self.host = host
        self.state = {name: value for name, _, value in PROPERTIES}
        self.types = {name: signature for name, signature, _ in PROPERTIES}
        self.heart_points = []
        self.heart_notes = []
        self.tasks = set()

    # ---- events from the bridge

    def update(self, **values):
        changed = {}
        for name, value in values.items():
            if value is None:
                continue
            if self.types[name] == "d" and math.isnan(value) and math.isnan(self.state[name]):
                continue                      # NaN != NaN, yet nothing changed
            if self.state[name] != value:
                self.state[name] = value
                changed[name] = value
        if changed:
            self.emit_properties_changed(changed)

    def apply(self, event):
        """Host listener: one bridge event in, property changes and signals out."""
        kind = event.get("t")
        if kind == "status":
            self.update(LinkState=str(event.get("state", "")))
            if event.get("device"):
                self.update(Device=str(event["device"]))
        elif kind == "error":
            self.update(LastError=str(event.get("msg") or ""))
        elif kind == "phase":
            self.update(Phase=str(event.get("phase") or ""), PhaseText=str(event.get("text") or ""))
        elif kind == "history":
            self.update(History=json.dumps(event.get("days") or {}, sort_keys=True))
        elif kind == "belt":
            self.update(BeltState=str(event.get("state") or "stopped"))
        elif kind == "targets":
            # The panel keeps its own value while the bridge has none; NaN says so.
            self.update(TargetSpeed=_number(event.get("target_speed"), math.nan),
                        TargetIncline=_number(event.get("target_incline"), math.nan))
        elif kind == "data":
            values = {}
            if "speed" in event:
                values["Speed"] = _number(event["speed"])
            if "incline" in event:
                values["Incline"] = _number(event["incline"])
            if "elapsed_s" in event:
                values["SessionElapsedS"] = _count(event["elapsed_s"])
            if "day" in event:
                values["Day"] = str(event["day"])
            for key, name in (("day_steps", "DaySteps"), ("day_distance_m", "DayDistanceM"),
                              ("day_kcal", "DayKcal"), ("day_elapsed_s", "DayElapsedS")):
                if key in event:
                    values[name] = _count(event[key])
            self.update(**values)
        elif kind == "heart":
            battery = event.get("battery")
            self.update(HeartState=str(event.get("state") or "idle"),
                        HeartBpm=_count(event.get("bpm")),
                        HeartDevice=str(event.get("device") or ""),
                        HeartBattery=-1 if battery is None else int(battery))
        elif kind == "hr_point":
            point = event.get("point")
            last = self.heart_points[-1][0] if self.heart_points else 0
            # A point the series reply already carried arrives once more right after it.
            if point and point[0] > last:
                self.heart_points.append(point)
                del self.heart_points[:-HEART_POINTS_MAX]
                self.HeartPoint(json.dumps(point))
        elif kind == "hr_note":
            note = {k: event.get(k) for k in ("at", "kind", "text", "bpm")}
            self.heart_notes.append(note)
            self.HeartNote(json.dumps(note))
        elif kind == "hr_series":
            if event.get("reset"):
                self.heart_points = []
                self.heart_notes = list(event.get("notes") or [])
            if event.get("points"):
                self.heart_points.extend(event["points"])
                del self.heart_points[:-HEART_POINTS_MAX]
            self.HeartSeriesChanged()
        elif kind == "lifecycle" and event.get("event") == "started":
            # A fresh bridge holds today's chart in its file; ask for it.
            self.spawn(self.host.send(b"heart-series"))

    def spawn(self, coroutine):
        task = asyncio.get_running_loop().create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    # ---- commands

    async def command(self, line):
        if not await self.host.send(line.encode()):
            raise DBusError(NOT_RUNNING, "the bridge is not running")

    @method()
    async def Start(self):
        """Starts or resumes the belt; speed and incline follow the targets."""
        await self.command("start")

    @method()
    async def Stop(self):
        await self.command("stop")

    @method()
    async def Pause(self):
        await self.command("pause")

    @method()
    async def SetSpeed(self, kmh: "d"):
        """A stopped belt keeps it as the target for the next start."""
        if _finite(kmh, "speed") < 0:
            raise DBusError(ErrorType.INVALID_ARGS, "speed must not be negative")
        await self.command(f"speed {kmh:.1f}")

    @method()
    async def SetIncline(self, percent: "d"):
        await self.command(f"incline {_finite(percent, 'incline'):g}")

    @method()
    async def Configure(self, args: "as"):
        """The bridge's command line (--address, --heart-address, --speed,
        ...). A change restarts the bridge; the same arguments again do nothing."""
        try:
            await self.host.configure(list(args))
        except ValueError as exc:
            raise DBusError(ErrorType.INVALID_ARGS, str(exc)) from exc

    @method()
    async def Reconnect(self):
        """A fresh bridge and Bluetooth link with the same settings. Saved
        steps stay; the belt is neither started nor stopped."""
        self.update(LastError="")
        if not await self.host.restart():
            raise DBusError(NOT_RUNNING, "the bridge is not configured yet")

    @method()
    def GetHeartSeries(self) -> "s":
        """Today's chart as JSON: {"points": [[unix time, bpm, speed,
        incline, walking], ...], "notes": [{at, kind, text, bpm}, ...]}."""
        return json.dumps({"points": self.heart_points, "notes": self.heart_notes})

    # ---- signals

    @signal()
    def HeartPoint(self, point) -> "s":
        """One new chart point, JSON as in GetHeartSeries."""
        return point

    @signal()
    def HeartNote(self, note) -> "s":
        return note

    @signal()
    def HeartSeriesChanged(self):
        """The chart was reloaded; fetch it again with GetHeartSeries."""


for _name, _signature, _ in PROPERTIES:
    setattr(Spacewalk, _name, _getter(_name, _signature))


async def serve(host, bus_address=None):
    """Exports the interface for `host` and takes the bus name. Returns the
    bus; disconnecting it gives the name up. Raises RuntimeError when another
    process owns the name."""
    bus = await (MessageBus(bus_address=bus_address) if bus_address
                 else MessageBus(bus_type=BusType.SESSION)).connect()
    interface = Spacewalk(host)
    bus.export(OBJECT_PATH, interface)
    for event in host.cache.values():
        interface.apply(event)
    host.listeners.append(interface.apply)
    reply = await bus.request_name(BUS_NAME, NameFlag.DO_NOT_QUEUE)
    if reply not in (RequestNameReply.PRIMARY_OWNER, RequestNameReply.ALREADY_OWNER):
        host.listeners.remove(interface.apply)
        bus.disconnect()
        raise RuntimeError(f"{BUS_NAME} is already taken on the session bus")
    return bus
