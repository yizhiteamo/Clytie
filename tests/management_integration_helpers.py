"""Fictional homes and synchronous cross-process barriers, never async import doubles."""

import errno
import shutil
import subprocess

import yaml

from clytie.clock import SystemClock
from clytie.supervisor.processes import Child, ProcessLauncher, creation, python_process

from .host_helpers import ROOT, SOURCE, plugin
from .test_supervisor_smoke import wait_until


def home_with_importer(tmp_path):
    home = tmp_path / "fictional-home"
    shutil.copytree(ROOT / "examples/home", home)
    folder = home / "companions/companion_a"
    shutil.copytree(folder, home / "companions/companion_b")
    path = home / "companions/companion_b/companion.yaml"
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    config["id"] = "companion_b"
    config["channels"][0]["account"] = "example_bot_b"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    path = home / "clytie.local.yaml"
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    config["companions"].append("companion_b")
    config["accounts"]["example_bot_b"] = {"channel": "fake-channel", "account": "1000000004"}
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    path = folder / "companion.yaml"
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    config["plugins"]["fictional-importer"] = {"enabled": True, "config": {}}
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    plugin(
        home,
        "fictional-importer",
        SOURCE
        + """
    def make_importer(self):
        return self
    def detect(self, source):
        import os
        import threading
        # This blocks the worker event loop. Only the independent control
        # thread can observe cancellation until the external barrier opens.
        with (source.parent / "entered").open("a", encoding="utf-8") as out:
            out.write(str(os.getpid()) + "\\n")
        while not (source.parent / "release").exists():
            threading.Event().wait(0.01)
        return True
    def read(self, source, cancel):
        from clytie.sdk.types import ImportCandidate
        candidate = ImportCandidate(record_id="e" * 32, kind="fact",
            content="fictional memory", visibility="private",
            created_at=self.ctx.clock.now(), updated_at=self.ctx.clock.now())
        class Iterator:
            done = False
            def __aiter__(self):
                return self
            async def __anext__(self):
                cancel.raise_if_cancelled()
                if self.done:
                    raise StopAsyncIteration
                self.done = True
                return candidate
        return Iterator()
""",
        contributes={
            "memory": {"importer": {"factory": "make_importer", "formats": ["fictional"]}}
        },
    )
    source = home / "fixture/source.txt"
    source.parent.mkdir()
    source.write_text("fictional source", encoding="utf-8")
    return home, source


def arguments(source):
    return dict(
        source=str(source), mode="new", importer="fictional-importer", confirm_identity=True
    )


async def until(predicate):
    # Wall time only bounds test-process scheduling, never business deadlines.
    await wait_until(predicate, SystemClock(), timeout=30)


def close_exited_launcher(launcher):
    launcher.job.close()
    for child in launcher.children:
        assert child.process.poll() is not None
        for stream in (child.process.stdin, child.process.stdout, child.log):
            try:
                stream.close()
            except OSError as exc:
                # Repeated escalation can leave a buffered command addressed
                # to an already exited child. Windows rejects its final flush.
                assert stream is child.process.stdin and exc.errno in {errno.EINVAL, errno.EPIPE}


class BarrierLauncher(ProcessLauncher):
    async def launch(self, cid):
        if cid != "companion_a":
            return await super().launch(cid)
        executable, env = python_process()
        log = (self.home / "startup-process.log").open("ab")
        process = subprocess.Popen(
            [executable, "-m", "tests.management_start_fixture", str(self.home), cid],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=log,
            env=env,
            **creation(),
        )
        self.job.assign(process)
        child = Child(process, log)
        self.children.append(child)
        await child.send({"type": "boot", "seq": 0})
        return child
