"""Stage B3 (run log), offline: synthetic data only. run_event (phase events), push_exclusion (what a plan leaves out), push_item.action, retention, migration 0011."""
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.exc import IntegrityError

from alembic import command
from app import runlog
from app.db import session_scope
from app.models import EXCLUSION_ISSUE_CODES, PushExclusion, PushItem, PushRun, RunEvent, SyncCycle
from app.reasons import CATALOG
from app.sync import engine

from .conftest import HEADERS, admin_engine, alembic_cfg, create_database
from .test_auth import add_user, bearer, fast_hash, login, token_of  # noqa: F401 (fast_hash: autouse fixture)
from .test_push import finish, go, items, mk, resume
from .test_push_orders import RECENT, Fake13, po, po_line, so_line
from .test_push_stage10 import ROWS10, Fake10, dry, run_all, set_line
from .test_scheduler import UTC, H, products

ALL6 = tuple(engine.STEPS)


@pytest.fixture
def h(clean_db, tmp_path):
    return H(clean_db, tmp_path)


def codes(**where: Any) -> list[str]:
    with session_scope() as s:
        return list(s.scalars(select(RunEvent.code).filter_by(**where).order_by(RunEvent.id)))


def exclusions(run_id: Any) -> Counter:
    with session_scope() as s:
        return Counter(s.execute(select(PushExclusion.entity, PushExclusion.code).where(PushExclusion.run_id == run_id)).tuples().all())


def no_names_stored() -> None:
    """Nothing the run log holds contains a synthetic name/description (the QuickBooks fixtures carry `SENTINEL-` in every free-text field) and every code has a catalog entry."""
    with session_scope() as s:
        blob = " ".join(s.execute(text("SELECT t::text FROM run_event t UNION ALL SELECT t::text FROM push_exclusion t UNION ALL SELECT t::text FROM push_item t")).scalars())
        used = set(s.scalars(select(RunEvent.code))) | set(s.scalars(select(PushExclusion.code))) | {c for c in s.scalars(select(PushItem.error_code)) if c}
    assert "SENTINEL" not in blob and blob
    assert {c.removeprefix("check_failed:") for c in used} <= set(CATALOG)


# ---- the cycle's phase events --------------------------------------------------------------------

def test_a_full_cycle_writes_its_events_in_order(h):
    h.baseline()
    products(h, 2)
    row = h.cycle()
    assert row.status == "done"
    assert codes(cycle_id=row.id) == ["cycle_started", "reads_queued", "waiting_for_web_connector", "reads_done", "snapshot_fresh", "dry_run_planned", "real_run_started", "cycle_finished"]
    with session_scope() as s:
        ev = {e.code: e for e in s.scalars(select(RunEvent).where(RunEvent.cycle_id == row.id))}
    assert ev["cycle_started"].actor == "scheduler" and ev["reads_queued"].counts == {"queued": 7, "reused": 0} and ev["dry_run_planned"].counts["planned_calls"] == 2
    assert ev["real_run_started"].run_id == row.push_run_id and ev["cycle_finished"].level == "info"
    run = [c for c in codes(run_id=row.push_run_id) if c != "real_run_started"]  # the run's own events: started, a start/finish pair per step, finished
    assert run[0] == "run_started" and run[-1] == "run_finished" and run.count("step_started") == run.count("step_finished") == 6
    with session_scope() as s:
        steps = [e.entity for e in s.scalars(select(RunEvent).where(RunEvent.run_id == row.push_run_id, RunEvent.code == "step_finished").order_by(RunEvent.id))]
        fin = s.scalars(select(RunEvent).where(RunEvent.run_id == row.push_run_id, RunEvent.code == "run_finished")).one()
    assert steps == ["supplier", "product", "bom_edge", "customer", "sales_order_line", "purchase_order"] and fin.counts == {"done": 2}
    no_names_stored()


