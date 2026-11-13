"""V6 observation of real control objects and already-read durable identities.

No new storage access, authorization, I/O, retry, SQL or measurement execution.
Unknown identities are retained. This module does not certify restart isolation.
"""

import asyncio
import functools
import hashlib
import importlib
import json
import math
import re
import sys
import threading
from pathlib import Path
from unittest.mock import patch
from weakref import WeakKeyDictionary

PHASE = "io_control_binding"
HASH_FIELDS = (
    "instance_hash",
    "controller_hash",
    "session_hash",
    "operation_hash",
    "stage_id",
    "generation_hash",
    "execution_hash",
    "responsibility_controller_hash",
    "call_hash",
)
FIELDS = (
    "id",
    "process_sequence",
    "phase",
    "at_s",
    "event",
    "source",
    "budget_id",
    *HASH_FIELDS,
    "action",
    "role",
    "responsibility_state",
    "storage_owner",
    "outcome",
)
EVENTS = {
    "observer_started",
    "control_created",
    "ledger_loaded",
    "ledger_saved",
    "ledger_failed",
    "responsibility_observed",
    "session_registered",
    "endpoint_bound",
    "request_started",
    "request_finished",
}
SOURCES = {"runtime_object", "validated_ledger", "durable_save", "unknown"}


class BindingError(ValueError):
    pass


def identity_hash(value):
    """Canonical structured identity; never serialize paths or identity preimages."""
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def token_hash(value):
    # Match the existing raw-stage/owner/session hash convention.
    return hashlib.sha256(value.encode()).hexdigest() if isinstance(value, str) else None


def outcome(error):
    worker = sys.modules.get("clytie.backup.worker")
    cancelled = getattr(worker, "Cancelled", None)
    return (
        "cancelled"
        if (
            isinstance(error, asyncio.CancelledError)
            or isinstance(cancelled, type)
            and isinstance(error, cancelled)
            or getattr(error, "code", None) == "cancelled"
        )
        else "failed"
    )


def validate_record(row):
    if set(row) != set(FIELDS) or row["phase"] != PHASE:
        raise BindingError("control_binding_fields_invalid")
    for field in ("id", "process_sequence"):
        if type(row[field]) is not int or row[field] < 0:
            raise BindingError("control_binding_namespace_invalid")
    sequence = row["process_sequence"]
    if sequence >= 256 or not (sequence + 1) << 40 < row["id"] < (sequence + 2) << 40:
        raise BindingError("control_binding_namespace_invalid")
    budget = row["budget_id"]
    if budget is not None and (
        type(budget) is not int or not (sequence + 1) << 40 < budget < (sequence + 2) << 40
    ):
        raise BindingError("control_binding_budget_invalid")
    if type(row["at_s"]) not in {int, float} or not math.isfinite(row["at_s"]) or row["at_s"] < 0:
        raise BindingError("control_binding_time_invalid")
    for field in HASH_FIELDS:
        value = row[field]
        if value is not None and (
            type(value) is not str or re.fullmatch(r"[a-f0-9]{64}", value) is None
        ):
            raise BindingError("control_binding_hash_invalid")
    for field, choices in (
        ("event", EVENTS),
        ("source", SOURCES),
        ("action", {"acquire", "charge", "release", "unknown"}),
        ("role", {"operation", "recovery", "capture", "startup_recovery", "unknown"}),
        ("responsibility_state", {"reserved", "active", "ended", "unknown"}),
        ("outcome", {"ok", "failed", "cancelled", "unknown"}),
    ):
        if row[field] not in choices:
            raise BindingError("control_binding_enum_invalid")
    if row["storage_owner"] is not None and type(row["storage_owner"]) is not bool:
        raise BindingError("control_binding_owner_invalid")
    if row["event"] in {"request_started", "request_finished"} and row["call_hash"] is None:
        raise BindingError("control_binding_call_missing")
    if row["event"] == "request_started" and row["outcome"] != "unknown":
        raise BindingError("control_binding_call_outcome_invalid")


