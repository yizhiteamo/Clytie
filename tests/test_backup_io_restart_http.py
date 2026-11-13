"""Production daemon restart with a surviving synchronous management executor."""

import asyncio
import json
import os
import signal
import socket
import sqlite3
from uuid import uuid4

import yaml

from clytie.api.client import Client, detached_start
from clytie.api.management_journal import Journal, executor_alive
from clytie.clock import SystemClock
from clytie.plugin_host.locking import process_identity
from clytie.supervisor.common import read
from clytie.supervisor.entry import status

from .host_helpers import SOURCE, plugin
from .management_integration_helpers import arguments, home_with_importer, until


def fictional_home(tmp_path):
    home, source = home_with_importer(tmp_path)
    # A first ordinary conversation in companion_b starts only after restart.
    plugin(
        home,
        "fictional-channel",
        SOURCE
        + """
    def create_channel(self):
        from clytie.sdk.testing import FakeChannel
        from clytie.sdk.types import (Actor, Conversation, EventEnvelope,
            Message, MessageRef, MentionPart, TextPart)
        cid = self.ctx.companion_id
        channel = "example_bot_b" if cid == "companion_b" else "example_bot"
        ref = MessageRef(channel_id=channel, conversation_id="test_group",
            platform_message_id="fictional-start-" + cid)
        event = EventEnvelope(event_id=channel + ":" + ref.platform_message_id,
            type="conversation.message", companion_id=cid, channel_id=channel,
            conversation=Conversation(conversation_id="test_group", kind="group",
                scene="public_group", channel_id=channel),
            message=Message(ref=ref, sender=Actor(actor_id="owner", role="owner",
                channel_id=channel), parts=[MentionPart(actor_id=cid, is_self=True),
                TextPart(text="Fictional greeting.")], sent_at=self.ctx.clock.now()),
            received_at=self.ctx.clock.now())
        return FakeChannel(self.ctx, incoming=(event,))
""",
        category="channel",
        contributes={"channel": {"factory": "create_channel", "platform": "fictional"}},
    )
    paths = [home / "clytie.local.yaml"] + [
        home / "companions" / cid / "companion.yaml" for cid in ("companion_a", "companion_b")
    ]
    for path in paths:
        path.write_text(
            path.read_text(encoding="utf-8").replace("fake-channel", "fictional-channel"),
            encoding="utf-8",
        )
    path = home / "clytie.local.yaml"
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    config["companions"] = ["companion_a"]
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        config["api"] = {"port": sock.getsockname()[1]}
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return home, source


def sent_reply(home):
    path = home / "companions/companion_b/ledger/ledger.sqlite3"
    if not path.exists():
        return False
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        try:
            rows = [json.loads(row[0]) for row in db.execute("SELECT data FROM intent")]
        except sqlite3.OperationalError:
            return False  # The startup schema is not committed yet.
    return any(row.get("status") == "sent" for row in rows)


def startup_observation(home, reply):
    """Fixed diagnostics from this fictional home; no arbitrary process output."""
    codes = []
    process_log = home / "logs/supervisor/process.log"
    if process_log.exists():
        for line in process_log.read_bytes().splitlines():
            try:
                row = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if isinstance(row, dict) and row.get("type") == "error":
                code = row.get("code")
                codes.append(
                    code
                    if code in {"api_bind_failed", "already_running", "config_invalid"}
                    else "other_start_error"
                )
    port = yaml.safe_load((home / "clytie.local.yaml").read_bytes())["api"]["port"]
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError as exc:
            bind_errno = exc.errno
        else:
            bind_errno = None
    return dict(
        response=reply,
        daemon_codes=codes,
        instance_locked=status(home)["running"],
        port_bind_errno=bind_errno,
    )