def test_the_manual_actor_and_a_failed_check_are_recorded(h):
    cid, conn = h.sch._begin("manual")  # no baseline yet: the sync gate refuses
    h.sch._execute(cid, conn, "alice")
    assert codes(cycle_id=cid) == ["cycle_started", "reads_queued", "waiting_for_web_connector", "reads_done", "snapshot_fresh", "check_failed:no_completed_initial_run", "cycle_finished"]
    with session_scope() as s:
        first = s.scalars(select(RunEvent).where(RunEvent.cycle_id == cid).order_by(RunEvent.id)).first()
        fails = s.scalars(select(RunEvent).where(RunEvent.code.like("check_failed:%"))).one()
        last = s.scalars(select(RunEvent).where(RunEvent.cycle_id == cid, RunEvent.code == "cycle_finished")).one()
    assert first.actor == "alice" and fails.level == "warning" and last.level == "warning"


def test_waiting_for_the_web_connector_is_logged_at_most_once_per_15_minutes(h):
    h.baseline()
    start = datetime(2026, 11, 18, 1, 0, tzinfo=UTC)
    h.on_poll = lambda h: h.connect() if h.clock.t >= start + timedelta(hours=2) else None
    row = h.cycle()
    assert row.status == "done" and codes(cycle_id=row.id).count("waiting_for_web_connector") == 8  # t = 0, 15, ... 105 minutes


def test_a_read_timeout_is_a_check_failure(h):
    h.baseline()
    h.on_poll = lambda h: None
    row = h.cycle()
    assert row.reason == "read_timeout" and "check_failed:read_timeout" in codes(cycle_id=row.id)


# ---- exclusions ----------------------------------------------------------------------------------