class ControlObservation:
    def __init__(self, io):
        self.io = io
        self.guard = threading.RLock()
        self.controls = WeakKeyDictionary()
        self.ledgers = WeakKeyDictionary()
        self.endpoints = WeakKeyDictionary()

    def safe(self, function):
        try:
            with self.guard:
                return function()
        except Exception:
            self.io.observer.recorder.failed = True
            return None

    def event(self, event, context=None, **changes):
        fields = dict.fromkeys(HASH_FIELDS)
        fields.update(
            budget_id=None,
            action="unknown",
            role="unknown",
            responsibility_state="unknown",
            storage_owner=None,
            source="unknown",
            outcome="unknown",
        )
        if context is not None:
            fields.update({key: context[key] for key in fields if key in context})
        fields.update(changes)
        self.io.event(PHASE, event=event, **fields)

    def control_created(self, control):
        context = dict(
            controller_hash=token_hash(control.controller),
            budget_id=self.io.budget_identifier(control.budget),
            instance_hash=None,
            records={},
        )
        self.controls[control] = context
        if control.responsibilities is not None:
            self.ledgers[control.responsibilities] = context
        self.event("control_created", context, source="runtime_object", outcome="ok")

    def approved_ledger(self, ledger):
        # Compare only paths already held by the real objects. No file is opened.
        root = Path(self.io.observer.root)
        return ledger.home == root or root in ledger.home.parents

    def ledger_observed(self, ledger, value, source):
        context = self.ledgers.get(ledger)
        if context is None or not self.approved_ledger(ledger):
            self.event("ledger_loaded" if source == "validated_ledger" else "ledger_saved")
            return
        instance = token_hash(value["instance"])
        if context["instance_hash"] not in {None, instance}:
            self.io.observer.recorder.failed = True
            return
        context["instance_hash"] = instance
        self.event(
            "ledger_loaded" if source == "validated_ledger" else "ledger_saved",
            context,
            source=source,
            outcome="ok",
        )
        # Input is the product's validated load or its successfully saved object.
        # Do not reread anchor/index, retain raw preimages, or inspect journal text.
        for generation, record in value["records"].items():
            fields = dict(
                instance_hash=instance,
                generation_hash=token_hash(generation),
                responsibility_controller_hash=token_hash(record["controller"]),
                session_hash=token_hash(record["session"]),
                operation_hash=token_hash(record["operation"]),
                execution_hash=identity_hash(record["identity"])
                if record["identity"] is not None
                else None,
                responsibility_state=record["state"],
                role=record["role"],
            )
            key = (
                fields["responsibility_controller_hash"],
                fields["session_hash"],
                fields["operation_hash"],
            )
            prior = context["records"].get(key)
            context["records"][key] = fields
            if prior != fields:
                self.event(
                    "responsibility_observed", context, **fields, source=source, outcome="ok"
                )

    def endpoint_bound(self, control, session, operation):
        context = self.controls.get(control)
        endpoint = control.sessions[session].endpoints[operation]
        if context is None:
            self.event("endpoint_bound")
            return
        bound = {key: value for key, value in context.items() if key != "records"}
        bound.update(
            session_hash=token_hash(endpoint.executor),
            operation_hash=token_hash(endpoint.operation),
        )
        key = bound["controller_hash"], bound["session_hash"], bound["operation_hash"]
        record = context["records"].get(key)
        if record is not None:
            bound.update(record)
        if endpoint.budget is not control.budget:
            self.io.observer.recorder.failed = True
            return
        prior = self.endpoints.get(endpoint)
        if prior is not None and prior != bound:
            self.io.observer.recorder.failed = True
            return
        self.endpoints[endpoint] = bound
        if prior is None:
            self.event("endpoint_bound", bound, source="runtime_object", outcome="ok")

    def request_context(self, endpoint):
        bound = dict(self.endpoints.get(endpoint, {}))
        if bound and (
            bound["budget_id"] != self.io.budget_identifier(endpoint.budget)
            or bound["session_hash"] != token_hash(endpoint.executor)
            or bound["operation_hash"] != token_hash(endpoint.operation)
        ):
            # Do not attribute a changed real endpoint to its old authorization.
            # Observation failure invalidates evidence; the original call still runs.
            self.io.observer.recorder.failed = True
            return {}
        return bound

    def install(self, stack):
        try:
            module = importlib.import_module("clytie.backup.io_control")
            ledger_module = importlib.import_module("clytie.backup.io_responsibility")
            endpoint_module = importlib.import_module("clytie.backup.io_protocol")
        except ModuleNotFoundError as error:
            if error.name not in {
                "clytie.backup.io_control",
                "clytie.backup.io_responsibility",
                "clytie.backup.io_protocol",
            }:
                raise
            self.event("observer_started")
            return
        self.event("observer_started", source="runtime_object", outcome="ok")
        cls, ledger_cls = module.IoControl, ledger_module.Responsibilities
        original_init, original_register, original_authorize = (
            cls.__init__,
            cls.register,
            cls.authorize,
        )

        @functools.wraps(original_init)
        def initialize(control, *args, **kwargs):
            result = original_init(control, *args, **kwargs)
            self.safe(lambda: self.control_created(control))
            return result

        @functools.wraps(original_register)
        def register(control, *args, **kwargs):
            session = original_register(control, *args, **kwargs)
            self.safe(
                lambda: self.event(
                    "session_registered",
                    self.controls.get(control),
                    session_hash=token_hash(session),
                    storage_owner=control.sessions[session].storage_owner,
                    source="runtime_object",
                    outcome="ok",
                )
            )
            return session

        @functools.wraps(original_authorize)
        def authorize(control, session, operation):
            result = original_authorize(control, session, operation)
            self.safe(lambda: self.endpoint_bound(control, session, operation))
            return result

        stack.enter_context(patch.object(cls, "__init__", initialize))
        stack.enter_context(patch.object(cls, "register", register))
        stack.enter_context(patch.object(cls, "authorize", authorize))

        for method, source in (("_load", "validated_ledger"), ("_save", "durable_save")):
            original = getattr(ledger_cls, method)

            def wrap_ledger(function, selected, provenance):
                @functools.wraps(function)
                def observed(ledger, *args, **kwargs):
                    try:
                        result = function(ledger, *args, **kwargs)
                    except BaseException:
                        self.safe(
                            lambda: self.event(
                                "ledger_failed", self.ledgers.get(ledger), outcome="failed"
                            )
                        )
                        raise
                    value = result if selected == "_load" else args[0] if args else kwargs["value"]
                    self.safe(lambda: self.ledger_observed(ledger, value, provenance))
                    return result

                return observed

            stack.enter_context(
                patch.object(ledger_cls, method, wrap_ledger(original, method, source))
            )

        original_request = endpoint_module.BudgetEndpoint.request

        @functools.wraps(original_request)
        async def request(endpoint, arguments):
            # Existing io_charge already records every real budget charge. Bind
            # the endpoint only at stage boundaries, avoiding duplicated per-chunk
            # metadata and any claim that protocol dispatch proves physical I/O.
            if type(arguments) is dict and arguments.get("action") == "charge":
                return await original_request(endpoint, arguments)
            frozen = self.safe(lambda: self.request_context(endpoint))
            frozen = frozen or {}
            call = self.safe(lambda: identity_hash(["observation_call", self.io.identifier()]))
            stage = arguments.get("stage") if type(arguments) is dict else None
            stage = token_hash(stage) if type(stage) is str and 0 < len(stage) <= 128 else None
            action = arguments.get("action") if type(arguments) is dict else None
            action = (
                action
                if type(action) is str and action in {"acquire", "charge", "release"}
                else "unknown"
            )
            fields = dict(stage_id=stage, action=action, call_hash=call)
            self.safe(
                lambda: self.event("request_started", frozen, **fields, source="runtime_object")
            )
            result_outcome = "ok"
            try:
                return await original_request(endpoint, arguments)
            except BaseException as error:
                result_outcome = outcome(error)
                raise
            finally:
                self.safe(
                    lambda: self.event(
                        "request_finished",
                        frozen,
                        **fields,
                        source="runtime_object",
                        outcome=result_outcome,
                    )
                )

        stack.enter_context(patch.object(endpoint_module.BudgetEndpoint, "request", request))


