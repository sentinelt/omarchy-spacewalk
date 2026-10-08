#!/usr/bin/env python3
"""Bridge between the treadmill (Bluetooth, FTMS) and the Omarchy plugin.

Emits one JSON object per line on stdout:

    {"t":"status","state":"connected","device":"..."}
    {"t":"data","speed":2.5,"incline":3.0,"distance_m":1840,"kcal":62,
     "elapsed_s":1620,"steps":2705,"day_steps":7412,...}
    {"t":"error","msg":"..."}

With a heart rate strap in reach (any Bluetooth strap with the standard Heart
Rate service), also:

    {"t":"heart","state":"connected","bpm":104,"device":"...","battery":100}
        state: off | idle | scanning | connecting | connected
    {"t":"hr_point","point":[1758196800,104,2.5,3,1]}   one a second
    {"t":"hr_note","at":1758196800,"kind":"jump","text":"..."}
    {"t":"hr_series","reset":true,"notes":[...]}        reply to heart-series,
    {"t":"hr_series","points":[[...],...]}              in chunks

Reads one command per line from stdin:

    start | stop | pause | speed 2.5 | incline 3 | reset-day | ping | heart-series
"""

import argparse
import asyncio
import collections
import contextlib
import fcntl
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
from datetime import date, datetime, timedelta
from pathlib import Path

try:
    from bleak import BleakClient, BleakScanner
    from bleak.exc import BleakError
except ImportError:
    # Without bleak there is nothing to talk Bluetooth with. This error
    # lands in the panel, so it says outright what to install.
    print(json.dumps({"t": "error",
                      "msg": "python-bleak is not installed — sudo pacman -S python-bleak"}),
          flush=True)
    sys.exit(66)

FTMS_SERVICE = "00001826-0000-1000-8000-00805f9b34fb"
TREADMILL_DATA = "00002acd-0000-1000-8000-00805f9b34fb"
CONTROL_POINT = "00002ad9-0000-1000-8000-00805f9b34fb"
MACHINE_STATUS = "00002ada-0000-1000-8000-00805f9b34fb"

HEART_SERVICE = "0000180d-0000-1000-8000-00805f9b34fb"
HEART_MEASUREMENT = "00002a37-0000-1000-8000-00805f9b34fb"
BATTERY_LEVEL = "00002a19-0000-1000-8000-00805f9b34fb"

STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "omarchy-spacewalk"

# Control point opcodes (FTMS 4.16.1)
OP_REQUEST_CONTROL = 0x00
OP_RESET = 0x01
OP_SET_SPEED = 0x02
OP_SET_INCLINATION = 0x03
OP_START = 0x07
OP_STOP = 0x08
RESPONSE_CODE = 0x80
RESULT_SUCCESS = 0x01

RESULT_NAMES = {
    0x01: "accepted",
    0x02: "not supported",
    0x03: "bad parameter",
    0x04: "rejected",
    0x05: "no control",
}


LOG_PATH = STATE_DIR / "bridge.log"
SESSIONS_PATH = STATE_DIR / "sessions.jsonl"
OPEN_SESSION_PATH = STATE_DIR / "session-open.json"
TARGETS_PATH = STATE_DIR / "targets.json"

# After this many seconds without movement the walk counts as finished. The
# treadmill stops itself when nobody is standing on it, so a short break to fix
# something at the desk should not split a walk into two sessions.
SESSION_IDLE_GAP = 90

# bridge.log gets about one line a second while the treadmill is connected and
# nothing else trims it (70 MB by 2026-09-18). Past this size it becomes
# bridge.log.1, replacing the previous one, so both never exceed twice the cap.
LOG_MAX_BYTES = 5 * 1024 * 1024

# Size of bridge.log as this process knows it: measured once, then advanced by
# what is written, so a line does not cost a stat. None means "measure again".
log_bytes = None


def append_log(text: str):
    global log_bytes
    data = text.encode("utf-8")
    try:
        if log_bytes is None:
            try:
                log_bytes = os.lstat(LOG_PATH).st_size
            except FileNotFoundError:
                log_bytes = 0
        if log_bytes > 0 and log_bytes + len(data) > LOG_MAX_BYTES:
            os.replace(LOG_PATH, LOG_PATH.with_name(LOG_PATH.name + ".1"))
            log_bytes = 0
        with LOG_PATH.open("ab") as fh:
            fh.write(data)
        log_bytes += len(data)
    except OSError:
        # The file may have been moved or removed under us; the count is no
        # longer trustworthy, so the next line measures it afresh.
        log_bytes = None
        raise


def emit(obj, log=True):
    line = json.dumps(obj, separators=(",", ":"))
    try:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
    except BrokenPipeError:
        # A vanished UI must not interrupt checkpointing or BLE cleanup.
        pass
    if not log:
        return  # the live heart rate and series replies would only bloat the log
    # Copy to a file: the shell consumes the bridge's stdout, so without this
    # there is no way to see what the treadmill says while the plugin runs.
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        append_log(f"{datetime.now().isoformat(timespec='seconds')} {line}\n")
    except OSError:
        pass


def status(state, **extra):
    emit({"t": "status", "state": state, **extra})


def error(msg):
    emit({"t": "error", "msg": str(msg)})


# ------------------------------------------------------------- state files

# The state dir is the user's own, but the plugin should not be a lever into
# the rest of their files: every state read refuses a swapped-in symlink
# (O_NOFOLLOW), a planted device or directory (the regular-file and owner
# check), and a file rigged to exhaust memory before it is parsed (the cap);
# O_NONBLOCK keeps a planted fifo from hanging the open. Every write lands
# through a fresh unpredictable temp file and an atomic rename, so it cannot be
# redirected through a pre-placed name and never leaves a half-written file.
MAX_STATE_BYTES = 8 * 1024 * 1024


def read_state(path: Path, limit: int = MAX_STATE_BYTES) -> str | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as exc:
        error(f"cannot open {path}: {exc}")
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
            error(f"refusing {path}: not a regular file owned by us")
            return None
        chunks = []
        remaining = limit + 1
        while remaining > 0:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > limit:
            error(f"refusing {path}: larger than the {limit} byte cap")
            return None
        return data.decode("utf-8", "replace")
    finally:
        os.close(fd)


