"""Content-free native validation observation; no SQL or cancellation policy changes."""

import asyncio
import functools
import importlib
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from unittest.mock import patch

_validation = ContextVar("measurement_native_validation", default=None)
_stage = ContextVar("measurement_native_stage", default=None)


def _is_cancelled(error):
    # SDK Cancelled inherits asyncio.CancelledError. Worker controls instead
    # raise their own exception, without a code. Compare the actual loaded
    # product type; names and exception text are not cancellation evidence.
    worker = sys.modules.get("clytie.backup.worker")
    worker_cancelled = getattr(worker, "Cancelled", None)
    return (
        isinstance(error, asyncio.CancelledError)
        or (isinstance(worker_cancelled, type) and isinstance(error, worker_cancelled))
        or getattr(error, "code", None) == "cancelled"
    )


class NativeObservation:
    def __init__(self, observer):
        self.observer = observer
        self.pending = {}
        self.connections = 0
        self.legacy = False

    def before_sql(self, db, sql, request):
        if not self.legacy or not isinstance(sql, str) or _validation.get() is not None:
            return None
        # Only fixed SQLite checks initiate an implicit legacy scope. The SQL is
        # inspected in memory and is never retained in evidence.
        words = sql.strip().rstrip(";").lower().split()
        if words not in (["pragma", "quick_check"], ["pragma", "integrity_check"]):
            return None
        existing = getattr(db, "_measurement_implicit", None)
        if existing is not None:
            return existing
        state = self.start("legacy_validation", request)
        state["implicit"] = True
        self.bind(db, state)
        db._measurement_implicit = state
        return state

    def cursor(self, cursor, state):
        if state is None:
            return cursor
        observation = self

        class Cursor:
            def __getattr__(self, name):
                return getattr(cursor, name)

            def __iter__(self):
                return self

            def _call(self, function, *args):
                try:
                    return function(*args)
                except StopIteration:
                    raise
                except BaseException:
                    state["outcome"] = "failed"
                    raise
                finally:
                    state["last_call"] = observation.observer.now()

            def __next__(self):
                return self._call(next, cursor)

            def fetchone(self):
                return self._call(cursor.fetchone)

            def fetchmany(self, *args):
                return self._call(cursor.fetchmany, *args)

            def fetchall(self):
                return self._call(cursor.fetchall)

            def close(self):
                return self._call(cursor.close)

        return Cursor()

    def event(self, state, event, *, connection=0, outcome=None, at=None):
        observer = self.observer
        if observer.recorder.failed:
            return
        with observer.guard:
            observer.sequence += 1
            identifier = observer.sequence
        observer.recorder.append(
            "storage-phases.jsonl",
            dict(
                id=identifier,
                request_id=state["request"],
                phase="native_validation",
                validation_id=state["id"],
                connection_id=connection,
                event=event,
                at_s=observer.now() if at is None else at,
                outcome=state["outcome"] if outcome is None else outcome,
                internal_read_bytes=None,
                boundary=state["boundary"],
            ),
        )

    def start(self, boundary, request):
        with self.observer.guard:
            self.observer.sequence += 1
            identifier = self.observer.sequence
        state = dict(
            id=identifier,
            request=request,
            boundary=boundary,
            outcome="ok",
            connections=set(),
            closed=set(),
            ended=False,
            cancelled=False,
            stage=_stage.get(),
            stage_ended=False,
            finished=False,
        )
        self.pending[identifier] = state
        if state["stage"] is not None:
            state["stage"].append(state)
        self.event(state, "started")
        return state

    def bind(self, db, state=None):
        state = _validation.get() if state is None else state
        if state is None:
            return
        if not hasattr(db, "_measurement_connection"):
            with self.observer.guard:
                self.connections += 1
                db._measurement_connection = self.connections + (self.observer.sequence >> 40 << 40)
            db._measurement_validations = []
        if state not in db._measurement_validations:
            db._measurement_validations.append(state)
            state["connections"].add(db._measurement_connection)
            self.event(state, "connection_attached", connection=db._measurement_connection)

    def cancel(self, state):
        if not state["cancelled"]:
            state["cancelled"] = True
            state["outcome"] = "cancelled"
            self.event(state, "cancel_accepted", at=state.get("interrupted_at"))

    def interrupted(self, state):
        if "interrupted_at" not in state:
            state["interrupted_at"] = self.observer.now()
            self.event(state, "interrupt_accepted", at=state["interrupted_at"])

    def end(self, state, error=None):
        if state["ended"]:
            return
        if error is not None:
            if _is_cancelled(error):
                self.cancel(state)
            elif state["outcome"] != "cancelled":
                state["outcome"] = "failed"
        state["ended"] = True
        self.event(state, "driver_finished", at=state.get("last_call"))
        self.finish(state)

    def finish(self, state):
        if state["stage_ended"] and not state["finished"]:
            self.event(state, "finished")
            state["finished"] = True
        if (
            state["ended"]
            and state["connections"] == state["closed"]
            and (state["stage"] is None or state["stage_ended"])
            and state["id"] in self.pending
        ):
            if not state["finished"]:
                self.event(state, "finished")
                state["finished"] = True
            self.pending.pop(state["id"])

    def closed(self, db):
        for state in getattr(db, "_measurement_validations", ()):
            identifier = db._measurement_connection
            if identifier not in state["closed"]:
                state["closed"].add(identifier)
                self.event(state, "connection_closed", connection=identifier)
                self.finish(state)
        db._measurement_validations = []

    def wrap_function(self, function, request):
        @functools.wraps(function)
        def measured(*args, **kwargs):
            if _validation.get() is not None:
                return function(*args, **kwargs)
            state = self.start("legacy_validation", request())
            token = _validation.set(state)
            error = None
            try:
                return function(*args, **kwargs)
            except BaseException as exc:
                error = exc
                raise
            finally:
                _validation.reset(token)
                self.end(state, error)

        return measured

    def install(self, stack, request):
        try:
            helper = importlib.import_module("clytie.backup.native_sqlite")
        except ModuleNotFoundError as exc:
            if exc.name != "clytie.backup.native_sqlite":
                raise
            helper = None
        replacements = []
        if helper is not None:
            original_checks, original_stage = helper.native_checks, helper.validation_stage

            @contextmanager
            def checks(db):
                state = self.start("native_checks", request())
                self.bind(db, state)
                token = _validation.set(state)
                error = None
                try:
                    with original_checks(db):
                        yield
                except BaseException as exc:
                    error = exc
                    raise
                finally:
                    _validation.reset(token)
                    self.end(state, error)

            @contextmanager
            def stage():
                if _stage.get() is not None:
                    with original_stage():
                        yield
                    return
                states = []
                error = None
                try:
                    with original_stage():
                        token = _stage.set(states)
                        try:
                            yield
                        finally:
                            _stage.reset(token)
                except BaseException as exc:
                    error = exc
                    raise
                finally:
                    for state in states:
                        if error is not None and state["outcome"] == "ok":
                            state["outcome"] = "cancelled" if _is_cancelled(error) else "failed"
                        state["stage_ended"] = True
                        self.finish(state)

            replacements.extend(((original_checks, checks), (original_stage, stage)))
        else:
            self.legacy = True
            # Freeze exact known legacy validation entry points, including imported
            # aliases. No replacement SQL, extra reads, or progress callback.
            for name, attributes in (
                ("clytie.backup.restore", ("_database", "_companion")),
                ("clytie.backup.creation", ("_validate",)),
            ):
                module = importlib.import_module(name)
                for attribute in attributes:
                    function = getattr(module, attribute)
                    replacements.append((function, self.wrap_function(function, request)))
        for module_name, module in tuple(sys.modules.items()):
            if module_name.startswith("clytie.") and module is not None:
                for name, value in tuple(vars(module).items()):
                    for original, replacement in replacements:
                        if value is original:
                            stack.enter_context(patch.object(module, name, replacement))

        def restore_late_aliases():
            # Modules imported while observation is installed can copy a wrapped
            # helper. Restore those aliases too, before the next run owns timing.
            for module_name, module in tuple(sys.modules.items()):
                if module_name.startswith("clytie.") and module is not None:
                    for name, value in tuple(vars(module).items()):
                        for original, replacement in replacements:
                            if value is replacement:
                                setattr(module, name, original)

        stack.callback(restore_late_aliases)

    def check_unsettled(self):
        unsettled = False
        for state in tuple(self.pending.values()):
            if state.get("implicit"):
                self.end(state)
            for identifier in sorted(state["connections"] - state["closed"]):
                self.event(
                    state, "connection_close_unknown", connection=identifier, outcome="unknown"
                )
            if state["boundary"] == "native_checks":
                unsettled = True
        if unsettled:
            self.observer.recorder.failed = True
            raise ValueError("measurement_native_unsettled")