def _rows(blocking: bool) -> list[tuple[str, str, dict]]:
    """Synthetic QuickBooks data that triggers every kind of exclusion. blocking=False keeps the pre-flight verdict ok (a real run is possible)."""
    rows = [
        ("Vendor", "V1", {"ListID": "V1", "Name": "SUPA", "CompanyName": "SENTINEL-co-a"}),
        ("Vendor", "V2", {"ListID": "V2", "Name": "SUPOFF", "CompanyName": "SENTINEL-co-off", "IsActive": "false"}),  # inactive_supplier
        ("ItemInventory", "I1", {"ListID": "I1", "Name": "P1", "SalesDesc": "SENTINEL-d1", "QuantityOnHand": "5", "PrefVendorRef": {"ListID": "V1"}}),
        ("ItemInventory", "I2", {"ListID": "I2", "Name": "P2", "SalesDesc": "SENTINEL-d2", "QuantityOnHand": "1", "PrefVendorRef": {"ListID": "V2"}}),  # unresolved_vendor
        ("ItemInventory", "I7", {"ListID": "I7", "Name": "P7", "SalesDesc": "SENTINEL-d7", "QuantityOnHand": "1"}),
        ("ItemInventoryAssembly", "A1", {"ListID": "A1", "Name": "ASM1", "SalesDesc": "SENTINEL-a1", "ItemInventoryAssemblyLine": [
            {"ItemInventoryRef": {"ListID": "I7"}, "Quantity": "1"}, {"ItemInventoryRef": {"ListID": "X9"}, "Quantity": "1"}]}),  # bom_component_unresolved
        ("Customer", "C1", {"ListID": "C1", "FullName": "SENTINEL-cust-1"}),
        ("Customer", "C2", {"ListID": "C2", "FullName": "SENTINEL-cust-2", "IsActive": "false"}),  # inactive_customer
        ("SalesOrder", "T1", {"TxnID": "T1", "TxnDate": RECENT, "CustomerRef": {"ListID": "C1"}, "IsManuallyClosed": "false", "SalesOrderLineRet": [
            so_line("L1", "I1", "5"), so_line("L2", "X1", "1"), so_line("L3", "I1", "0")], "SalesOrderLineGroupRet": {"TxnLineID": "G", "SalesOrderLineRet": [so_line("L4", "I1", "2")]}}),
        ("SalesOrder", "T2", {"TxnID": "T2", "TxnDate": RECENT, "CustomerRef": {"ListID": "C1"}, "IsManuallyClosed": "true", "SalesOrderLineRet": [so_line("L1", "I1", "10")]}),  # closed_uninvoiced
        ("PurchaseOrder", "O1", po("O1", "V1", [po_line("1", "I1", "10", "4"), po_line("2", "X1", "1", "0")])),  # sent + lines_non_product
        ("PurchaseOrder", "O2", po("O2", "V2", [po_line("1", "I1", "1", "0")])),  # vendor_not_pushed (V2 is inactive)
        ("PurchaseOrder", "O3", po("O3", "V1", [po_line("1", "I1", "3", "3")])),  # no_lines
    ]
    if blocking:
        rows += [
            ("Vendor", "V3", {"ListID": "V3", "Name": " PAD", "CompanyName": "SENTINEL-co-pad"}),  # whitespace_key
            ("Vendor", "V4", {"ListID": "V4", "Name": "SUPB", "CompanyName": "SENTINEL-co-b", "Phone": "x"}),
            ("ItemInventory", "I3", {"ListID": "I3", "Name": " P3", "SalesDesc": "SENTINEL-d3"}),  # whitespace_key
            ("ItemInventory", "I4", {"ListID": "I4", "Name": "dup", "SalesDesc": "SENTINEL-d4"}), ("ItemInventory", "I5", {"ListID": "I5", "Name": "DUP", "SalesDesc": "SENTINEL-d5"}),  # duplicate_key x2
            ("ItemInventory", "I6", {"ListID": "I6", "Name": "P6", "SalesDesc": "SENTINEL-d6", "QuantityOnHand": "abc"}),  # parse_error
            ("ItemInventoryAssembly", "A2", {"ListID": "A2", "Name": "ASM2", "SalesDesc": "SENTINEL-a2", "ItemInventoryAssemblyLine": [{"ItemInventoryRef": {"ListID": "A3"}, "Quantity": "1"}]}),
            ("ItemInventoryAssembly", "A3", {"ListID": "A3", "Name": "ASM3", "SalesDesc": "SENTINEL-a3", "ItemInventoryAssemblyLine": [{"ItemInventoryRef": {"ListID": "A2"}, "Quantity": "1"}]}),  # bom_cycle x2
            ("ItemInventoryAssembly", "A4", {"ListID": "A4", "Name": "ASM4", "SalesDesc": "SENTINEL-a4", "ItemInventoryAssemblyLine": [{"ItemInventoryRef": {"ListID": "I3"}, "Quantity": "1"}]}),  # bom_component_excluded
        ]
    return rows


def _preflight_issue_counts(env) -> Counter:
    """What the pre-flight report counts per exclusion issue code, mapped to the push entities."""
    r = env.client.post("/api/v1/preflight", json={"stocktrim": False}, headers=HEADERS).json()
    ent = {"Vendor": "supplier", "Customer": "customer", "bom": "bom_edge"}
    out: Counter = Counter()
    for k, g in r["quickbooks"]["issues"].items():
        e, c = k.split(".")
        if c in EXCLUSION_ISSUE_CODES:
            out[(ent.get(e, "product" if e.startswith("ItemInventory") else e), c)] += g["count"]
    return out


def _order_stats(plan: dict) -> Counter:
    so, p = plan["sales_orders"], plan["purchase_orders"]
    return Counter({("sales_order_line", c): so[c] for c in ("non_product", "group", "zero_quantity", "closed_uninvoiced") if so.get(c)}
                   | {("purchase_order", c): p[c] for c in ("vendor_not_pushed", "no_lines", "lines_non_product") if p.get(c)})