def write_state(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def append_state(path: Path, text: str, sync: bool = True):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        data = text.encode("utf-8")
        while data:
            data = data[os.write(fd, data):]
        if sync:
            os.fsync(fd)
    finally:
        os.close(fd)


# A bridge killed between mkstemp and os.replace in write_state() leaves its
# temp file behind for good (50 copies of targets.json.*.tmp from 2026-09-08).
# Anything younger than this may still belong to a write in progress.
STALE_TEMP_SECONDS = 3600


def is_state_temp_name(name: str) -> bool:
    """True only for the names write_state() makes: <state file>.<random>.tmp.
    Everything else in the state dir is the walker's history and stays."""
    if not name.endswith(".tmp"):
        return False
    target, _, random_part = name[:-len(".tmp")].rpartition(".")
    if not target or not random_part:
        return False
    if target in (TARGETS_PATH.name, SESSIONS_PATH.name, OPEN_SESSION_PATH.name):
        return True
    day, _, suffix = target.rpartition(".")
    if suffix != "json" or len(day) != 10:
        return False
    try:
        date.fromisoformat(day)
    except ValueError:
        return False
    return True


def remove_stale_temp_files() -> int:
    removed = 0
    try:
        entries = list(os.scandir(STATE_DIR))
    except OSError:
        return 0
    now = time.time()
    for entry in entries:
        if not is_state_temp_name(entry.name):
            continue
        try:
            st = entry.stat(follow_symlinks=False)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
                continue
            if now - st.st_mtime < STALE_TEMP_SECONDS:
                continue
            os.unlink(entry.path)
            removed += 1
        except OSError:
            continue
    return removed


# ------------------------------------------------------------------ targets

def load_targets(defaults: dict) -> dict:
    """The last speed and incline the walker chose, if they were chosen under
    the same settings. The bridge used to start from the settings every time,
    so a plugin reload put the panel back to 3 % while the belt stood at 0 —
    and the next start raised it for real. A changed setting still wins: it
    is the one thing the walker changed on purpose since."""
    text = read_state(TARGETS_PATH)
    if text is None:
        return defaults
    try:
        raw = json.loads(text)
        if raw.get("defaults") != defaults:
            return defaults
        return {"speed": raw.get("speed", defaults["speed"]),
                "incline": raw.get("incline", defaults["incline"])}
    except (ValueError, TypeError, AttributeError) as exc:
        error(f"cannot read {TARGETS_PATH}: {exc}")
        return defaults


# ------------------------------------------------------------- treadmill data

def parse_treadmill_data(data: bytes) -> dict:
    """Parses a Treadmill Data packet (0x2ACD).

    The first two bytes are flags; each field is present only when its bit
    says so, and the fields come in the order of table 4.9.1.1 of the FTMS
    spec. Bit 0 is inverted: 0 means "instantaneous speed is in the packet".
    """
    if len(data) < 2:
        return {}
    flags = int.from_bytes(data[0:2], "little")
    pos = 2
    out = {}

    def take(width, signed=False):
        nonlocal pos
        if pos + width > len(data):
            raise ValueError("packet shorter than its flags claim")
        value = int.from_bytes(data[pos:pos + width], "little", signed=signed)
        pos += width
        return value

    try:
        if not (flags & 0x0001):                      # bit 0: More Data
            out["speed"] = take(2) / 100.0            # km/h
        if flags & 0x0002:
            out["avg_speed"] = take(2) / 100.0
        if flags & 0x0004:
            out["distance_m"] = take(3)
        if flags & 0x0008:
            out["incline"] = take(2, signed=True) / 10.0     # %
            out["ramp_angle"] = take(2, signed=True) / 10.0  # degrees
        if flags & 0x0010:
            out["elevation_pos_m"] = take(2) / 10.0
            out["elevation_neg_m"] = take(2) / 10.0
        if flags & 0x0020:
            out["pace"] = take(1)
        if flags & 0x0040:
            out["avg_pace"] = take(1)
        if flags & 0x0080:
            out["kcal"] = take(2)
            out["kcal_per_hour"] = take(2)
            per_min = take(1)
            out["kcal_per_min"] = None if per_min == 0xFF else per_min
        if flags & 0x0100:
            out["heart_rate"] = take(1)
        if flags & 0x0200:
            out["met"] = take(1) / 10.0
        if flags & 0x0400:
            out["elapsed_s"] = take(2)
        if flags & 0x0800:
            out["remaining_s"] = take(2)
        # Bit 13 does not exist in the FTMS spec — Urevo put a step counter
        # (uint24) here. Verified on URTM024: 179 steps per 100 m of walking,
        # while the time in the same packet grew by exactly 1/s.
        if flags & 0x2000:
            out["steps"] = take(3)
    except ValueError as exc:
        error(f"{exc}: flags 0x{flags:04x}, data {data.hex(' ')}")

    return out


# --------------------------------------------------------------- daily totals

class DayTotals:
    """Daily total: the treadmill zeroes its counters on every start, so we
    add increments instead of overwriting the sum."""

    FIELDS = ("steps", "distance_m", "kcal", "elapsed_s")

    def __init__(self):
        self.day = date.today()
        self.totals = {f: 0.0 for f in self.FIELDS}
        self.session = {f: None for f in self.FIELDS}
        self.dirty = False
        self.load()

    @property
    def path(self) -> Path:
        return STATE_DIR / f"{self.day.isoformat()}.json"

    def load(self):
        text = read_state(self.path)
        if text is None:
            return
        try:
            raw = json.loads(text)
            for f in self.FIELDS:
                self.totals[f] = float(raw.get(f, 0))
            # The treadmill's counters as last credited, saved together with
            # the totals. A bridge that restarts mid-walk continues from them:
            # the belt kept counting, so whatever the counter shows above the
            # reference is walked distance, not a new starting point. Before
            # this, every restart made the first reading the reference and
            # the walk in between vanished (13 minutes on 2026-09-07).
            ref = raw.get("session_ref") or {}
            for f in self.FIELDS:
                if ref.get(f) is not None:
                    self.session[f] = float(ref[f])
        except (ValueError, TypeError) as exc:
            error(f"cannot load {self.path}: {exc}")

    def save(self):
        if not self.dirty:
            return True
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            payload = {f: round(self.totals[f], 2) for f in self.FIELDS}
            payload["session_ref"] = {f: self.session[f] for f in self.FIELDS
                                      if self.session[f] is not None}
            payload["updated"] = datetime.now().isoformat(timespec="seconds")
            write_state(self.path, json.dumps(payload, indent=2))
            self.dirty = False
            return True
        except OSError as exc:
            error(f"cannot save {self.path}: {exc}")
            return False

    def roll_over_if_needed(self):
        today = date.today()
        if today == self.day:
            return
        self.save()
        self.day = today
        self.totals = {f: 0.0 for f in self.FIELDS}
        self.session = {f: None for f in self.FIELDS}
        self.load()
        emit({"t": "history", "days": read_history()})

    def update(self, sample: dict):
        """Takes values cumulative since the session start and adds the delta.

        Distance, calories and time count only when the same reading also
        gained steps. The treadmill counts distance from belt movement but
        steps from the person — without this condition an empty, spinning belt
        credited the day with meters and calories nobody walked (150 m instead
        of 30).
        """
        self.roll_over_if_needed()

        deltas = {}
        for f in self.FIELDS:
            if f not in sample or sample[f] is None:
                continue
            value = float(sample[f])
            previous = self.session[f]
            if previous is None:
                # First reading after a start, or the first ever today: no
                # telling how much of this counter was already credited, so it
                # serves only as a reference point. Without this, pressing
                # Start credited the previous walk's whole counter once more.
                self.session[f] = value
                self.dirty = True
                deltas[f] = 0.0
                continue
            if value < previous:
                # The treadmill zeroed the counter — count from zero.
                previous = 0.0
            deltas[f] = value - previous
            if self.session[f] != value:
                self.dirty = True
            self.session[f] = value

        if deltas.get("steps", 0) <= 0:
            return {}

        applied = {}
        for f, delta in deltas.items():
            if delta > 0:
                self.totals[f] += delta
                applied[f] = delta
                self.dirty = True
        return applied

    def new_session(self):
        """Drops the reference point instead of zeroing it: only the first
        reading says what to count increments from."""
        self.session = {f: None for f in self.FIELDS}

    def snapshot(self) -> dict:
        return {
            "day": self.day.isoformat(),
            "day_steps": int(self.totals["steps"]),
            "day_distance_m": int(self.totals["distance_m"]),
            "day_kcal": int(self.totals["kcal"]),
            "day_elapsed_s": int(self.totals["elapsed_s"]),
        }


# -------------------------------------------------------------------- history

HISTORY_DAYS = 120


def read_history(days: int = HISTORY_DAYS) -> dict:
    """Totals for recent days, straight from the day files. The panel draws
    its grid from this, so we read the directory once at startup, not every
    time the panel opens."""
    out = {}
    today = date.today()
    for offset in range(days):
        day = today - timedelta(days=offset)
        path = STATE_DIR / f"{day.isoformat()}.json"
        text = read_state(path)
        if text is None:
            continue
        try:
            raw = json.loads(text)
        except ValueError:
            continue
        out[day.isoformat()] = {
            "steps": int(float(raw.get("steps", 0))),
            "distance_m": int(float(raw.get("distance_m", 0))),
            "kcal": int(float(raw.get("kcal", 0))),
            "elapsed_s": int(float(raw.get("elapsed_s", 0))),
        }
    return out


# ----------------------------------------------------------------- heart rate

# The strap sends two readings a second; a point is their mean. The chart
# averages further by itself when zoomed out, and draws every point zoomed in.
HEART_POINT_SECONDS = 1
HEART_SERIES_MAX = 21600    # six hours of points: what the chart gets
# The service host reads the bridge's stdout with asyncio's default 64 KB line
# limit and dies on a longer line, so the series travels in chunks.
HEART_SERIES_CHUNK = 500
HEART_NOTES_MAX = 200


def parse_heart_rate(data: bytes) -> dict:
    """Parses a Heart Rate Measurement packet (0x2A37).

    Only the rate and the skin contact bits are taken. The packet can also
    carry RR intervals, but the Magene strap this was written against fills
    that field with 60000 / bpm (checked over 60 packets) — a number derived
    from the averaged rate, with nothing beat-to-beat in it.
    """
    if len(data) < 2:
        return {}
    flags = data[0]
    if flags & 0x01:
        if len(data) < 3:
            return {}
        bpm = int.from_bytes(data[1:3], "little")
    else:
        bpm = data[1]
    # Bit 2: the strap can sense skin contact at all; bit 1: it does right now.
    contact = bool(flags & 0x02) if flags & 0x04 else None
    return {"bpm": bpm, "contact": contact}


def load_text(speed: float, incline: float) -> str:
    if speed <= 0:
        return "with the belt stopped"
    return f"at a steady {speed:.1f} km/h, {round(incline)}%"


class HeartNotes:
    """Turns the heart rate stream into the notes the panel pins to its chart.

    The bridge knows the belt's speed and incline, which is what a watch does
    not: a rise that follows a faster belt is expected and only gets a marker
    saying what changed, while the same rise at an unchanged load is a note of
    its own. Everything works from the averaged rate the strap sends, so single
    irregular beats are out of reach — see parse_heart_rate.

    The thresholds are first guesses, to be tuned on recorded walks.
    """

    JUMP_BPM = 20          # a rise or fall this large...
    JUMP_WINDOW = 20.0     # ...within this many seconds
    # A jump must hold this long. Dry electrodes and static from a shirt show
    # up as spikes of a second or two; a real change of rate stays.
    JUMP_HOLD = 5.0
    # Heart rate trails a change of speed or incline by about a minute, so a
    # jump inside this stretch belongs to the load, not to the heart. It also
    # covers the first moments of a strap just put on.
    LOAD_LAG = 90.0
    LOAD_SETTLE = 10.0     # the belt ramps in 0.1 km/h steps; one marker per change
    COOLDOWN = 120.0
    HIGH_HOLD = 30.0
    RECOVERY_AFTER = 60.0
    RECOVERY_MIN_WALK = 300.0
    DRIFT_AFTER = 1200.0
    DRIFT_BPM = 10
    KEEP = 300.0
    GAP = 15.0             # a longer silence, and the windows start over

    def __init__(self, limit: float = 0):
        self.limit = limit
        self.recent: collections.deque = collections.deque()   # (time, bpm)
        self.last: float | None = None
        self.load: tuple | None = None         # (speed, incline) as last read
        self.load_since = 0.0
        self.shown_load: tuple | None = None   # the load the last marker announced
        self.walk_since: float | None = None
        self.recovery: dict | None = None
        self.noted_at: dict = {}
        self.high_armed = True
        self.base_sum = 0.0
        self.base_count = 0
        self.drift_noted = False

    def mean(self, now: float, seconds: float) -> float:
        values = [bpm for at, bpm in self.recent if at > now - seconds]
        return sum(values) / len(values) if values else 0.0

    def cooled(self, kind: str, now: float) -> bool:
        if now - self.noted_at.get(kind, -self.COOLDOWN) < self.COOLDOWN:
            return False
        self.noted_at[kind] = now
        return True

    def feed(self, now: float, bpm: int, speed: float, incline: float) -> list[dict]:
        """One usable reading; `now` is a monotonic clock. Returns the notes it
        gave rise to, each {"kind", "text", "bpm"}."""
        if self.last is not None and now - self.last > self.GAP:
            self.recent.clear()
            self.load = None
            self.walk_since = None
            self.recovery = None
        self.last = now
        self.recent.append((now, bpm))
        while self.recent[0][0] < now - self.KEEP:
            self.recent.popleft()
        notes = (self.track_load(now, speed, incline) + self.check_recovery(now)
                 + self.check_jump(now) + self.check_high(now, bpm)
                 + self.check_drift(now, bpm))
        for note in notes:
            note.setdefault("bpm", bpm)
        return notes

    def track_load(self, now: float, speed: float, incline: float) -> list[dict]:
        load = (round(speed, 1), round(incline))
        if load != self.load:
            was_walking = self.load is not None and self.load[0] > 0
            if load[0] > 0 and not was_walking:
                self.walk_since = now
                self.recovery = None
            elif load[0] <= 0 and was_walking:
                if self.walk_since is not None and now - self.walk_since >= self.RECOVERY_MIN_WALK:
                    self.recovery = {"due": now + self.RECOVERY_AFTER, "from": self.mean(now, 10.0)}
                self.walk_since = None
            self.load = load
            self.load_since = now
            self.base_sum, self.base_count, self.drift_noted = 0.0, 0, False
        if self.shown_load is None:
            self.shown_load = load
        if load == self.shown_load or now - self.load_since < self.LOAD_SETTLE:
            return []
        before, self.shown_load = self.shown_load, load
        # "belt" and "load" apart: the chart shows a running belt by the colour
        # of its line and only marks the changes made while walking.
        if load[0] <= 0:
            return [{"kind": "belt", "text": "Belt stopped"}]
        if before[0] <= 0:
            return [{"kind": "belt", "text": f"Belt started: {load[0]:.1f} km/h, {load[1]}%"}]
        return [{"kind": "load", "text": f"Now {load[0]:.1f} km/h, {load[1]}% "
                                         f"(was {before[0]:.1f} km/h, {before[1]}%)"}]

    def check_recovery(self, now: float) -> list[dict]:
        if self.recovery is None or now < self.recovery["due"]:
            return []
        start, self.recovery = self.recovery["from"], None
        end = self.mean(now, 5.0)
        return [{"kind": "recovery",
                 "text": f"Recovery: {start:.0f} to {end:.0f} bpm in the minute after stopping"}]

    @staticmethod
    def level(values: list[int], share: float) -> int:
        """The reading `share` of the way up the sorted values."""
        ordered = sorted(values)
        return ordered[min(len(ordered) - 1, int(len(ordered) * share))]

    def check_jump(self, now: float) -> list[dict]:
        """The last JUMP_HOLD seconds against the stretch before them. The new
        rate has to hold in every reading. The level it is measured against is
        a percentile, not the lowest or highest reading: a spike a few seconds
        earlier would otherwise read as a fall from its peak."""
        if now - self.load_since < self.LOAD_LAG or self.recent[0][0] > now - self.JUMP_WINDOW:
            return []
        before = [bpm for at, bpm in self.recent
                  if now - self.JUMP_WINDOW <= at < now - self.JUMP_HOLD]
        held = [bpm for at, bpm in self.recent if at >= now - self.JUMP_HOLD]
        if len(before) < 8 or len(held) < 3:
            return []
        where = load_text(*self.load)
        low, high = self.level(before, 0.3), self.level(before, 0.7)
        if min(held) - low >= self.JUMP_BPM and self.cooled("jump", now):
            return [{"kind": "jump", "text": f"Jumped from {low} to {max(held)} bpm "
                                             f"within {self.JUMP_WINDOW:.0f} s {where}"}]
        if high - max(held) >= self.JUMP_BPM and self.cooled("fall", now):
            return [{"kind": "fall", "text": f"Fell from {high} to {min(held)} bpm "
                                             f"within {self.JUMP_WINDOW:.0f} s {where}"}]
        return []

    def check_high(self, now: float, bpm: int) -> list[dict]:
        if self.limit <= 0:
            return []
        if bpm < self.limit - 5:
            self.high_armed = True
        if not self.high_armed or self.recent[0][0] > now - self.HIGH_HOLD:
            return []
        window = [value for at, value in self.recent if at >= now - self.HIGH_HOLD]
        if min(window) < self.limit:
            return []
        self.high_armed = False
        return [{"kind": "high", "text": f"Above {self.limit:.0f} bpm for {self.HIGH_HOLD:.0f} s, "
                                         f"peak {max(window)}"}]

    def check_drift(self, now: float, bpm: int) -> list[dict]:
        """At an unchanged load the rate should level off. Minutes 5–10 of the
        stretch are the baseline — by then the rate has caught up with the load."""
        if self.load is None or self.load[0] <= 0:
            return []
        age = now - self.load_since
        if 300.0 <= age < 600.0:
            self.base_sum += bpm
            self.base_count += 1
        if age < self.DRIFT_AFTER or self.drift_noted or self.base_count == 0:
            return []
        gain = self.mean(now, 300.0) - self.base_sum / self.base_count
        if gain < self.DRIFT_BPM:
            return []
        self.drift_noted = True
        return [{"kind": "drift", "text": f"Drift: +{gain:.0f} bpm over {age / 60:.0f} min "
                                          f"{load_text(*self.load)}"}]


class HeartDay:
    """Today's heart rate the way the chart draws it: one point per
    HEART_POINT_SECONDS — [unix time, bpm, speed, incline, walking] — and the
    notes. Points from before the walking flag have four items.
    Every addition goes to the day's file first, so a bridge restart mid-walk
    (there is one after every backend edit) keeps the chart."""

    def __init__(self):
        self.day = date.today()
        self.points: list[list] = []
        self.notes: list[dict] = []
        self.load()

    @property
    def path(self) -> Path:
        return STATE_DIR / f"heart-{self.day.isoformat()}.jsonl"

    def load(self):
        self.points, self.notes = [], []
        text = read_state(self.path)
        if text is None:
            return
        for line in text.splitlines():
            try:
                raw = json.loads(line)
            except ValueError:
                continue
            if not isinstance(raw, dict):
                continue
            point, note = raw.get("p"), raw.get("n")
            if (isinstance(point, list) and len(point) in (4, 5)
                    and all(isinstance(v, (int, float)) for v in point)):
                self.points.append(point)
            elif isinstance(note, dict) and isinstance(note.get("at"), (int, float)):
                self.notes.append(note)
        del self.points[:-HEART_SERIES_MAX]
        del self.notes[:-HEART_NOTES_MAX]

    def add(self, key: str, value, at: float) -> bool:
        """Returns True when this addition opened a new day, and with it an
        empty chart."""
        rolled = date.fromtimestamp(at) != self.day
        if rolled:
            self.day = date.fromtimestamp(at)
            self.load()
        kept, cap = ((self.points, HEART_SERIES_MAX) if key == "p"
                     else (self.notes, HEART_NOTES_MAX))
        kept.append(value)
        del kept[:-cap]
        try:
            # A point a second is not worth a disk flush each: losing the last
            # few to a power cut costs nothing. Notes are rare and are flushed.
            append_state(self.path, json.dumps({key: value}, separators=(",", ":")) + "\n",
                         sync=key != "p")
        except OSError as exc:
            error(f"cannot save {self.path}: {exc}")
        return rolled

    def messages(self):
        first = self.points[0][0] if self.points else 0
        yield {"t": "hr_series", "reset": True, "day": self.day.isoformat(),
               "notes": [n for n in self.notes if n["at"] >= first]}
        for start in range(0, len(self.points), HEART_SERIES_CHUNK):
            yield {"t": "hr_series", "points": self.points[start:start + HEART_SERIES_CHUNK]}


# ---------------------------------------------------------------------- radio

class Radio:
    """One discovery shared by everyone waiting for a device.

    The treadmill and the strap are looked for at the same time, and a second
    BleakScanner in one process fails in BlueZ with "Operation already in
    progress". A waiter with drives=False only listens in on a discovery
    someone else keeps running, and costs the radio nothing.
    """

    def __init__(self):
        self.waiters: list[dict] = []
        self.scanner: BleakScanner | None = None
        self.lock = asyncio.Lock()

    def on_detect(self, device, adv):
        for waiter in self.waiters:
            if not waiter["found"].done() and waiter["match"](device, adv):
                waiter["found"].set_result((device, adv))

    async def sync(self):
        wanted = any(w["drives"] for w in self.waiters)
        if wanted and self.scanner is None:
            scanner = BleakScanner(detection_callback=self.on_detect)
            await scanner.start()
            self.scanner = scanner
        elif not wanted and self.scanner is not None:
            scanner, self.scanner = self.scanner, None
            await scanner.stop()

    async def wait_for(self, match, patience: float, drives: bool = True):
        """The first (device, advertisement) that match() accepts, or None
        once patience runs out."""
        waiter = {"match": match, "drives": drives,
                  "found": asyncio.get_running_loop().create_future()}
        try:
            async with self.lock:
                self.waiters.append(waiter)
                await self.sync()
            return await asyncio.wait_for(waiter["found"], patience)
        except asyncio.TimeoutError:
            return None
        finally:
            async with self.lock:
                if waiter in self.waiters:
                    self.waiters.remove(waiter)
                await self.sync()


class LinkWatch:
    """Tells when an established link ends.

    BlueZ sometimes aborts a connection attempt ("le-connection-abort-by-local");
    bleak then retries, and calls disconnected_callback once for every aborted
    attempt — before it hands over the connection that finally worked. Taken at
    face value, those calls ended the session right after it began: two
    packets, then a disconnect (2026-10-08, two to four aborts per connect).
    So a disconnect counts only once the link is up."""

    def __init__(self):
        self.up = False
        self.gone = asyncio.Event()

    def on_disconnect(self, _client):
        if self.up:
            self.gone.set()


@contextlib.asynccontextmanager
async def link(client: BleakClient):
    """`async with client`, except that taking the link down cannot fail.

    When the other side has already dropped the link, bleak's disconnect call
    often fails with a bare EOFError from dbus-fast (BlueZ: "No matching
    connection for device") — every time on 2026-10-08. The link is gone
    either way; the error only cut short the cleanup behind the session. It
    goes to the log, not to the panel."""
    await client.connect()
    try:
        yield client
    finally:
        try:
            await client.disconnect()
        except Exception as exc:
            emit({"t": "lifecycle", "event": "unclean-disconnect", "error": repr(exc)})


# ---------------------------------------------------------------- phone server

def read_sessions() -> list[dict]:
    text = read_state(SESSIONS_PATH)
    if text is None:
        return []
    lines = text.splitlines()
    out = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def write_sessions(records: list[dict]):
    write_state(SESSIONS_PATH, "".join(json.dumps(r) + "\n" for r in records))


def mark_sent(ids: list[str]) -> int:
    records = read_sessions()
    wanted = set(ids)
    changed = 0
    for r in records:
        if r.get("id") in wanted and not r.get("sent"):
            r["sent"] = True
            changed += 1
    if changed:
        write_sessions(records)
    return changed


def tailscale_ip() -> str | None:
    """This machine's address in the tailnet, or None while Tailscale has not
    come up yet. Without it there is nowhere to bind the server so that the
    phone sees it and the rest of the network does not."""
    try:
        out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True,
                             text=True, timeout=5).stdout.strip().splitlines()
        if out:
            return out[0].strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return None