def test_startup_observation_retains_only_fixed_codes_and_real_bind_result(tmp_path):
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        (tmp_path / "clytie.local.yaml").write_text(
            yaml.safe_dump({"api": {"port": occupied.getsockname()[1]}}), encoding="utf-8"
        )
        log = tmp_path / "logs/supervisor/process.log"
        log.parent.mkdir(parents=True)
        log.write_text(
            "unstructured fictional detail\n"
            '{"type":"error","code":"api_bind_failed"}\n'
            '{"type":"error","code":"fictional-private-detail"}\n',
            encoding="utf-8",
        )
        result = startup_observation(tmp_path, {"ok": False})
    assert result["daemon_codes"] == ["api_bind_failed", "other_start_error"]
    assert result["port_bind_errno"] is not None
    assert not result["instance_locked"]
    assert "fictional-private-detail" not in json.dumps(result)


async def test_real_daemon_restart_blocks_orphan_io_but_other_companion_chats(tmp_path):
    home, source = fictional_home(tmp_path)
    clock, client = SystemClock(), Client(home)
    key = uuid4().hex
    path = home / "companions/companion_a/management/operations" / (key + ".json")
    task = None
    identity = None
    try:
        started = await detached_start(home, clock)
        assert started["ok"], startup_observation(home, started)

        async def current():
            reply = await client.request("GET", "/api/v1/status")
            assert reply.status == 200, reply.json
            return reply.json["result"]

        for _ in range(300):
            if (await current())["companions"]["companion_a"]["state"] == "running":
                break
            await clock.sleep(0.1)
        else:
            raise AssertionError("fictional companion did not start")
        stopped = await client.request(
            "POST", "/api/v1/companions/companion_a/stop", json={"confirm": True}
        )
        assert stopped.status == 200, stopped.json
        task = asyncio.create_task(
            client.request(
                "POST",
                "/api/v1/companions/companion_a/memory/import/plan",
                json={**arguments(source), "operation_id": key},
            )
        )
        await until(lambda: (source.parent / "entered").exists())
        identity = Journal.load(path, clock).value["executor"]
        daemon = process_identity(read(home / "run/api.json")["pid"])
        assert identity != daemon and executor_alive(identity) is True
        assert process_identity(daemon["pid"]) == daemon
        os.kill(daemon["pid"], signal.SIGTERM if os.name == "nt" else signal.SIGKILL)
        await until(lambda: executor_alive(daemon) is False)
        assert (await task).status == 503
        assert executor_alive(identity) is True
        config_path = home / "clytie.local.yaml"
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        config["companions"].append("companion_b")
        config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
        restarted = await detached_start(home, clock)
        assert restarted["ok"], startup_observation(home, restarted)
        result = await current()
        assert result["heavy_io"]["state"] == "blocked"
        assert any(row["operation"] == key for row in result["heavy_io"]["blockers"])
        await until(lambda: sent_reply(home))
        result = await current()
        assert result["companions"]["companion_b"]["state"] == "running"
        denied = await client.request(
            "POST",
            "/api/v1/companions/companion_b/memory/import/plan",
            json=arguments(source),
        )
        assert denied.status == 409, denied.json
        assert denied.json["error"]["code"] == "backup_io_recheck_required"
        directory = home / "companions/companion_b/management/operations"
        assert not directory.exists() or not list(directory.iterdir())
        assert len((source.parent / "entered").read_text().splitlines()) == 1
        (source.parent / "release").touch()
        await until(lambda: executor_alive(identity) is False)
        result = await current()
        assert result["heavy_io"]["state"] == "ready", result["heavy_io"]
        assert Journal.load(path, clock).value["state"] == "cancelled"
        assert not Journal.load(path, clock).value["committed"]
        assert len((source.parent / "entered").read_text().splitlines()) == 1
    finally:
        (source.parent / "release").touch()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        if status(home)["running"]:
            response = await client.request(
                "POST", "/api/v1/supervisor/stop", json={"confirm": True}
            )
            assert response.status == 200, response.json
            await until(lambda: not status(home)["running"])
        if identity is not None:
            await until(lambda: executor_alive(identity) is False)
