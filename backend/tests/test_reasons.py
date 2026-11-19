"""The reason catalog covers every code that can be emitted. The sets come from the Python tuples and from the source of the places that raise them, so a new code without an entry fails here."""
import re
from pathlib import Path
from typing import get_args

from app import models
from app.domain.models import IssueCode
from app.reasons import CATALOG, UNKNOWN, describe
from app.sync.engine import ERROR_CODES

APP = Path(__file__).resolve().parents[1] / "app"
NOT_EXCLUSIONS = {"key_too_long", "internal_double_space", "non_ascii_key", "duplicate_name"}  # issues that leave the record in the plan


def src(path: str) -> str:
    return (APP / path).read_text(encoding="utf-8")


def emitted() -> dict[str, set[str]]:
    report = src("preflight/report.py")
    findings = set(re.findall(r'add\((?:blocking|warnings), "(\w+)"', report)) | set(re.findall(r'for code in \(([^)]*)\)', report)[0].replace('"', "").replace(" ", "").split(","))
    findings |= {f"stocktrim_{s}" for s in re.search(r'status in \(([^)]*)\)', report).group(1).replace('"', "").replace(" ", "").split(",")}
    return {
        "push item error codes": set(ERROR_CODES),
        "issue codes": set(get_args(IssueCode)),
        "exclusion codes": set(models.EXCLUSION_CODES),
        "event codes (check_failed:<reason> is described by its reason)": {c.removeprefix("check_failed:") for c in models.EVENT_CODES},
        "statuses": {*models.PUSH_RUN_STATUSES, *models.CYCLE_STATUSES, *models.PUSH_ITEM_STATUSES},
        "cycle reasons": set(models.CYCLE_REASONS),
        "push errors": set(models.PUSH_ERROR_CODES),
        "pre-flight findings": findings,
    }


def test_every_emitted_code_has_a_catalog_entry():
    missing = {what: sorted(codes - set(CATALOG)) for what, codes in emitted().items() if codes - set(CATALOG)}
    assert not missing, missing


def test_the_code_sets_match_the_source_that_raises_them():
    assert set(re.findall(r'PushError\(\d+, "(\w+)"', src("sync/engine.py"))) <= set(models.PUSH_ERROR_CODES)
    ends = set(re.findall(r'_End\("\w+", "(\w+)"', src("scheduler.py")))
    assert ends <= {*models.CYCLE_REASONS, *models.PUSH_ERROR_CODES} and {f"push_{s}" for s in ("done_with_failures", "failed", "interrupted", "cancelled")} <= set(models.CYCLE_REASONS)
    assert set(re.findall(r'_interrupt_stale\("(\w+)"\)', src("scheduler.py"))) <= set(models.CYCLE_REASONS)
    assert set(get_args(IssueCode)) - set(models.EXCLUSION_ISSUE_CODES) == NOT_EXCLUSIONS  # a new issue code must be classified: excluding or not


def test_entries_are_complete_and_unknown_codes_fall_back():
    assert all(v["title"] and v["explanation"] and v["next_step"] for v in CATALOG.values())
    assert describe("check_failed:mass_requeue") == CATALOG["mass_requeue"] and describe("NoSuchCode") == UNKNOWN