class PhoneServer:
    """Three endpoints for the iPhone shortcut, over Tailscale:

        GET  /pending   walks not yet sent
        POST /ack       {"ids": [...]} — mark as sent
        GET  /today     daily totals (preview)

    Listens only on the given address, by default the Tailscale one, so it
    exposes nothing to the local network or the internet.
    """

    def __init__(self, host: str | None, port: int):
        # None: bind the Tailscale address once Tailscale has one. At boot the
        # bridge starts before tailscaled hands out the address; binding
        # localhost then would leave the phone unable to reach it until the
        # next restart.
        self.host = host
        self.port = port
        # What the last /pending handed out — so the shortcut can confirm
        # receipt in one call, without assembling JSON with the ids.
        self.last_served: list[str] = []

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            request_line = await asyncio.wait_for(reader.readline(), 5.0)
            if not request_line:
                return
            parts = request_line.decode("latin-1").split()
            if len(parts) < 2:
                return
            method, path = parts[0], parts[1]

            length = 0
            while True:
                header = await asyncio.wait_for(reader.readline(), 5.0)
                if header in (b"\r\n", b"\n", b""):
                    break
                name, _, value = header.decode("latin-1").partition(":")
                if name.strip().lower() == "content-length":
                    length = int(value.strip() or 0)
            body = await reader.readexactly(length) if length else b""

            peer = writer.get_extra_info("peername")
            status_code, payload = self.route(method, path, body)
            emit({"t": "request", "method": method, "path": path,
                  "from": peer[0] if peer else "?", "status": status_code})
            data = json.dumps(payload).encode()
            writer.write(
                f"HTTP/1.1 {status_code}\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: {len(data)}\r\n"
                "Connection: close\r\n\r\n".encode() + data
            )
            await writer.drain()
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError, ValueError):
            pass
        finally:
            writer.close()

    def route(self, method: str, path: str, body: bytes):
        path, _, query = path.partition("?")
        if method == "GET" and path == "/pending":
            pending = [r for r in read_sessions() if not r.get("sent")]
            self.last_served = [r["id"] for r in pending]
            # iPhone Shortcuts do not turn "2026-08-31T12:47:32" into a date
            # on their own; with a space instead of the T the "Get dates from
            # input" action copes without a format being set.
            for r in pending:
                r["end_text"] = r.get("end", "").replace("T", " ")
                r["start_text"] = r.get("start", "").replace("T", " ")
                r["distance_km"] = round(r.get("distance_m", 0) / 1000, 3)
            return "200 OK", {"sessions": pending}
        if method == "GET" and path == "/ack-all":
            return "200 OK", {"marked": mark_sent(self.last_served)}
        if method == "GET" and path == "/ack":
            ids = [i for i in query.replace("ids=", "").split(",") if i]
            return "200 OK", {"marked": mark_sent(ids)}
        if method == "GET" and path == "/today":
            day = DayTotals()
            return "200 OK", day.snapshot()
        if method == "POST" and path == "/ack":
            try:
                ids = json.loads(body or b"{}").get("ids") or []
            except ValueError:
                return "400 Bad Request", {"error": "bad JSON"}
            return "200 OK", {"marked": mark_sent([str(i) for i in ids])}
        return "404 Not Found", {"error": "no such path"}

    async def serve(self):
        waited = 0
        while self.host is None:
            self.host = tailscale_ip()
            if self.host is not None:
                break
            if waited % 60 == 0:
                error("no Tailscale address yet — waiting before binding the phone server")
            await asyncio.sleep(5.0)
            waited += 5
        # Retry instead of giving up: after a shell restart the previous bridge
        # can hold the port a moment longer, and finishing this task used to
        # kill the whole bridge (run() ends on the first completed task).
        while True:
            try:
                server = await asyncio.start_server(self.handle, self.host, self.port)
                break
            except OSError as exc:
                error(f"port {self.host}:{self.port} is taken ({exc}) — retrying in 5 s")
                await asyncio.sleep(5.0)
        # Its own type, not "status": "status" describes the treadmill link
        # and the panel takes it directly as the connection state.
        emit({"t": "server", "address": f"{self.host}:{self.port}"})
        async with server:
            await server.serve_forever()


