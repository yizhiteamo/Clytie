"""Draft v6: observe existing refill/charge state, never read a new clock value.

All timing used for accounting is the product's existing `_updated` or signal
receipt. Wall at_s is archival ordering only. No SQL, polls, waits or policy edits.
"""

import functools
import hashlib
from contextvars import ContextVar
from unittest.mock import patch

if __package__:
    from . import measurement_evidence as evidence
    from .measurement_io_observation import _outcome
else:
    import measurement_evidence as evidence
    from measurement_io_observation import _outcome

BASE = ("id", "process_sequence", "phase", "at_s")
FIELDS = (
    *BASE,
    "event",
    "budget_id",
    "charge_call_id",
    "refill_id",
    "stage_id",
    "charge_sequence",
    "direction",
    "requested_bytes",
    "before",
    "after",
    "owners",
    "signals",
    "outcome",
)
STATE = ("updated", "tokens", "normal", "low_since", "charged")
SOURCE = (*BASE, "owner_id", "budget_id", "controller_hash", "owner_hash", "source")


def token(value):
    return hashlib.sha256(value.encode()).hexdigest()


def state(budget):
    return dict(
        updated=budget._updated,
        tokens=budget._tokens,
        normal=budget._normal,
        low_since=budget._low_since,
        charged=dict(budget._charged),
    )


def validate_record(row):
    phase = row["phase"]
    evidence._fields(row, SOURCE if phase == "pressure_source_binding" else FIELDS)
    evidence._enum(phase, {"pressure_source_binding", "budget_accounting"})
    sequence = row["process_sequence"]
    evidence._number(sequence, integer=True)
    if sequence >= 256:
        raise evidence.EvidenceError("accounting_namespace_invalid")
    for field in ("id", "budget_id", "owner_id", "charge_call_id", "refill_id"):
        value = row.get(field)
        if value is not None:
            evidence._number(value, integer=True, positive=True)
            if not (sequence + 1) << 40 < value < (sequence + 2) << 40:
                raise evidence.EvidenceError("accounting_namespace_invalid")
    evidence._number(row["at_s"])
    if phase == "pressure_source_binding":
        for field in ("controller_hash", "owner_hash"):
            evidence._pattern(row[field], r"[a-f0-9]{64}")
        evidence._enum(row["source"], {"explicit_trial_object_binding"})
        return
    evidence._enum(row["event"], {"budget_created", "refill_returned", "charge_returned"})
    evidence._enum(row["outcome"], {"ok", "failed", "cancelled", "unknown"})
    if row["stage_id"] is not None:
        evidence._pattern(row["stage_id"], r"[a-f0-9]{64}")
    if row["charge_sequence"] is not None:
        evidence._number(row["charge_sequence"], integer=True, positive=True)
    if row["requested_bytes"] is not None:
        evidence._number(row["requested_bytes"], integer=True)
    evidence._enum(row["direction"], {"read", "write", "none"})
    for item in (row["before"], row["after"]):
        if item is None:
            continue
        evidence._fields(item, STATE)
        for field in ("updated", "tokens"):
            evidence._number(item[field])
        if item["low_since"] is not None:
            evidence._number(item["low_since"])
        if type(item["normal"]) is not bool:
            raise evidence.EvidenceError("accounting_boolean_invalid")
        evidence._fields(item["charged"], ("read", "write"))
        for value in item["charged"].values():
            evidence._number(value, integer=True)
    owners, signals = row["owners"], row["signals"]
    if owners is not None:
        if type(owners) is not list or len(owners) != len(set(owners)):
            raise evidence.EvidenceError("accounting_owners_invalid")
        for owner in owners:
            evidence._pattern(owner, r"[a-f0-9]{64}")
    if signals is not None:
        if type(signals) is not dict:
            raise evidence.EvidenceError("accounting_signals_invalid")
        for owner, values in signals.items():
            evidence._pattern(owner, r"[a-f0-9]{64}")
            if type(values) is not list or len(values) != 4 or type(values[3]) is not bool:
                raise evidence.EvidenceError("accounting_signal_invalid")
            for value in values[:3]:
                evidence._number(value)


