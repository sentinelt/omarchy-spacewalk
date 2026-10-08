#!/usr/bin/env python3
"""Keep the BLE bridge alive independently of Quickshell plugin reloads.

The user service owns the bridge. Short-lived panel clients only relay JSON
events and commands through a private Unix socket; disconnecting a client
never stops the bridge or the treadmill.
"""
import argparse
import asyncio
import contextlib
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import signal
import sys
import stat
import tempfile

UNIT = "omarchy-spacewalk.service"
RUNTIME = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "omarchy-spacewalk"
SOCKET = RUNTIME / "bridge.sock"
STATE = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "omarchy-spacewalk"
BRIDGE = Path(__file__).with_name("spacewalk-bridge.py")
MANIFEST = Path(__file__).with_name("manifest.json")
DBUS = Path(__file__).with_name("spacewalk_dbus.py")


def load_dbus():
    """The D-Bus face, from next to this file — wherever Python was started
    from. Loaded only when asked for: the Omarchy panel does without it."""
    spec = importlib.util.spec_from_file_location("spacewalk_dbus", DBUS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def ensure_service():
    """A transient user unit needs neither installation nor an uninstall hook."""
    RUNTIME.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(RUNTIME / "launch.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                await asyncio.sleep(0.05)
        check = await asyncio.create_subprocess_exec(
            "systemctl", "--user", "is-active", "--quiet", UNIT)
        if await check.wait() == 0:
            return
        proc = await asyncio.create_subprocess_exec(
            "systemd-run", "--user", "--quiet", "--collect", "--unit=" + UNIT,
            "--property=Restart=on-failure", "--property=RestartSec=3",
            "--property=TimeoutStopSec=25", "--property=KillMode=mixed",
            "--property=UMask=0077", "/usr/bin/python3", str(Path(__file__).absolute()), "--host")
        if await proc.wait():
            raise RuntimeError(f"could not start {UNIT}")
    finally:
        os.close(fd)


async def watch_installation(stop, manifest=None, interval=2, grace=6):
    # A panel disappearing during a reload is irrelevant. Only removal of
    # the installed plugin ends the service; tolerate brief atomic updates.
    manifest = MANIFEST if manifest is None else manifest
    missing_since = None
    loop = asyncio.get_running_loop()
    while not stop.is_set():
        if manifest.is_file():
            missing_since = None
        elif missing_since is None:
            missing_since = loop.time()
        elif loop.time() - missing_since >= grace:
            stop.set()
            return
        await asyncio.sleep(interval)


def save_args(args):
    STATE.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=STATE, prefix="service-args.")
    try:
        with os.fdopen(fd, "w") as out:
            json.dump(args, out)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, STATE / "service-args.json")
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp)