# ----------------------------------------------------------------- connection

async def bluetoothctl(*args: str, timeout: float = 10.0) -> bytes | None:
    """The command's output, or None when it could not be run to the end."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "bluetoothctl", *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        return out
    except (OSError, asyncio.TimeoutError):
        return None


class Bridge:
    def __init__(self, address: str | None, steps_uuid: str | None, stride_m: float,
                 heart_address: str = "", heart_limit: float = 0):
        self.address = address
        self.steps_uuid = steps_uuid
        self.stride_m = stride_m
        self.client: BleakClient | None = None
        self.radio = Radio()
        # "" takes the first strap that advertises the Heart Rate service,
        # "off" leaves the strap alone.
        self.heart_address = heart_address
        self.heart = HeartDay()
        self.heart_notes = HeartNotes(heart_limit)
        self.heart_status: dict = {}
        self.heart_sent_at = 0.0
        self.heart_last_packet = 0.0
        self.heart_slot: int | None = None
        self.heart_bucket: list[int] = []
        self.day = DayTotals()
        self.latest: dict = {}
        self.control_replies: asyncio.Queue = asyncio.Queue()
        self.has_control = False
        self.connected = asyncio.Event()
        self.target_speed: float | None = None
        self.target_incline: float | None = None
        self.target_defaults: dict = {}
        self.targets_saved: dict | None = None
        self.targets_task: asyncio.Task | None = None
        # While a start or a target loop drives the belt, readings are on
        # their way somewhere and say nothing about the walker's wishes.
        self.busy_phase = False
        # After a panel command the belt takes a few seconds to follow; until
        # then a reading that differs from the target is lag, not a choice.
        self.hold_until = 0.0
        self.session_started_at: datetime | None = None
        self.session_last_move: float = 0.0
        self.session_last_move_wall: datetime | None = None
        self.session_peak: dict = {}
        self.server: PhoneServer | None = None
        # running | paused | stopped — pause means stepping off the belt, stop
        # a command or the safety key. The treadmill tells one from the other
        # in the machine status; the panel shows it and offers to resume.
        self.belt_state = "stopped"

    @property
    def running_belt(self) -> bool:
        return self.latest.get("speed", 0) > 0

    # ---- heart rate

    def current_load(self) -> tuple[float, float]:
        """Speed and incline as the notes and the chart should see them. The
        last reading outlives a lost treadmill link, and a belt that may long
        have stopped must not pass for a steady walk."""
        if not self.client or not self.client.is_connected:
            return 0.0, 0.0
        return float(self.latest.get("speed", 0)), float(self.latest.get("incline", 0))

    # The treadmill reports a step about once a second while someone walks.
    STEP_SILENCE = 4.0

    def walker_on_belt(self) -> bool:
        """A running belt and a walker are two things: the belt runs on for a
        while after you step off, and it can be started with nobody on it.
        Steps are what tells them apart — the treadmill counts them from the
        person, not from the belt."""
        speed, _ = self.current_load()
        return speed > 0 and time.monotonic() - self.session_last_move < self.STEP_SILENCE

    def publish_heart(self, state: str, bpm: int | None = None, **extra):
        """The strap's state and the live rate, for the panel. A change of
        state goes out at once and into the log; the rate alone at most once a
        second and past the log, which has no rotation."""
        known = {k: v for k, v in self.heart_status.items() if k in ("device", "battery")}
        current = {"state": state, "bpm": bpm, **known, **extra}
        if current == self.heart_status:
            return
        changed_state = state != self.heart_status.get("state")
        now = time.monotonic()
        if not changed_state and now - self.heart_sent_at < 1.0:
            return
        self.heart_status = current
        self.heart_sent_at = now
        emit({"t": "heart", **current}, log=changed_state)

    def on_heart(self, _sender, data: bytearray):
        sample = parse_heart_rate(bytes(data))
        self.heart_last_packet = time.monotonic()
        bpm = sample.get("bpm")
        # No skin contact, or a number no heart produces: the strap is being
        # put on or taken off. Shown as "no reading", kept out of the chart.
        if bpm is None or sample.get("contact") is False or not 30 <= bpm <= 230:
            self.publish_heart("connected", None)
            return
        self.publish_heart("connected", bpm)
        wall = time.time()
        speed, incline = self.current_load()
        for note in self.heart_notes.feed(self.heart_last_packet, bpm, speed, incline):
            note = {"at": int(wall), **note}
            if self.heart.add("n", note, wall):
                self.send_heart_series()
            emit({"t": "hr_note", **note})
        slot = int(wall // HEART_POINT_SECONDS)
        if self.heart_slot is not None and slot != self.heart_slot:
            self.flush_heart_point()
        self.heart_slot = slot
        self.heart_bucket.append(bpm)

    def flush_heart_point(self):
        if not self.heart_bucket or self.heart_slot is None:
            return
        speed, incline = self.current_load()
        at = (self.heart_slot + 1) * HEART_POINT_SECONDS
        point = [at, round(sum(self.heart_bucket) / len(self.heart_bucket)),
                 round(speed, 1), round(incline), int(self.walker_on_belt())]
        self.heart_bucket = []
        if self.heart.add("p", point, at):
            self.send_heart_series()   # past midnight: the panel starts an empty chart
        emit({"t": "hr_point", "point": point}, log=False)

    def send_heart_series(self):
        for message in self.heart.messages():
            emit(message, log=False)

    def is_strap(self, device, adv) -> bool:
        if self.heart_address:
            return device.address.upper() == self.heart_address.upper()
        return HEART_SERVICE in [u.lower() for u in (adv.service_uuids or [])]

    async def release_stale_strap(self):
        """The strap's version of release_stale_link: a link left open by a
        bridge that died stops the strap from advertising. Without a set
        address, whatever BlueZ holds connected that has the Heart Rate
        service is taken for that leftover."""
        if self.heart_address:
            await self.release_stale_link(self.heart_address)
            return
        listing = await bluetoothctl("devices", "Connected")
        for line in (listing or b"").decode("utf-8", "replace").splitlines():
            parts = line.split()
            if len(parts) < 2 or parts[0] != "Device":
                continue
            info = await bluetoothctl("info", parts[1])
            if info and HEART_SERVICE.encode() in info.lower():
                await self.release_stale_link(parts[1])

    async def heart_session(self, device, adv):
        name = adv.local_name or device.name or device.address
        self.publish_heart("connecting", device=name)
        watch = LinkWatch()
        gone = watch.gone
        try:
            async with link(BleakClient(device, timeout=20.0,
                                        disconnected_callback=watch.on_disconnect)) as client:
                watch.up = True
                battery = None
                try:
                    battery = bytes(await client.read_gatt_char(BATTERY_LEVEL))[0]
                except (BleakError, IndexError):
                    pass  # not every strap reports its battery
                self.heart_last_packet = time.monotonic()
                await client.start_notify(HEART_MEASUREMENT, self.on_heart)
                self.publish_heart("connected", device=name, battery=battery)
                # A strap taken off goes quiet before it drops the link; hanging
                # on to a silent link would keep it from being found again.
                while not gone.is_set() and time.monotonic() - self.heart_last_packet < 30.0:
                    try:
                        await asyncio.wait_for(gone.wait(), 5.0)
                    except asyncio.TimeoutError:
                        pass
        finally:
            self.flush_heart_point()
            self.heart_slot = None
            self.publish_heart("idle")

    async def heart_loop(self):
        if self.heart_address.lower() == "off":
            self.publish_heart("off")
            return
        self.publish_heart("idle")
        await self.release_stale_strap()
        misses = 0
        while True:
            # Looking for the strap is worth the radio only while the treadmill
            # is connected — a walk is about to start or under way. The rest of
            # the time this just listens in on the scans for the treadmill.
            treadmill_up = bool(self.client and self.client.is_connected)
            # "scanning" only when this loop runs the scan itself; listening in
            # is not something the panel has to tell anyone about.
            self.publish_heart("scanning" if treadmill_up else "idle")
            try:
                # Listening in is kept short: it cannot tell that the treadmill
                # has connected meanwhile and that nobody scans any more. At a
                # restart that happens within seconds, and a full-length wait
                # left the strap unlooked-for for 20 s (2026-09-21).
                hit = await self.radio.wait_for(self.is_strap, 20.0 if treadmill_up else 3.0,
                                                drives=treadmill_up)
                if hit is not None:
                    misses = 0
                    await self.heart_session(*hit)
                    await asyncio.sleep(3.0)
                    continue
            except (BleakError, asyncio.TimeoutError, OSError) as exc:
                error(f"heart rate strap: {exc}")
                await asyncio.sleep(10.0)
                continue
            except Exception as exc:
                # See connection_loop: an unexpected error ended the strap
                # search for good, silently.
                error(f"heart rate strap: {exc!r}")
                await asyncio.sleep(10.0)
                continue
            if treadmill_up:
                misses += 1
                self.publish_heart("idle")
                await asyncio.sleep(40.0 if misses < 5 else 100.0)
            else:
                misses = 0

    # ---- sessions (walks) to be sent to the phone

    def track_session(self, applied: dict):
        """Opens a walk on the first movement and closes it after
        SESSION_IDLE_GAP seconds without any.

        Sums the same increments that feed the daily counter — not the
        treadmill's readings. The readings survive a bridge restart, so a walk
        counted from them was recorded whole all over again after every
        restart, and the queue for the phone swelled with duplicates.
        """
        now = time.monotonic()

        if applied.get("steps", 0) > 0:
            if self.session_started_at is None:
                self.session_started_at = datetime.now()
                self.session_peak = {}
            self.session_last_move = now
            self.session_last_move_wall = datetime.now()
            for field, delta in applied.items():
                self.session_peak[field] = self.session_peak.get(field, 0) + delta
        elif self.session_started_at is not None and now - self.session_last_move > SESSION_IDLE_GAP:
            self.close_session()

    def close_session(self):
        if self.session_started_at is None:
            return
        peak = self.session_peak
        started = self.session_started_at
        self.session_started_at = None
        self.session_peak = {}
        try:
            OPEN_SESSION_PATH.unlink()
        except OSError:
            pass
        if peak.get("steps", 0) <= 0:
            return  # the belt spun with nobody on it — nothing worth recording
        record = {
            "id": started.strftime("%Y%m%dT%H%M%S"),
            "start": started.isoformat(timespec="seconds"),
            "end": datetime.now().isoformat(timespec="seconds"),
            "steps": int(peak.get("steps", 0)),
            "distance_m": int(peak.get("distance_m", 0)),
            "kcal": int(peak.get("kcal", 0)),
            "elapsed_s": int(peak.get("elapsed_s", 0)),
            "sent": False,
        }
        self.append_session(record)

    def append_session(self, record: dict):
        try:
            append_state(SESSIONS_PATH, json.dumps(record) + "\n")
        except OSError as exc:
            error(f"cannot save the session: {exc}")
        emit({"t": "session", **record})

    def persist_open_session(self):
        """Mirrors the walk in progress to disk. The shell kills the bridge on
        every plugin reload, and a walk held only in memory died with it: the
        steps stayed on the bar (the daily counter is saved every 30 s) but
        never reached the phone."""
        if self.session_started_at is None:
            return
        payload = {
            "start": self.session_started_at.isoformat(timespec="seconds"),
            "last_move": (self.session_last_move_wall or datetime.now()).isoformat(timespec="seconds"),
            "peak": self.session_peak,
        }
        try:
            write_state(OPEN_SESSION_PATH, json.dumps(payload))
        except OSError as exc:
            error(f"cannot save the open session: {exc}")

    def recover_open_session(self):
        """A leftover open-session file means the previous bridge died
        mid-walk. Close that walk as of its last recorded movement and queue
        it for the phone."""
        text = read_state(OPEN_SESSION_PATH)
        if text is None:
            return
        try:
            raw = json.loads(text)
        except ValueError as exc:
            error(f"cannot read {OPEN_SESSION_PATH}: {exc}")
            return
        try:
            OPEN_SESSION_PATH.unlink()
        except OSError:
            pass
        peak = raw.get("peak") or {}
        start = raw.get("start") or ""
        if peak.get("steps", 0) <= 0 or not start:
            return
        self.append_session({
            "id": start.replace("-", "").replace(":", ""),
            "start": start,
            "end": raw.get("last_move") or start,
            "steps": int(peak.get("steps", 0)),
            "distance_m": int(peak.get("distance_m", 0)),
            "kcal": int(peak.get("kcal", 0)),
            "elapsed_s": int(peak.get("elapsed_s", 0)),
            "sent": False,
        })

    def publish_targets(self):
        """Targets kept apart from readings: when the belt stands still the
        treadmill reports zeros, and the panel should show what will be set
        after start."""
        emit({"t": "targets", "target_speed": self.target_speed,
              "target_incline": self.target_incline})
        self.save_targets()

    def save_targets(self):
        record = {"speed": self.target_speed, "incline": self.target_incline,
                  "defaults": self.target_defaults}
        if record == self.targets_saved:
            return
        try:
            write_state(TARGETS_PATH, json.dumps(record))
            self.targets_saved = record
        except OSError as exc:
            error(f"cannot save the targets: {exc}")

    def adopt_targets(self, sample: dict):
        """The treadmill's own console changes speed and incline behind our
        back. While the belt runs and nothing of ours is driving it, a reading
        that differs from the target is the walker's choice: it becomes the
        target, so the panel shows what the belt does and a resume does not
        put the old value back."""
        if self.busy_phase or time.monotonic() < self.hold_until:
            return
        if self.targets_task and not self.targets_task.done():
            return
        if sample.get("speed", 0) <= 0:
            return
        changed = False
        speed = sample.get("speed")
        if (speed is not None and self.target_speed is not None
                and abs(speed - self.target_speed) >= 0.05):
            self.target_speed = round(speed, 1)
            changed = True
        incline = sample.get("incline")
        if (incline is not None and self.target_incline is not None
                and abs(incline - self.target_incline) >= 0.05):
            self.target_incline = round(incline, 1)
            changed = True
        if changed:
            self.publish_targets()

    def hold_targets(self, seconds: float = 8.0):
        self.hold_until = time.monotonic() + seconds

    def phase(self, name: str, text: str):
        self.busy_phase = name in ("control", "starting", "unconfirmed", "spinup", "setting")
        """Start progress for the panel. The treadmill starts with a delay,
        confirms commands seconds later and accepts targets only once up to
        speed — without this the Start button looks like it did nothing."""
        emit({"t": "phase", "phase": name, "text": text})

    # ---- receiving

    def steps_from(self, sample: dict) -> float | None:
        if "steps" in sample:
            return sample["steps"]
        if "distance_m" in sample and self.stride_m > 0:
            return sample["distance_m"] / self.stride_m
        return None

    def on_treadmill_data(self, _sender, data: bytearray):
        sample = parse_treadmill_data(bytes(data))
        if not sample:
            return
        steps = self.steps_from(sample)
        if steps is not None:
            sample["steps"] = steps
        applied = self.day.update(sample) or {}
        self.latest = sample
        self.adopt_targets(sample)
        self.track_session(applied)
        # Commit before publishing: a displayed gain survives a hard kill.
        saved = self.day.save()
        if applied:
            self.persist_open_session()
        if not saved:
            return  # Never advertise a total which failed to reach disk.
        payload = {"t": "data"}
        payload.update({k: round(v, 2) if isinstance(v, float) else v
                        for k, v in sample.items() if v is not None})
        payload.update(self.day.snapshot())
        emit(payload)

    def on_control_reply(self, _sender, data: bytearray):
        raw = bytes(data)
        if len(raw) >= 3 and raw[0] == RESPONSE_CODE:
            self.control_replies.put_nowait((raw[1], raw[2]))
        else:
            emit({"t": "control", "raw": raw.hex(" ")})

    def on_machine_status(self, _sender, data: bytearray):
        """Machine status (FTMS 4.17): 0x02 is a stop by the user, where
        parameter 0x01 means stop and 0x02 pause — the treadmill pauses on its
        own when nobody is standing on it. 0x04 is a start or a resume."""
        raw = bytes(data)
        emit({"t": "machine", "raw": raw.hex(" ")})
        if raw[:1] == b"\x02":
            paused = len(raw) > 1 and raw[1] == 0x02
            self.belt_state = "paused" if paused else "stopped"
            emit({"t": "belt", "state": self.belt_state})
            self.phase("paused" if paused else "stopped",
                       "paused — you stepped off the belt" if paused else "stopped")
            self.day.save()
        elif raw[:1] == b"\x03":  # safety key
            self.belt_state = "stopped"
            emit({"t": "belt", "state": self.belt_state})
            self.day.save()
        elif raw[:1] == b"\x04":
            resumed = self.belt_state == "paused"
            self.belt_state = "running"
            emit({"t": "belt", "state": self.belt_state})
            # Stepping back on the belt resumes it, but at the treadmill's own
            # speed and incline. The vendor app re-sent the targets silently;
            # without this the panel user had to set them again by hand.
            if resumed and (self.target_speed is not None or self.target_incline is not None):
                self.kick_targets(ensure_control=True)

    # ---- sending

    # The treadmill confirms a start as late as 7 s in (seen in the log), so
    # a shorter timeout turned a successful command into an error.
    async def send_command(self, opcode: int, payload: bytes = b"", timeout: float = 10.0) -> bool:
        if not self.client or not self.client.is_connected:
            error("the treadmill is not connected")
            return False
        while not self.control_replies.empty():
            self.control_replies.get_nowait()
        try:
            await self.client.write_gatt_char(CONTROL_POINT, bytes([opcode]) + payload, response=True)
        except BleakError as exc:
            error(f"writing command 0x{opcode:02x} failed: {exc}")
            return False
        try:
            replied_op, result = await asyncio.wait_for(self.control_replies.get(), timeout)
        except asyncio.TimeoutError:
            error(f"no reply to command 0x{opcode:02x}")
            return False
        if replied_op != opcode:
            error(f"reply to a different command: 0x{replied_op:02x}")
            return False
        if result != RESULT_SUCCESS:
            error(f"command 0x{opcode:02x} rejected: {RESULT_NAMES.get(result, hex(result))}")
            return False
        return True

    def targets_reached(self) -> bool:
        speed_ok = (self.target_speed is None
                    or abs(self.latest.get("speed", 0) - self.target_speed) < 0.05)
        incline_ok = (self.target_incline is None
                      or abs(self.latest.get("incline", 0) - self.target_incline) < 0.05)
        return speed_ok and incline_ok

    async def apply_targets(self, resume: bool = False):
        """Keeps sending speed and incline after start until the treadmill
        shows them.

        The rhythm was tuned on the hardware: the first attempt only after
        9 s, because a command sent earlier vanishes without a reply — the
        treadmill spins up to 1 km/h and only then listens. Then every 3 s,
        with a short wait for the confirmation: no reply means "ignored", so
        there is no point waiting the full 10 s as at start.

        A resume from pause skips the spin-up wait and pushes every 1.5 s
        instead: the belt is back under way within a second or two, and the
        targets should land before the walker settles into 1 km/h.
        """
        if resume:
            self.phase("spinup", "resuming, sending speed and incline")
        else:
            self.phase("spinup", "belt is starting, waiting for it to come up to speed")
            await asyncio.sleep(6.0)
        pace = 1.5 if resume else 3.0
        reply_wait = 2.0 if resume else 4.0
        for attempt in range(14 if resume else 10):
            await asyncio.sleep(pace)
            if not self.client or not self.client.is_connected:
                self.phase("error", "the treadmill disconnected")
                return
            # The belt stopped (the treadmill stops itself when nobody is on
            # it) — sending targets to a stopped machine yields only errors.
            if self.latest.get("speed", 0) <= 0:
                if resume and attempt < 8:
                    continue  # a resumed belt reports zero for a moment
                self.phase("failed", "the belt did not start")
                return
            if self.targets_reached():
                self.phase("running", self.running_text())
                return
            # Speed all the way to target first, incline only after: incline
            # reaches its mark in one command while speed needs a couple, so
            # sending both at once left the incline set visibly before the
            # speed had settled. One command per pass keeps the order clear.
            speed_reached = (self.target_speed is None
                             or abs(self.latest.get("speed", 0) - self.target_speed) < 0.05)
            if not speed_reached:
                self.phase("setting", f"setting {self.target_speed:.1f} km/h")
                await self.send_command(OP_SET_SPEED,
                                        round(self.target_speed * 100).to_bytes(2, "little"),
                                        timeout=reply_wait)
            elif self.target_incline is not None and abs(self.latest.get("incline", 0) - self.target_incline) >= 0.05:
                self.phase("setting", f"setting incline {round(self.target_incline)}")
                await self.send_command(OP_SET_INCLINATION,
                                        round(self.target_incline * 10).to_bytes(2, "little", signed=True),
                                        timeout=reply_wait)
        self.phase("running" if self.targets_reached() else "partial", self.running_text())

    def running_text(self) -> str:
        speed = f"{self.latest.get('speed', 0):.1f}"
        return f"running {speed} km/h, incline {round(self.latest.get('incline', 0))}"

    def kick_targets(self, ensure_control: bool = False, resume: bool = False):
        """At most one target loop at a time: a Start from the panel and the
        0x04 status it triggers would otherwise race each other with duplicate
        commands."""
        if self.targets_task and not self.targets_task.done():
            return
        coro = self.resume_targets() if ensure_control else self.apply_targets(resume=resume)
        self.targets_task = asyncio.create_task(coro)

    async def resume_targets(self):
        # Control does not survive a reconnect, and a resume can be the first
        # command-worthy moment of a connection.
        if not await self.request_control():
            return
        await self.apply_targets(resume=True)

    async def request_control(self) -> bool:
        if self.has_control:
            return True
        self.has_control = await self.send_command(OP_REQUEST_CONTROL)
        return self.has_control

    async def handle_line(self, line: str):
        parts = line.strip().split()
        if not parts:
            return
        cmd, args = parts[0].lower(), parts[1:]

        if cmd == "ping":
            emit({"t": "pong", "connected": bool(self.client and self.client.is_connected)})
            return
        if cmd == "heart-series":
            self.send_heart_series()
            return
        if cmd == "reset-day":
            self.day.totals = {f: 0.0 for f in DayTotals.FIELDS}
            self.day.dirty = True
            self.day.save()
            emit({"t": "data", **self.day.snapshot()})
            return

        if cmd == "start":
            self.phase("control", "taking control of the treadmill")
        if not await self.request_control():
            if cmd == "start":
                self.phase("error", "the treadmill would not hand over control")
            return

        if cmd == "start":
            # A start on a paused belt is a resume: the belt moves again within
            # a second or two, so the targets go out on the fast rhythm.
            resuming = self.belt_state == "paused"
            # Starting/resuming is not a counter reset. Keep the last credited
            # reading; update() detects the treadmill's actual reset to zero.
            if args:
                self.target_speed = float(args[0])
            if len(args) > 1:
                self.target_incline = float(args[1])
            self.publish_targets()
            self.phase("starting", "sent start, waiting for the reply")
            # No confirmation does not mean the belt did not start — sometimes
            # the reply gets lost while the treadmill starts anyway. Targets go
            # out either way; apply_targets bails out when the belt stands.
            if not await self.send_command(OP_START):
                self.phase("unconfirmed", "no reply, checking whether the belt moved")
            self.kick_targets(resume=resuming)
        elif cmd == "stop":
            if await self.send_command(OP_STOP, bytes([0x01])):
                self.day.save()
                self.phase("stopped", "stopped")
        elif cmd == "pause":
            if await self.send_command(OP_STOP, bytes([0x02])):
                self.day.save()
                self.phase("stopped", "paused")
        elif cmd == "speed" and args:
            kmh = max(0.0, float(args[0]))
            self.target_speed = kmh
            self.hold_targets()
            self.publish_targets()
            # A stopped treadmill accepts neither speed nor incline — remember
            # the target and send it after start.
            if self.running_belt:
                await self.send_command(OP_SET_SPEED, round(kmh * 100).to_bytes(2, "little"))
        elif cmd == "incline" and args:
            percent = float(args[0])
            self.target_incline = percent
            self.hold_targets()
            self.publish_targets()
            if self.running_belt:
                await self.send_command(OP_SET_INCLINATION,
                                        round(percent * 10).to_bytes(2, "little", signed=True))
        else:
            error(f"unknown command: {line.strip()}")

    # ---- loop

    async def release_stale_link(self, address: str | None):
        """A bridge that died without saying goodbye (SIGKILL, a crash) leaves
        its link open in BlueZ. The treadmill does not advertise while it has
        a connection, so a scan would never find it — the previous bridge
        scanned for 13 minutes on 2026-09-07. Drop the link first; the
        treadmill starts advertising within seconds."""
        if not address:
            return
        out = await bluetoothctl("info", address)
        if out is None or b"Connected: yes" not in out:
            return
        # The panel reads "status" as the treadmill link; the strap's leftovers
        # are none of its business.
        if address == self.address:
            status("releasing", address=address)
        if await bluetoothctl("disconnect", address, timeout=15.0) is None:
            error(f"could not release the stale link to {address}")

    async def find_device(self, patience: float = 60.0):
        """A continuous scan instead of a one-shot query. The treadmill
        advertises only for a moment after power-on, so we listen non-stop and
        grab it the moment it speaks up. BlueZ forgets it after a disconnect,
        so connecting by the address alone ends in "device not found"."""
        await self.release_stale_link(self.address)
        status("scanning")
        want = (self.address or "").upper()

        def is_treadmill(device, adv):
            if want:
                return device.address.upper() == want
            return FTMS_SERVICE in [u.lower() for u in (adv.service_uuids or [])]

        hit = await self.radio.wait_for(is_treadmill, patience)
        if hit is None:
            return None
        device, adv = hit
        status("found", device=adv.local_name or device.name or device.address,
               address=device.address)
        return device

    async def session(self) -> bool:
        """Returns False when the treadmill is not on the air — then a longer
        wait is worthwhile."""
        device = await self.find_device()
        if device is None:
            status("not_found")
            return False
        address = device.address
        status("connecting", address=address)
        watch = LinkWatch()

        try:
            async with link(BleakClient(device, timeout=30.0,
                                        disconnected_callback=watch.on_disconnect)) as client:
                watch.up = True
                self.client = client
                self.has_control = False
                # No new_session() here: the reference survives a reconnect on
                # purpose. If the belt ran on while we were away, the counters are
                # higher than the reference by exactly what was walked meanwhile,
                # and the next reading credits it. A counter below the reference
                # means the treadmill was restarted, and update() counts from zero.
                status("connected", address=address)

                await client.start_notify(TREADMILL_DATA, self.on_treadmill_data)
                await client.start_notify(CONTROL_POINT, self.on_control_reply)
                try:
                    await client.start_notify(MACHINE_STATUS, self.on_machine_status)
                except BleakError:
                    pass  # not every treadmill has machine status
                if self.steps_uuid:
                    try:
                        await client.start_notify(self.steps_uuid, self.on_steps_char)
                    except BleakError as exc:
                        error(f"cannot subscribe to steps ({self.steps_uuid}): {exc}")

                await watch.gone.wait()

        finally:
            self.client = None
            self.has_control = False
            self.connected.clear()
            self.day.save()
            self.close_session()
            status("disconnected")
        return True

    def on_steps_char(self, _sender, data: bytearray):
        """Steps from a vendor characteristic — probe.py works out the layout."""
        emit({"t": "steps_raw", "raw": bytes(data).hex(" ")})

    async def connection_loop(self):
        attempt = 0
        while True:
            found = False
            try:
                found = await self.session()
                if found:
                    attempt = 0
            except (BleakError, asyncio.TimeoutError, OSError) as exc:
                error(f"connection failed: {exc}")
            except Exception as exc:
                # Not every failure comes as a BleakError: dbus-fast raises a
                # bare EOFError when BlueZ has already dropped the link. Letting
                # it through ended the bridge without a word in the log.
                error(f"connection failed: {exc!r}")
            if not found:
                attempt += 1
            # A powered-off treadmill is the normal state, not a failure: after
            # a few empty tries we drop to one scan a minute, so Bluetooth is
            # not hogged from other devices.
            if attempt == 0:
                delay = 5.0
            elif attempt < 5:
                delay = 10.0
            else:
                delay = 60.0
            await asyncio.sleep(delay)

    async def stdin_loop(self):
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
        while True:
            line = await reader.readline()
            if not line:
                return  # stdin closed — the shell is gone
            try:
                await self.handle_line(line.decode("utf-8", "replace"))
            except Exception as exc:
                error(f"command failed: {exc}")

    async def save_loop(self):
        while True:
            await asyncio.sleep(30)
            self.day.roll_over_if_needed()
            self.day.save()
            self.persist_open_session()

    async def run(self):
        self.recover_open_session()
        emit({"t": "data", **self.day.snapshot()})
        emit({"t": "history", "days": read_history()})
        self.publish_targets()
        stdin_task = asyncio.create_task(self.stdin_loop())
        conn_task = asyncio.create_task(self.connection_loop())
        save_task = asyncio.create_task(self.save_loop())
        heart_task = asyncio.create_task(self.heart_loop())
        server_task = asyncio.create_task(self.server.serve()) if self.server else None
        # The shell stops the bridge with a signal on every plugin reload.
        # Python's default for SIGTERM ends the process on the spot, link and
        # all: BlueZ kept the treadmill connected, the treadmill stopped
        # advertising, and the next bridge could not find it. So the signal
        # only sets an event, and the shutdown below says goodbye properly.
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            loop.add_signal_handler(sig, stop.set)
        stop_task = asyncio.create_task(stop.wait())
        # Wait only for the tasks whose end truly means the work is done:
        # a closed stdin (the shell is gone), a broken connection loop, or
        # the stop signal. The phone server lives alongside; its troubles
        # must not kill the bridge.
        done, _ = await asyncio.wait([stdin_task, conn_task, stop_task],
                                     return_when=asyncio.FIRST_COMPLETED)
        # Save first, before any output or potentially slow BLE cleanup.
        self.day.save()
        self.persist_open_session()
        if stop_task in done:
            status("stopping")
        tasks = [t for t in (stdin_task, conn_task, stop_task, save_task, heart_task, server_task) if t]
        for task in tasks:
            task.cancel()
        # Cancelling the connection task unwinds `async with link(...)`,
        # which asks BlueZ to drop the link. That takes a moment — leaving
        # before it is done keeps the link open just like a kill would.
        await asyncio.wait(tasks, timeout=5.0)
        self.close_session()
        self.day.save()


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--address", help="treadmill address; scans for FTMS without it")
    parser.add_argument("--steps-uuid", help="characteristic carrying steps, if different from FTMS")
    parser.add_argument("--stride", type=float, default=0.0,
                        help="stride length in meters — derives steps from distance when the treadmill reports none")
    parser.add_argument("--serve", metavar="HOST:PORT",
                        help="expose sessions for the phone, e.g. :8787 (bare port = the Tailscale address)")
    parser.add_argument("--heart-address", default="",
                        help="heart rate strap address; empty takes any strap, off disables")
    parser.add_argument("--heart-limit", type=float, default=0,
                        help="note a heart rate that stays above this many bpm; 0 disables")
    parser.add_argument("--speed", type=float, default=None, help="speed to set after start")
    parser.add_argument("--incline", type=float, default=None, help="incline to set after start")
    args = parser.parse_args()
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(STATE_DIR / "bridge.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(lock_fd)
        error("another Spacewalk bridge already owns the connection")
        return
    # Only under the lock: no other bridge can be in the middle of a write.
    removed = remove_stale_temp_files()
    if removed:
        emit({"t": "lifecycle", "event": "cleanup", "stale_temp_files": removed})
    # Keep this descriptor alive until process exit, before loading counters.
    bridge = Bridge(args.address, args.steps_uuid, args.stride,
                    heart_address=args.heart_address, heart_limit=args.heart_limit)
    bridge.target_defaults = {"speed": args.speed, "incline": args.incline}
    targets = load_targets(bridge.target_defaults)
    bridge.target_speed = targets["speed"]
    bridge.target_incline = targets["incline"]
    if args.serve:
        host, _, port = args.serve.rpartition(":")
        bridge.server = PhoneServer(host or None, int(port))
    emit({"t": "lifecycle", "event": "started", "pid": os.getpid()})
    await bridge.run()
    emit({"t": "lifecycle", "event": "stopped", "pid": os.getpid()})


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