def test_a_dry_run_records_every_exclusion_and_the_counts_match_the_report(clean_db, tmp_path, caplog):
    env = mk(clean_db, tmp_path, Fake13(), rows=_rows(blocking=True))
    d = dry(env, ALL6)
    got = exclusions(d["run_id"])
    assert got == _preflight_issue_counts(env) + _order_stats(d["plan"]) and got[("product", "duplicate_key")] == 2 and got[("bom_edge", "bom_cycle")] == 2
    assert {c for _, c in got} >= {"whitespace_key", "parse_error", "inactive_supplier", "inactive_customer", "unresolved_vendor", "bom_cycle", "bom_component_excluded",
                                   "bom_component_unresolved", "non_product", "group", "zero_quantity", "closed_uninvoiced", "vendor_not_pushed", "no_lines", "lines_non_product"}
    with session_scope() as s:
        ws = s.scalars(select(PushExclusion).where(PushExclusion.run_id == d["run_id"], PushExclusion.code == "whitespace_key", PushExclusion.entity == "supplier")).one()
        so = s.scalars(select(PushExclusion).where(PushExclusion.run_id == d["run_id"], PushExclusion.code == "closed_uninvoiced")).one()
    assert (ws.key, ws.ref) == (" PAD", "V3") and (so.key, so.ref) == ("T2:L1", "T2")  # business key + QuickBooks ID
    assert items() == [] and "SENTINEL" not in caplog.text and codes(run_id=d["run_id"]) == ["run_finished"]
    no_names_stored()


def test_a_real_run_records_the_same_exclusions_and_resume_is_unaffected(clean_db, tmp_path, caplog):
    fail = {"on": True}
    fake = Fake13(on_product=lambda n, b: httpx.Response(400, json={"errors": {"Other": ["SENTINEL-msg"]}}) if fail["on"] and b["productCodeReadable"] == "P7" else None)
    env = mk(clean_db, tmp_path, fake, rows=_rows(blocking=False))
    d = dry(env, ALL6)
    want = exclusions(d["run_id"])
    assert want == _preflight_issue_counts(env) + _order_stats(d["plan"]) and sum(want.values()) >= 9
    out = finish(env, go(env, ALL6).json()["run_id"])
    assert out["status"] == "done_with_failures" and exclusions(out["run_id"]) == want
    with session_scope() as s:
        fp = s.get(PushRun, uuid.UUID(out["run_id"])).options["plan_fingerprint"]
    assert fp == d["plan_fingerprint"]  # exclusions are not part of the plan fingerprint
    fail["on"] = False
    resume(env, out["run_id"])
    assert finish(env, out["run_id"])["status"] == "done" and exclusions(out["run_id"]) == want  # not duplicated, not lost
    assert "SENTINEL" not in caplog.text
    no_names_stored()


# ---- item actions --------------------------------------------------------------------------------

def test_items_carry_upsert_or_delete_and_an_unchanged_sync_plans_no_calls(clean_db, tmp_path):
    env = mk(clean_db, tmp_path, Fake10(), rows=ROWS10)
    assert run_all(env)["status"] == "done"
    assert {i.action for i in items()} == {"upsert"} and all(i.created_at for i in items())
    assert dry(env, ("suppliers", "products", "bom", "customers"))["planned_calls"] == 0  # the unchanged sync still plans nothing
    set_line("A1", "I2", None)
    out = finish(env, go(env, ("bom",)).json()["run_id"])
    assert out["status"] == "done"
    assert [(i.key, i.action) for i in items("bom_edge") if i.run_id == uuid.UUID(out["run_id"])] == [('["ASM1", "P2"]', "delete")]


# ---- who did it ----------------------------------------------------------------------------------