class BudgetObservation:
    def __init__(self, io):
        self.io = io
        self.charge = ContextVar("measurement_actual_budget_charge", default=None)

    def safe(self, function):
        try:
            return function()
        except Exception:
            self.io.observer.recorder.failed = True
            return None

    def emit(self, budget, event, context=None, **changes):
        fields = dict(
            budget_id=self.io.budget_identifier(budget),
            charge_call_id=None,
            refill_id=None,
            stage_id=None,
            charge_sequence=None,
            direction="none",
            requested_bytes=None,
            before=None,
            after=None,
            owners=None,
            signals=None,
            outcome="unknown",
        )
        if context is not None:
            fields.update({key: context[key] for key in fields if key in context})
        fields.update(changes)
        self.io.event("budget_accounting", event=event, **fields)

    def bind_source(self, owner, control, session):
        # This is an explicit call at the harness's actual object binding point,
        # before its first sample. It cannot assert a remote daemon pipe mapping.
        if self.io.observer.pressure is None or not control.sessions[session].storage_owner:
            raise ValueError("pressure_source_binding_invalid")
        self.io.event(
            "pressure_source_binding",
            owner_id=self.io.observer.pressure.identifier(owner),
            budget_id=self.io.budget_identifier(control.budget),
            controller_hash=token(control.controller),
            owner_hash=token(session),
            source="explicit_trial_object_binding",
        )

    def install(self, stack):
        if self.io.budget is None:
            return
        cls = self.io.budget.IoBudget
        original_init, original_refill, original_charge = cls.__init__, cls._refill, cls._charge

        @functools.wraps(original_init)
        def initialize(budget, *args, **kwargs):
            result = original_init(budget, *args, **kwargs)
            self.safe(
                lambda: self.emit(
                    budget,
                    "budget_created",
                    after=state(budget),
                    owners=None,
                    signals={},
                    outcome="ok",
                )
            )
            return result

        @functools.wraps(original_refill)
        def refill(budget):
            before = self.safe(lambda: state(budget))
            owners = self.safe(
                lambda: (
                    None
                    if budget._owners is None
                    else sorted(token(owner) for owner in budget._owners)
                )
            )
            signals = self.safe(
                lambda: {token(owner): list(values) for owner, values in budget._signals.items()}
            )
            context = self.charge.get()
            context = context if context is not None and context["budget"] is budget else None
            refill_id = self.safe(self.io.identifier)
            error = None
            try:
                return original_refill(budget)
            except BaseException as exc:
                error = exc
                raise
            finally:

                def record():
                    if context is not None:
                        context["refill_id"] = refill_id
                    self.emit(
                        budget,
                        "refill_returned",
                        context,
                        refill_id=refill_id,
                        before=before,
                        after=state(budget),
                        owners=owners,
                        signals=signals,
                        outcome=_outcome(error),
                    )

                self.safe(record)

        @functools.wraps(original_charge)
        async def charge(budget, stage, direction, size):
            context = self.safe(
                lambda: dict(
                    budget=budget,
                    charge_call_id=self.io.identifier(),
                    stage_id=token(stage.key.stage),
                    charge_sequence=stage.sequence,
                    direction=direction,
                    requested_bytes=size,
                    refill_id=None,
                )
            )
            binding = self.charge.set(context)
            error = None
            try:
                return await original_charge(budget, stage, direction, size)
            except BaseException as exc:
                error = exc
                raise
            finally:
                try:
                    self.safe(
                        lambda: self.emit(
                            budget,
                            "charge_returned",
                            context,
                            after=state(budget),
                            outcome=_outcome(error),
                        )
                    )
                finally:
                    self.charge.reset(binding)

        stack.enter_context(patch.object(cls, "__init__", initialize))
        stack.enter_context(patch.object(cls, "_refill", refill))
        stack.enter_context(patch.object(cls, "_charge", charge))