def read_args():
    try:
        fd = os.open(STATE / "service-args.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > 4096:
            raise ValueError("invalid saved bridge arguments")
        return validate_args(json.load(stream))


def validate_args(args):
    parser = argparse.ArgumentParser(exit_on_error=False)
    parser.add_argument("--address")
    parser.add_argument("--stride", type=float)
    parser.add_argument("--speed", type=float)
    parser.add_argument("--incline", type=float)
    parser.add_argument("--serve")
    parser.add_argument("--steps-uuid")
    parser.add_argument("--heart-address")
    parser.add_argument("--heart-limit", type=float)
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise ValueError("invalid bridge arguments")
    try:
        parser.parse_args(args)
    except (SystemExit, argparse.ArgumentError) as exc:
        raise ValueError("invalid bridge arguments") from exc
    return args


class Host:
    def __init__(self):
        self.process = None
        self.args = None
        self.clients = set()
        # Called with every event, in order: the D-Bus face keeps its state
        # this way. A listener must not block.
        self.listeners = []
        self.connections = set()
        self.cache = {}
        self.lock = asyncio.Lock()
        self.stopping = False
        self.supervisor = None
        self.spawned = asyncio.Event()

    def publish(self, event):
        kind = event.get("t")
        if kind == "data":
            if event.get("day") != self.cache.get("data", {}).get("day"):
                self.cache.pop("data", None)
            self.cache["data"] = {**self.cache.get("data", {}), **event}
        elif kind in ("status", "targets", "history", "belt", "phase", "server", "heart"):
            self.cache[kind] = event
        for listener in tuple(self.listeners):
            listener(event)
        encoded = (json.dumps(event) + "\n").encode()
        for queue in tuple(self.clients):
            # A stuck UI must never stall the treadmill's data or disk writes.
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(encoded)

    async def stop_bridge(self):
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 15)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()

    async def configure(self, args):
        args = validate_args(args)
        async with self.lock:
            if self.stopping:
                return
            if self.args == args:
                return
            # Settings changes are serialized; ordinary UI reloads do nothing.
            self.args = args
            save_args(args)
            await self.respawn()

    async def respawn(self):
        """A fresh bridge with the current arguments. The caller holds the lock."""
        if self.supervisor:
            self.supervisor.cancel()
            await self.stop_bridge()
            await asyncio.gather(self.supervisor, return_exceptions=True)
        self.cache.clear()
        self.spawned.clear()
        self.supervisor = asyncio.create_task(self.run_bridge())
        # Return with the process started, so a command sent right after a
        # settings change is not refused. A supervisor that dies first (no
        # python?) must not leave us waiting forever.
        spawned = asyncio.create_task(self.spawned.wait())
        await asyncio.wait([spawned, self.supervisor], return_when=asyncio.FIRST_COMPLETED)
        spawned.cancel()

    async def restart(self):
        """Restart the bridge, and with it the Bluetooth link, keeping the
        settings. Unlike a crash this is no error, so none is reported.
        Returns False before anything has configured a bridge."""
        async with self.lock:
            if self.stopping or self.args is None:
                return False
            await self.respawn()
            return True

    async def send(self, line):
        """Hands one command line to the bridge. False when none is running."""
        async with self.lock:
            if not (self.process and self.process.returncode is None):
                return False
            try:
                self.process.stdin.write(line if line.endswith(b"\n") else line + b"\n")
                await self.process.stdin.drain()
            except ConnectionError:
                return False             # the bridge exited a moment ago
            return True

    async def run_bridge(self):
        while not self.stopping:
            self.process = await asyncio.create_subprocess_exec(
                sys.executable, str(BRIDGE), *self.args,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
            self.spawned.set()
            while line := await self.process.stdout.readline():
                try:
                    self.publish(json.loads(line))
                except (ValueError, TypeError):
                    continue
            code = await self.process.wait()
            self.publish({"t": "status", "state": "disconnected"})
            self.publish({"t": "error", "msg": f"bridge exited ({code}); reconnecting"})
            await asyncio.sleep(5)

    async def serve_client(self, reader, writer):
        self.connections.add(writer)
        queue = asyncio.Queue(maxsize=256)
        sender = None
        async def send_events():
            while True:
                writer.write(await queue.get())
                await writer.drain()
        try:
            hello = json.loads(await asyncio.wait_for(reader.readline(), 10))
            await self.configure(hello["args"])
            for event in self.cache.values():
                queue.put_nowait((json.dumps(event) + "\n").encode())
            self.clients.add(queue)
            sender = asyncio.create_task(send_events())
            while line := await reader.readline():
                await self.send(line)
        except (ValueError, KeyError, ConnectionError, asyncio.TimeoutError):
            pass
        finally:
            self.connections.discard(writer)
            self.clients.discard(queue)
            if sender:
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    async def run(self, dbus=False):
        """dbus: also serve the session bus (spacewalk_dbus.py), and run a
        bridge with default settings until a client configures one."""
        RUNTIME.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(RUNTIME / "host.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            SOCKET.unlink(missing_ok=True)
            server = await asyncio.start_unix_server(self.serve_client, path=str(SOCKET))
            os.chmod(SOCKET, 0o600)
            stop = asyncio.Event()
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, stop.set)
            watcher = asyncio.create_task(watch_installation(stop))
            bus = None
            try:
                if dbus:
                    bus = await load_dbus().serve(self)
                saved = read_args()
                if saved is None and dbus:
                    # Nobody else starts the bridge; count steps from the start.
                    saved = []
                if saved is not None:
                    await self.configure(saved)
                async with server:
                    await stop.wait()
                    self.stopping = True
                    # Server.__aexit__ waits for all accepted connections too.
                    # Close UI sockets first; clients otherwise wait for us forever.
                    server.close()
                    for writer in tuple(self.connections):
                        writer.close()
            finally:
                self.stopping = True
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
                # Keep draining stdout while the child checkpoints and exits.
                await self.stop_bridge()
                if self.supervisor:
                    self.supervisor.cancel()
                    await asyncio.gather(self.supervisor, return_exceptions=True)
                SOCKET.unlink(missing_ok=True)
                if bus:
                    bus.disconnect()


async def client(args):
    validate_args(args)
    await ensure_service()
    for attempt in range(50):
        try:
            reader, writer = await asyncio.open_unix_connection(str(SOCKET))
            break
        except (FileNotFoundError, ConnectionRefusedError):
            if attempt == 49:
                raise
            await asyncio.sleep(0.1)
    writer.write((json.dumps({"args": args}) + "\n").encode())
    await writer.drain()
    stdin = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(stdin)
    transport, _ = await asyncio.get_running_loop().connect_read_pipe(lambda: protocol, sys.stdin)
    async def commands():
        while line := await stdin.readline():
            writer.write(line)
            await writer.drain()
    async def events():
        while line := await reader.readline():
            sys.stdout.write(line.decode())
            sys.stdout.flush()
    tasks = [asyncio.create_task(commands()), asyncio.create_task(events())]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        transport.close()
        writer.close()
        with contextlib.suppress(ConnectionError):
            await writer.wait_closed()


if __name__ == "__main__":
    os.umask(0o077)
    try:
        if sys.argv[1:2] == ["--host"]:
            if sys.argv[2:] not in ([], ["--dbus"]):
                sys.exit("usage: spacewalk-service.py --host [--dbus]")
            asyncio.run(Host().run(dbus=sys.argv[2:] == ["--dbus"]))
        else:
            asyncio.run(client(sys.argv[1:]))
    except (BrokenPipeError, KeyboardInterrupt):
        pass
