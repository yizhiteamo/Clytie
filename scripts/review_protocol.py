"""Outer review bookkeeping. Reviewers never receive publication authority."""

import hashlib
import json
import os
import re
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4


class ReviewIncomplete(RuntimeError):
    pass


def save(path, value):
    temporary = path.with_name(path.name + "." + uuid4().hex + ".pending")
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


class ReviewProtocol:
    def __init__(self, root, pr):
        if not isinstance(pr, int) or pr < 1:
            raise ReviewIncomplete("invalid PR")
        self.directory = Path(root) / ("pr-" + str(pr))
        self.directory.mkdir(parents=True, exist_ok=True)
        self.ledger = self.directory / "rounds.json"
        self.pr = pr

    @contextmanager
    def locked(self):
        path = self.directory / "orchestrator.lock"
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise ReviewIncomplete(
                "review coordinator occupied; inspect retained evidence"
            ) from None
        try:
            yield
        finally:
            os.close(descriptor)
            path.unlink()

    def rounds(self):
        try:
            rows = json.loads(self.ledger.read_text(encoding="utf-8"))
            if not isinstance(rows, list):
                raise ValueError()
            for index, row in enumerate(rows, 1):
                if (
                    not isinstance(row, dict)
                    or row.get("round") != index
                    or not re.fullmatch("[a-f0-9]{40}", row.get("head", ""))
                    or row.get("status") not in {"review_incomplete", "report_valid"}
                    or row.get("publication") not in {"not_started", "publishing", "published"}
                ):
                    raise ValueError()
            return rows
        except FileNotFoundError:
            # Existing round directories with a missing ledger must not reset the counter.
            if any(p.name.startswith("round-") for p in self.directory.iterdir()):
                raise ReviewIncomplete("round ledger missing") from None
            return []
        except (ValueError, OSError):
            raise ReviewIncomplete("round ledger unreadable") from None

    def reserve(self, metadata):
        required = {
            "repository",
            "pr",
            "base",
            "head",
            "ci",
            "scans",
            "approved_specs",
            "checklist",
            "rules",
            "cli_version",
            "model",
            "effort",
        }
        if not required <= metadata.keys():
            raise ReviewIncomplete("snapshot metadata incomplete")
        if metadata["pr"] != self.pr or not all(
            re.fullmatch("[a-f0-9]{40}", metadata[k]) for k in ("base", "head")
        ):
            raise ReviewIncomplete("invalid commit binding")
        if metadata["model"] != "gpt-6-astra" or metadata["effort"] != "high":
            raise ReviewIncomplete("review model configuration mismatch")
        if metadata["ci"] != {"head": metadata["head"], "passed": True} or metadata["scans"] != {
            "generic": True,
            "private": True,
        }:
            raise ReviewIncomplete("required checks incomplete")
        if (
            not metadata["approved_specs"]
            or not metadata["checklist"]
            or not metadata["cli_version"]
            or not re.fullmatch("[a-f0-9]{64}", metadata["rules"])
        ):
            raise ReviewIncomplete("review evidence incomplete")
        with self.locked():
            rows = self.rounds()
            if len(rows) >= 3:
                raise ReviewIncomplete("formal review limit reached")
            if any(r["head"] == metadata["head"] for r in rows):
                raise ReviewIncomplete("commit already submitted for formal review")
            number = len(rows) + 1
            directory = self.directory / ("round-" + str(number))
            directory.mkdir(exist_ok=False)
            save(directory / "metadata.json", metadata)
            rows.append(
                {
                    "round": number,
                    "head": metadata["head"],
                    "status": "review_incomplete",
                    "publication": "not_started",
                }
            )
            save(self.ledger, rows)
            return number

    def validate(self, number, raw):
        if not isinstance(number, int) or not 1 <= number <= len(self.rounds()):
            raise ReviewIncomplete("unknown review round")
        directory = self.directory / ("round-" + str(number))
        # Preserve failed output as well as successful output; never overwrite evidence.
        with (directory / ("raw-" + uuid4().hex + ".txt")).open("x", encoding="utf-8") as stream:
            stream.write(raw)
        with self.locked():
            rows = self.rounds()
            if number < 1 or number > len(rows):
                raise ReviewIncomplete("unknown review round")
            row = rows[number - 1]
            try:
                report = json.loads(raw)
                if set(report) != {"head", "conclusion", "findings", "evidence", "uncovered"}:
                    raise ValueError()
                if report["head"] != row["head"] or report["conclusion"] not in {
                    "通过",
                    "需要修改",
                    "需要用户决定",
                }:
                    raise ValueError()
                if not all(
                    isinstance(report[key], list) for key in ("findings", "evidence", "uncovered")
                ):
                    raise ValueError()
                for finding in report["findings"]:
                    if set(finding) != {
                        "severity",
                        "problem",
                        "spec",
                        "location",
                        "evidence",
                    } or not all(isinstance(v, str) and v for v in finding.values()):
                        raise ValueError()
                    if finding["severity"] not in {"blocking", "nonblocking", "decision"}:
                        raise ValueError()
                if report["conclusion"] == "通过" and any(
                    f["severity"] != "nonblocking" for f in report["findings"]
                ):
                    raise ValueError()
                if report["conclusion"] != "通过" and not report["findings"]:
                    raise ValueError()
                if not report["evidence"] or not all(
                    isinstance(v, str) for v in report["evidence"] + report["uncovered"]
                ):
                    raise ValueError()
            except (ValueError, TypeError, KeyError):
                raise ReviewIncomplete("invalid structured report") from None
            target = directory / "report.txt"
            if target.exists() and target.read_text(encoding="utf-8") != raw:
                raise ReviewIncomplete("cannot replace an existing conclusion")
            if not target.exists():
                target.write_text(raw, encoding="utf-8")
            row["status"] = "report_valid"
            row["report_sha256"] = hashlib.sha256(raw.encode("utf-8")).hexdigest()
            save(self.ledger, rows)
            return report

    def publish(self, number, *, head, scan, send):
        with self.locked():
            rows = self.rounds()
            if not isinstance(number, int) or not 1 <= number <= len(rows):
                raise ReviewIncomplete("unknown review round")
            row = rows[number - 1]
            if row["publication"] == "published":
                return row["comment"]
            if row["publication"] != "not_started" or row["status"] != "report_valid":
                raise ReviewIncomplete("publication unavailable or delivery unknown")
            if head() != row["head"]:
                raise ReviewIncomplete("PR head changed; report is historical only")
            raw = (self.directory / ("round-" + str(number)) / "report.txt").read_text(
                encoding="utf-8"
            )
            if hashlib.sha256(raw.encode("utf-8")).hexdigest() != row.get("report_sha256"):
                raise ReviewIncomplete("validated report changed")
            if not scan(raw):
                raise ReviewIncomplete("report scan failed")
            if head() != row["head"]:
                raise ReviewIncomplete("PR head changed before publication")
            row["publication"] = "publishing"
            save(self.ledger, rows)
            comment = send(raw)
            row.update(publication="published", comment=comment)
            save(self.ledger, rows)
            return comment