def test_the_actor_is_the_username_or_cli(clean_db, tmp_path):
    env = mk(clean_db, tmp_path, Fake10(), rows=ROWS10)
    add_user("alice")
    tok = token_of(login(env.client))
    r = env.client.post("/api/v1/push", json={"steps": ["suppliers"]}, headers=bearer(tok))
    assert r.status_code == 200 and env.client.post("/api/v1/push", json={"steps": ["suppliers"]}, headers=HEADERS).status_code == 200
    assert env.client.post("/api/v1/preflight", json={"stocktrim": False}, headers=bearer(tok)).status_code == 200
    with session_scope() as s:
        by = [e.actor for e in s.scalars(select(RunEvent).where(RunEvent.code == "run_finished").order_by(RunEvent.id))]
        pre = s.scalars(select(RunEvent).where(RunEvent.code == "preflight_requested")).one()
    assert by == ["alice", "cli"] and pre.actor == "alice"
    d = dry(env, ("suppliers",))
    run_id = env.client.post("/api/v1/push", json={"steps": ["suppliers"], "dry_run": False, "preflight_fingerprint": d["preflight_fingerprint"], "plan_fingerprint": d["plan_fingerprint"]},
                             headers=bearer(tok)).json()["run_id"]
    finish(env, run_id)
    with session_scope() as s:
        assert s.get(PushRun, uuid.UUID(run_id)).requested_by == "alice"
    assert [e for e in codes(run_id=run_id)][0] == "run_started"


# ---- retention -----------------------------------------------------------------------------------

def _run(target: str, age_days: int, status: str = "done", dry_run: bool = False, pending: bool = False) -> uuid.UUID:
    started = datetime.now(timezone.utc) - timedelta(days=age_days)
    with session_scope() as s:
        run = PushRun(target=target, started_at=started, status=status, options={"steps": ["products"], "dry_run": dry_run, "mode": "initial"}, finished_at=started)
        s.add(run)
        s.flush()
        s.add(PushItem(run_id=run.id, entity="product", key="K", status="pending" if pending else "done"))
        s.add(PushExclusion(run_id=run.id, entity="product", key="K", code="whitespace_key"))
        return run.id


def test_prune_deletes_old_history_but_keeps_what_is_needed(make_env):
    env = make_env()
    old_dry, new_dry = _run("T", 40, dry_run=True), _run("T", 10, dry_run=True)
    old_real, baseline = _run("T", 200), _run("T", 100)  # `baseline` is the latest COMPLETED real run: the sync gate needs it
    resumable, abandoned = _run("T", 250, "failed", pending=True), _run("T", 250, "failed")
    latest = _run("T", 1, "done_with_failures")
    only = _run("U", 300)  # the only (so latest) run of another target
    with session_scope() as s:
        cyc = SyncCycle(trigger="schedule", status="done")
        s.add(cyc)
        s.flush()
        s.add_all([RunEvent(code="cycle_started", cycle_id=cyc.id, at=datetime.now(timezone.utc) - timedelta(days=100)), RunEvent(code="cycle_finished", cycle_id=cyc.id),
                   RunEvent(code="run_finished", run_id=old_dry), RunEvent(code="run_finished", run_id=latest)])
    assert runlog.prune(env.settings) == {"run_events": 1, "push_runs": 3, "push_exclusions": 0}
    with session_scope() as s:
        alive = set(s.scalars(select(PushRun.id)))
        assert alive == {new_dry, baseline, resumable, latest, only}  # old_dry, old_real and abandoned are gone
        assert {old_real, abandoned, old_dry}.isdisjoint(alive)
        assert s.scalar(text("SELECT count(*) FROM push_item")) == 5 and s.scalar(text("SELECT count(*) FROM push_exclusion")) == 5  # cascaded
        assert sorted(s.scalars(select(RunEvent.code))) == ["cycle_finished", "run_finished"]  # the 100-day-old event and the deleted run's event are gone
    assert runlog.prune(env.settings) == {"run_events": 0, "push_runs": 0, "push_exclusions": 0}


def test_prune_keeps_exclusions_only_for_the_newest_runs_and_resumable_ones(make_env):
    env = make_env()
    runs = [_run("V", d, dry_run=bool(d % 2)) for d in (1, 2, 3, 4, 5)]  # newest first: dry-runs and real runs count together
    resumable = _run("V", 20, "failed", pending=True)
    other = _run("W", 2)
    assert runlog.prune(env.settings) == {"run_events": 0, "push_runs": 0, "push_exclusions": 2}  # the runs 4 and 5 days old lose theirs; the 20-day-old resumable keeps them
    with session_scope() as s:
        have = set(s.scalars(select(PushExclusion.run_id)))
        assert have == {*runs[:3], resumable, other}
        assert s.scalar(text("SELECT count(*) FROM push_run")) == 7 and s.scalar(text("SELECT count(*) FROM push_item")) == 7  # run rows and items untouched
    assert runlog.prune(env.settings)["push_exclusions"] == 0