def single_instance_bindings(rows):
    """Order-independent mapping check, not complete I/O or restart certification.

    Empty streams, unbound initialization I/O and multiple controllers stay unknown.
    Existing NativeEvents/IoEvents still own complete lifecycle validation.
    """
    bindings = [row for row in rows if row.get("phase") == PHASE]
    slots = [row for row in rows if row.get("phase") == "io_slot" and row["event"] == "granted"]
    if not bindings or not slots:
        return dict(status="unknown", reasons=["binding_or_grant_missing"], stages=0, unbound_io=0)
    for row in bindings:
        validate_record(row)
    reasons, identities, scopes, first_loaded = set(), {}, set(), {}
    if any(row["event"] == "ledger_failed" for row in bindings):
        reasons.add("ledger_evidence_failed")
    for row in bindings:
        # A returned save proves that write, but only the validated load checks
        # index.instance against anchor.instance. A save alone is insufficient.
        if (
            row["event"] != "ledger_loaded"
            or row["source"] != "validated_ledger"
            or row["outcome"] != "ok"
        ):
            continue
        if (
            row["instance_hash"] is None
            or row["controller_hash"] is None
            or row["budget_id"] is None
        ):
            reasons.add("instance_binding_unknown")
            continue
        key = row["process_sequence"], row["budget_id"]
        identity = row["instance_hash"], row["controller_hash"]
        if key in identities and identities[key] != identity:
            raise BindingError("control_identity_changed")
        identities[key] = identity
        first_loaded[key] = min(first_loaded.get(key, row["at_s"]), row["at_s"])
        scopes.add(identity)
    if len({item[0] for item in scopes}) != 1:
        reasons.add("single_instance_unconfirmed")
    if len(scopes) != 1:
        reasons.add("controller_multiplicity_uncovered")
    if any(
        (row["process_sequence"], row["budget_id"]) not in identities
        for row in bindings
        if row["event"] == "control_created"
    ):
        reasons.add("controller_scope_unknown")
    endpoints = [
        row for row in bindings if row["event"] == "endpoint_bound" and row["outcome"] == "ok"
    ]
    requests = [
        row for row in bindings if row["event"] == "request_started" and row["action"] == "acquire"
    ]
    records = [
        row
        for row in bindings
        if row["event"] == "responsibility_observed" and row["outcome"] == "ok"
    ]
    calls = {}
    for row in bindings:
        if row["event"] in {"request_started", "request_finished"}:
            call = calls.setdefault(row["call_hash"], {})
            if row["event"] in call:
                raise BindingError("control_call_duplicated")
            call[row["event"]] = row
    for call in calls.values():
        if set(call) != {"request_started", "request_finished"}:
            reasons.add("control_call_incomplete")
            continue
        start, end = call["request_started"], call["request_finished"]
        stable = (
            "budget_id",
            *HASH_FIELDS,
            "action",
            "role",
            "responsibility_state",
            "storage_owner",
        )
        if end["at_s"] < start["at_s"] or any(start[field] != end[field] for field in stable):
            raise BindingError("control_call_binding_changed")
    stage_owners = {}
    for slot in slots:
        key = slot["process_sequence"], slot["budget_id"]
        if slot["stage_id"] in stage_owners and stage_owners[slot["stage_id"]] != key:
            raise BindingError("stage_controller_ambiguous")
        stage_owners[slot["stage_id"]] = key
        identity = identities.get(key)
        if identity is None or first_loaded[key] > slot["at_s"]:
            reasons.add("slot_controller_unconfirmed")
            continue
        matches = [
            row
            for row in requests
            if (row["process_sequence"], row["budget_id"]) == key
            and row["stage_id"] == slot["stage_id"]
            and row["at_s"] <= slot["at_s"]
        ]
        if not matches:
            reasons.add("slot_endpoint_unconfirmed")
            continue
        for request in matches:
            immutable = (
                "instance_hash",
                "controller_hash",
                "budget_id",
                "session_hash",
                "operation_hash",
                "generation_hash",
                "execution_hash",
                "responsibility_controller_hash",
            )

            def same(a, b):
                return all(a[field] == b[field] for field in immutable)

            if (request["instance_hash"], request["controller_hash"]) != identity:
                raise BindingError("slot_identity_mismatch")
            if not any(same(row, request) and row["at_s"] <= request["at_s"] for row in endpoints):
                reasons.add("endpoint_source_unconfirmed")
            if (
                request["responsibility_controller_hash"] != request["controller_hash"]
                or request["generation_hash"] is None
                or request["execution_hash"] is None
                or request["responsibility_state"] != "active"
                or not any(
                    same(row, request)
                    and row["responsibility_state"] == "active"
                    and row["at_s"] <= request["at_s"]
                    for row in records
                )
            ):
                reasons.add("responsibility_source_unconfirmed")
    unbound = sum(
        row.get("phase") in {"file_batch", "sqlite_batch", "native_validation"}
        and row.get("event") == "started"
        and row.get("stage_id") is None
        for row in rows
    )
    if unbound:
        reasons.add("unbound_io_requires_scope_classification")
    return dict(
        status="unknown" if reasons else "observed_bindings_complete",
        reasons=sorted(reasons),
        stages=len(slots),
        unbound_io=unbound,
    )
