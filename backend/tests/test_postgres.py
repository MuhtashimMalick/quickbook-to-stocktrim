import uuid

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, inspect, text

from alembic import command
from app import jobs
from app.config import Settings
from app.db import Base, session_scope
from app.models import Job
from app.quickbooks import snapshot
from app.quickbooks import tasks as qb_tasks
from app.quickbooks.tasks import WRITE_JOB_TYPES
from app.quickbooks.tasks import base as tasks_base
from app.quickbooks.tasks.base import QuerySpec

from .conftest import QB, admin_engine, alembic_cfg, create_database, make_records, rs_error

TABLES = {"jobs", "sync_log", "qb_records", "po_link"}


def keys(env, entity):
    return sorted(r["payload"].get("ListID") or r["payload"].get("TxnID") for r in env.records(entity))


# ---- config -------------------------------------------------------------------------------------

def test_database_url_must_be_postgres():
    with pytest.raises(ValidationError, match="PostgreSQL"):
        Settings(_env_file=None, qbwc_username="u", qbwc_password="p", admin_token="t", qwc_owner_id=uuid.uuid4(),
                 qwc_file_id=uuid.uuid4(), database_url="sqlite:///x.db")


# ---- migrations ---------------------------------------------------------------------------------

def test_migration_upgrade_downgrade_on_scratch_db(db_url):
    name = f"qb_scratch_{uuid.uuid4().hex[:8]}"
    create_database(db_url, name)
    url = str(create_engine(db_url).url.set(database=name).render_as_string(hide_password=False))
    engine = create_engine(url)
    try:
        command.upgrade(alembic_cfg(url), "head")
        assert TABLES <= set(inspect(engine).get_table_names())
        command.downgrade(alembic_cfg(url), "base")
        assert set(inspect(engine).get_table_names()) == {"alembic_version"}
        command.upgrade(alembic_cfg(url), "head")
        with engine.connect() as c:  # models must equal the migration (no drift)
            assert compare_metadata(MigrationContext.configure(c), Base.metadata) == []
    finally:
        engine.dispose()
        with admin_engine(db_url).connect() as c:
            c.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))


# ---- snapshot -----------------------------------------------------------------------------------

def test_upsert_is_idempotent_and_updates(env):
    recs = make_records("Customer", 3)
    assert snapshot.upsert_records("Customer", recs, None) == 3
    recs[0]["Name"] = "renamed"
    assert snapshot.upsert_records("Customer", recs, None) == 3
    rows = snapshot.list_records(["Customer"])
    assert len(rows) == 3 and {r.payload["Name"] for r in rows} >= {"renamed"}


def test_successful_read_prunes_rows_quickbooks_no_longer_returns(env):
    snapshot.upsert_records("Customer", make_records("Customer", 5), None)  # stale: 5 rows from "earlier"
    snapshot.upsert_records("Vendor", make_records("Vendor", 2), None)  # other entity untouched
    env.queue("customers")
    env.run(QB(entities={"Customer": make_records("Customer", 250)[:2]}))
    assert keys(env, "Customer") == ["Customer-0", "Customer-1"]
    assert len(env.records("Vendor")) == 2


def test_failed_second_page_prunes_nothing(env):
    snapshot.upsert_records("Customer", make_records("Customer", 5)[3:], None)  # Customer-3, Customer-4 from an earlier read
    job_id = env.queue("customers").json()["id"]
    qb = QB(entities={"Customer": make_records("Customer", 250)})

    def responder(req: str) -> str:
        return rs_error("CustomerQueryRs", 3120) if 'iterator="Continue"' in req else qb(req)

    env.run(responder)
    assert env.job(job_id)["status"] == "error"
    assert {"Customer-3", "Customer-4"} <= set(keys(env, "Customer"))  # old rows survive the failed read


def test_empty_successful_read_clears_entity(env):
    snapshot.upsert_records("Customer", make_records("Customer", 3), None)
    job_id = env.queue("customers").json()["id"]
    env.run(QB())  # status 1: nothing found
    assert env.job(job_id)["status"] == "done"
    assert env.records("Customer") == []


def _seed_stale(env):
    snapshot.upsert_records("Customer", make_records("Customer", 250)[240:243], None)  # not in any page below