def test_the_prune_command_prints_counts(make_env, monkeypatch, capsys):
    from app import cli, config, db  # noqa: PLC0415
    env = make_env()
    _run("T", 40, dry_run=True)
    _run("T", 1)
    monkeypatch.setattr(config, "Settings", lambda: env.settings)
    monkeypatch.setattr(db, "init_engine", lambda url: None)  # the test engine is already up
    cli.main(["prune"])
    assert capsys.readouterr().out.strip() == "{'run_events': 0, 'push_runs': 1, 'push_exclusions': 0}"


# ---- migration 0011 ------------------------------------------------------------------------------

def test_migration_0011_upgrades_data_checks_and_downgrades(db_url):
    name = f"qb_scratch_{uuid.uuid4().hex[:8]}"
    create_database(db_url, name)
    url = create_engine(db_url).url.set(database=name).render_as_string(hide_password=False)
    eng = create_engine(url)
    rid = uuid.uuid4()
    try:
        command.upgrade(alembic_cfg(url), "0010")
        with eng.begin() as c:
            c.execute(text("INSERT INTO push_run (id, target, started_at, status, options) VALUES (:i, 'T', now(), 'done', '{}')"), {"i": rid})
            c.execute(text("INSERT INTO push_item (run_id, entity, key, status, attempts, updated_at) VALUES (:r, 'product', 'K', 'done', 1, now())"), {"r": rid})
        command.upgrade(alembic_cfg(url), "head")
        with eng.begin() as c:
            assert c.execute(text("SELECT action IS NULL AND created_at IS NOT NULL FROM push_item")).scalar_one()  # old rows: no action, created_at = now()
            c.execute(text("INSERT INTO push_exclusion (run_id, entity, key, code) VALUES (:r, 'product', 'K', 'whitespace_key')"), {"r": rid})
            c.execute(text("INSERT INTO run_event (run_id, level, code, counts) VALUES (:r, 'info', 'run_finished', '{\"done\": 1}')"), {"r": rid})
        bad = ["INSERT INTO push_exclusion (run_id, entity, key, code) VALUES ('%s', 'product', 'K', 'whitespace_key')" % rid,  # unique (run, entity, key, code)
               "INSERT INTO push_exclusion (run_id, entity, key, code) VALUES ('%s', 'product', 'K2', 'bogus')" % rid,
               "INSERT INTO run_event (level, code) VALUES ('info', 'bogus')", "INSERT INTO run_event (level, code) VALUES ('fatal', 'run_finished')",
               "INSERT INTO run_event (code, level, run_id) VALUES ('run_finished', 'info', gen_random_uuid())",
               "UPDATE push_item SET action = 'bogus'"]
        for sql in bad:
            with pytest.raises(IntegrityError), eng.begin() as c:
                c.execute(text(sql))
        with eng.begin() as c:
            c.execute(text("INSERT INTO run_event (level, code) VALUES ('info', :c)"), {"c": "check_failed:mass_requeue"})
            c.execute(text("DELETE FROM push_run"))  # cascades to items, exclusions and the run's events
            assert [c.execute(text(f"SELECT count(*) FROM {t}")).scalar_one() for t in ("push_item", "push_exclusion", "run_event")] == [0, 0, 1]
        command.downgrade(alembic_cfg(url), "0010")
        insp = inspect(eng)
        assert not {"run_event", "push_exclusion"} & set(insp.get_table_names()) and not {"action", "created_at"} & {c["name"] for c in insp.get_columns("push_item")}
        command.upgrade(alembic_cfg(url), "head")
    finally:
        eng.dispose()
        with admin_engine(db_url).connect() as c:
            c.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
