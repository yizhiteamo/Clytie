"""Independent sender; application state is copied on its owning event loop."""

import asyncio
import threading


class Heartbeat:
    def __init__(self, clock, emit):
        self.clock, self.emit = clock, emit
        self.phase = "starting"
        self.fields = {}
        self.refreshed = clock.monotonic()
        self.interval, self.hang = 5, 30
        self.lock = threading.RLock()
        self.closed = threading.Event()
        self.thread = None
        self.loop = self.task = None

    def update(self, fields=None, *, phase=None, interval=None, hang=None):
        with self.lock:
            if fields is not None:
                self.fields = dict(fields)
            if phase is not None:
                self.phase = phase
            if interval is not None:
                self.interval = interval
            if hang is not None:
                self.hang = hang
            self.refreshed = self.clock.monotonic()
        if phase is not None:
            self.pulse()

    def pulse(self):
        with self.lock:
            if self.phase != "migrating" and self.clock.monotonic() - self.refreshed >= self.hang:
                return False
            fields = dict(self.fields, phase=self.phase)
        try:
            self.emit("heartbeat", **fields)
        except OSError:
            return False
        return True

    def start(self):
        async def run():
            self.loop = asyncio.get_running_loop()
            self.task = asyncio.current_task()
            try:
                while not self.closed.is_set():
                    self.pulse()
                    await self.clock.sleep(self.interval)
            except asyncio.CancelledError:
                pass

        self.thread = threading.Thread(target=lambda: asyncio.run(run()), daemon=True)
        self.thread.start()

    def close(self):
        self.closed.set()
        if self.loop and self.task:
            try:
                self.loop.call_soon_threadsafe(self.task.cancel)
            except RuntimeError:
                pass
        if self.thread:
            self.thread.join(1)