@pytest.mark.parametrize("responder", [
    pytest.param(lambda req: '<?xml version="1.0"?><QBXML><QBXMLMsgsRs></QBXMLMsgsRs></QBXML>', id="empty-response"),
    pytest.param(lambda req: rs_error("CustomerQueryRs", 3250), id="unsupported"),
])
def test_failed_first_page_prunes_nothing(env, responder):
    _seed_stale(env)
    env.queue("customers")
    env.run(responder)
    assert keys(env, "Customer") == ["Customer-240", "Customer-241", "Customer-242"]


def test_max_pages_give_up_prunes_nothing(env, monkeypatch):
    monkeypatch.setattr(tasks_base, "MAX_PAGES", 2)
    _seed_stale(env)
    job_id = env.queue("customers").json()["id"]
    env.run(QB(entities={"Customer": make_records("Customer", 250)}))
    assert "gave up" in env.job(job_id)["status_message"]
    assert {"Customer-240", "Customer-241", "Customer-242"} <= set(keys(env, "Customer"))


@pytest.mark.parametrize("entities", [{}, {"Customer": make_records("Customer", 250)[:2]}], ids=["nothing-found", "partial"])
def test_windowed_read_keeps_existing_rows(env, monkeypatch, entities):
    monkeypatch.setattr(qb_tasks.CustomersTask, "specs", [QuerySpec("Customer", full_snapshot=False)])
    _seed_stale(env)
    job_id = env.queue("customers").json()["id"]
    env.run(QB(entities=entities))
    assert env.job(job_id)["status"] == "done"
    assert {"Customer-240", "Customer-241", "Customer-242"} <= set(keys(env, "Customer"))


def test_failed_second_spec_keeps_its_old_rows_but_first_is_pruned(env):
    snapshot.upsert_records("InventorySite", make_records("InventorySite", 3), None)
    snapshot.upsert_records("ItemSites", make_records("ItemSites", 2), None)
    job_id = env.queue("sites").json()["id"]
    qb = QB(entities={"InventorySite": make_records("InventorySite", 3)[:1], "ItemSites": make_records("ItemSites", 2)})
    env.run(lambda req: rs_error("ItemSitesQueryRs", 3120) if "ItemSitesQueryRq" in req else qb(req))
    assert env.job(job_id)["status"] == "error"
    assert keys(env, "InventorySite") == ["InventorySite-0"]
    assert keys(env, "ItemSites") == ["ItemSites-0", "ItemSites-1"]


def test_test_po_survives_following_full_purchase_orders_read(make_env):
    env = make_env(writes=True)
    env.queue("create_test_po", {"vendor_list_id": "V1", "item_list_id": "I1", "quantity": "5", "rate": "2.5", "idempotency_key": "k1"})
    qb = QB()
    env.run(qb)
    before = keys(env, "PurchaseOrder")
    assert len(before) == 1
    env.queue("purchase_orders")
    env.run(qb)  # QuickBooks now returns the PO it just stored
    assert keys(env, "PurchaseOrder") == before


# ---- startup recovery ---------------------------------------------------------------------------

def _status(job_id):
    return jobs.get_job(job_id).status


def test_recover_interrupted(env):
    read, write, done, queued = (jobs.create_job(t).id for t in ("customers", "create_test_po", "vendors", "items"))
    for j in (read, write):
        jobs.mark_running(j)
    jobs.set_job_status(done, "done")
    assert jobs.recover_interrupted(WRITE_JOB_TYPES) == 2
    assert [_status(j) for j in (read, write, done, queued)] == ["error", "queued", "done", "queued"]
    assert jobs.get_job(read).status_message == "interrupted by restart" and jobs.get_job(read).finished_at
    assert jobs.get_job(write).finished_at is None
    assert jobs.recover_interrupted(WRITE_JOB_TYPES) == 0


def test_lifespan_runs_recovery(env):
    job_id = jobs.create_job("customers").id
    jobs.mark_running(job_id)
    with TestClient(env.app):
        pass
    assert _status(job_id) == "error"


def test_no_recovery_at_build_time(make_env):
    env = make_env()
    job_id = jobs.create_job("customers").id
    jobs.mark_running(job_id)
    make_env()  # building another app must not touch running jobs
    with session_scope() as s:
        assert s.get(Job, job_id).status == "running"
    assert env
