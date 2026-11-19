"""Stage B4 (read APIs of the admin UI), offline: synthetic data only, injected clock. Routes are probed through the simulated proxy with a Bearer token."""
import csv
import io
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event

from app.api.v1 import entries as entries_mod
from app.cli import main as cli_main
from app.config import Settings
from app.db import get_engine, session_scope
from app.models import EXCLUSION_CODES, Job, PushExclusion, PushItem, PushRun, QBRecord, RunEvent, SyncCycle
from app.reasons import CATALOG, UNKNOWN
from app.sync.engine import ERROR_CODES

from .conftest import HEADERS, TOKEN, Env
from .test_auth import add_user, bearer, fast_hash, login, token_of  # noqa: F401 (fast_hash: autouse fixture)
from .test_exposure import PROXY_REACHABLE, XFF, proxied  # noqa: F401 (proxied: fixture)

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
NEW_ROUTES = {("GET", "/api/v1/cycles"), ("GET", "/api/v1/cycles/{cycle_id}"), ("GET", "/api/v1/events"), ("GET", "/api/v1/push/runs"), ("GET", "/api/v1/push/runs/{run_id}/entries"),
              ("GET", "/api/v1/push/runs/{run_id}/entries.csv"), ("GET", "/api/v1/settings"), ("GET", "/api/v1/reason-codes"), ("GET", "/api/v1/overview")}
TARGET = "test|https://api.stocktrim.com|abcdef123456"


@pytest.fixture
def api(env, proxied):  # noqa: F811
    add_user()
    return proxied, bearer(token_of(login(proxied)))


def at(n: int) -> datetime:
    return T0 + timedelta(minutes=n)


def mk_run(dry: bool = True, n: int = 0, status: str = "done", **kw) -> uuid.UUID:
    rid = uuid.uuid4()
    with session_scope() as s:
        s.add(PushRun(id=rid, target=TARGET, requested_by="alice", status=status, started_at=at(n), options={"dry_run": dry, "steps": ["suppliers", "products"], "mode": "initial"}, **kw))
    return rid


def seed_entries() -> uuid.UUID:
    rid = mk_run()
    it = lambda ent, key, st, code=None, ref=None, n=0, **kw: PushItem(run_id=rid, entity=ent, key=key, status=st, error_code=code, ref=ref, updated_at=at(n), **kw)  # noqa: E731
    ex = lambda ent, key, code, ref, n=0: PushExclusion(run_id=rid, entity=ent, key=key, code=code, ref=ref, created_at=at(n))  # noqa: E731
    with session_scope() as s:
        s.add_all([
            it("supplier", "SUP-A", "failed", "rejected_400", "V1", 1, action="upsert", attempts=2, detail={"status": 400, "fields": ["Email", "Phone"]}),
            it("product", "P1", "failed", "unavailable", "I1", 2, action="upsert"),
            it("product", "P2", "skipped", "aborted", "I-GONE", 3, action="upsert"),
            it("product", "P3", "done", None, "I3", 4), it("product", "P4", "pending", None, "I4", 5),  # neither is an entry
            ex("supplier", "SUPOFF", "inactive_supplier", "V2", 6), ex("product", "100%_X", "duplicate_key", "I5", 7), ex("product", "100AX", "duplicate_key", "I6", 7),
            ex("product", "=cmd", "empty_key", "I7", 8),
            QBRecord(entity="Vendor", qb_id="V1", payload={"ListID": "V1", "Name": "SENTINEL-vendor-1"}), QBRecord(entity="ItemInventory", qb_id="I1", payload={"Name": "SENTINEL-item-1"}),
            QBRecord(entity="Vendor", qb_id="V2", payload={"Name": "SENTINEL-off", "FullName": "SENTINEL-full-off"}), QBRecord(entity="ItemInventory", qb_id="I5", payload={"Name": "SENTINEL-i5"}),
            QBRecord(entity="ItemInventory", qb_id="I7", payload={"Name": "=HYPERLINK(1)"}),
        ])
    return rid


def get(api, path, **params):
    c, tok = api
    return c.get(path, headers=tok, params=params)


# ---- auth and exposure
def test_every_new_route_needs_a_bearer_and_is_proxy_reachable(env, api):
    assert NEW_ROUTES <= PROXY_REACHABLE
    c, tok = api
    rid, cid = mk_run(), uuid.uuid4()
    with session_scope() as s:
        s.add(SyncCycle(id=cid, trigger="manual", status="done", started_at=at(0)))
    for m, p in NEW_ROUTES:
        path = p.replace("{run_id}", str(rid)).replace("{cycle_id}", str(cid))
        assert c.request(m, path).status_code == 401, path
        assert c.request(m, path, headers=HEADERS).status_code == 401, path  # X-Admin-Token is ignored once proxied
        assert c.request(m, path, headers=tok).status_code == 200, path


# ---- cycles
def test_cycles_list_cursors_and_detail(api):
    ids = [uuid.uuid4() for _ in range(5)]
    run = mk_run(dry=False, n=3)
    with session_scope() as s:
        s.add_all([SyncCycle(id=i, trigger="manual", status="done", started_at=at(n), finished_at=at(n + 1), summary={"read_jobs": {"queued": 3}}) for n, i in enumerate(ids)])
        s.add(SyncCycle(trigger="schedule", status="aborted", reason="mass_vanish", started_at=at(10), finished_at=at(11), push_run_id=run))
        s.add_all([PushItem(run_id=run, entity="product", key="K", status="done"), PushItem(run_id=run, entity="product", key="L", status="failed", error_code="unavailable")])
        s.add_all([Job(job_type="customers", status="done", created_at=at(10), finished_at=at(10)), Job(job_type="customers", status="done", created_at=at(50)), Job(job_type="create_test_po", status="done", created_at=at(10))])
    rows = get(api, "/api/v1/cycles", limit=3).json()
    assert [r["status"] for r in rows] == ["aborted", "done", "done"] and rows[1]["id"] == str(ids[4]) and rows[1]["summary"] == {"read_jobs": {"queued": 3}}
    assert [r["id"] for r in get(api, "/api/v1/cycles", before=rows[-1]["id"]).json()] == [str(i) for i in ids[2::-1]]  # by id
    assert [r["id"] for r in get(api, "/api/v1/cycles", before=at(2).isoformat()).json()] == [str(ids[1]), str(ids[0])]  # by time
    assert get(api, "/api/v1/cycles", before="nope").status_code == 422 and get(api, "/api/v1/cycles", before=str(uuid.uuid4())).status_code == 422
    assert get(api, "/api/v1/cycles", limit=101).status_code == 422
    d = get(api, f"/api/v1/cycles/{rows[0]['id']}").json()
    assert d["reason_info"]["title"] == CATALOG["mass_vanish"]["title"] and d["reason_info"]["next_step"]
    assert [(j["job_type"], j["status"]) for j in d["jobs"]] == [("customers", "done")]  # only read jobs created while the cycle ran
    assert d["push_run"]["run_id"] == str(run) and d["push_run"]["counts"] == {"product": {"done": 1, "failed": 1}} and d["wrote_to_stocktrim"] is True
    plain = get(api, f"/api/v1/cycles/{ids[0]}").json()
    assert plain["reason_info"] is None and plain["push_run"] is None and plain["wrote_to_stocktrim"] is False
    assert get(api, f"/api/v1/cycles/{uuid.uuid4()}").status_code == 404


def test_a_dry_run_push_never_counts_as_written(api):
    run = mk_run(dry=True)
    with session_scope() as s:
        s.add(SyncCycle(trigger="manual", status="dry_run_only", started_at=at(0), push_run_id=run))
        s.add(PushItem(run_id=run, entity="product", key="K", status="done"))
    assert get(api, "/api/v1/cycles").json()[0]["push_run_id"] == str(run)
    cid = get(api, "/api/v1/cycles").json()[0]["id"]
    assert get(api, f"/api/v1/cycles/{cid}").json()["wrote_to_stocktrim"] is False


# ---- events
def test_events_cursor_scoping_and_cap(api):
    c1, c2, run = uuid.uuid4(), uuid.uuid4(), mk_run()
    with session_scope() as s:
        s.add_all([SyncCycle(id=c1, trigger="manual", status="done"), SyncCycle(id=c2, trigger="manual", status="done")])
        s.flush()
        s.add_all([RunEvent(cycle_id=c1, code="cycle_started"), RunEvent(cycle_id=c2, code="cycle_started"), RunEvent(cycle_id=c1, run_id=run, code="check_failed:mass_vanish", level="warning", counts={"n": 3}),
                   *(RunEvent(cycle_id=c2, code="step_started") for _ in range(205))])
    a = get(api, "/api/v1/events", cycle_id=str(c1)).json()
    assert [e["code"] for e in a["items"]] == ["cycle_started", "check_failed:mass_vanish"] and a["items"][1]["title"] == CATALOG["mass_vanish"]["title"] and a["items"][1]["counts"] == {"n": 3}
    assert get(api, "/api/v1/events", cycle_id=str(c1), after_id=a["last_id"]).json() == {"items": [], "last_id": a["last_id"]}
    assert [e["code"] for e in get(api, "/api/v1/events", run_id=str(run)).json()["items"]] == ["check_failed:mass_vanish"]
    big = get(api, "/api/v1/events", cycle_id=str(c2), limit=200).json()
    assert len(big["items"]) == 200 and get(api, "/api/v1/events", limit=201).status_code == 422
    rest = get(api, "/api/v1/events", cycle_id=str(c2), after_id=big["last_id"], limit=200).json()
    assert len(rest["items"]) == 6 and [e["id"] for e in big["items"]] == sorted(e["id"] for e in big["items"])


# ---- runs list
def test_runs_list(api):
    a, b = mk_run(dry=True, n=1), mk_run(dry=False, n=2)
    with session_scope() as s:
        s.add_all([PushItem(run_id=a, entity="product", key="1", status="done"), PushItem(run_id=a, entity="product", key="2", status="failed")])
    r = get(api, "/api/v1/push/runs")
    rows = r.json()
    assert [x["id"] for x in rows] == [str(b), str(a)] and rows[1]["counts"] == {"done": 1, "failed": 1} and rows[0]["counts"] == {}
    assert rows[0]["target"] == "test @ api.stocktrim.com" and rows[0]["requested_by"] == "alice" and rows[0]["steps"] == ["suppliers", "products"] and rows[0]["mode"] == "initial"
    assert "abcdef123456" not in r.text
    assert [x["id"] for x in get(api, "/api/v1/push/runs", dry_run="true").json()] == [str(a)]
    assert [x["id"] for x in get(api, "/api/v1/push/runs", before=str(b)).json()] == [str(a)]
    assert get(api, f"/api/v1/push/runs/{a}").status_code == 200  # the existing progress route is untouched


# ---- entries
def test_entries_union_filters_counts_and_labels(api):
    rid = seed_entries()
    base = f"/api/v1/push/runs/{rid}/entries"
    d = get(api, base).json()
    assert d["total"] == 7 and d["counts"] == {"failed": 2, "skipped": 1, "excluded": 4}
    assert [e["key"] for e in d["items"]] == ["=cmd", "100AX", "100%_X", "SUPOFF", "P2", "P1", "SUP-A"]  # updated_at desc, then source/id: stable
    by = {e["key"]: e for e in d["items"]}
    assert (by["SUP-A"]["source"], by["SUP-A"]["outcome"], by["SUP-A"]["http_status"], by["SUP-A"]["fields"], by["SUP-A"]["attempts"], by["SUP-A"]["action"]) == ("item", "failed", 400, ["Email", "Phone"], 2, "upsert")
    assert by["SUP-A"]["qb_label"] == "SENTINEL-vendor-1" and by["SUP-A"]["reason"]["title"] == CATALOG["rejected_400"]["title"]
    assert by["P2"]["qb_label"] is None and by["P2"]["outcome"] == "skipped"  # gone from the snapshot
    assert (by["SUPOFF"]["source"], by["SUPOFF"]["outcome"], by["SUPOFF"]["attempts"], by["SUPOFF"]["http_status"], by["SUPOFF"]["fields"]) == ("exclusion", "excluded", None, None, [])
    assert by["SUPOFF"]["qb_label"] == "SENTINEL-full-off"  # FullName wins over Name
    f = get(api, base, outcome=["failed", "skipped"]).json()
    assert f["total"] == 3 and f["counts"] == {"failed": 2, "skipped": 1, "excluded": 4}  # counts ignore the outcome filter
    assert get(api, base, outcome="excluded", entity="supplier").json()["total"] == 1
    assert {e["key"] for e in get(api, base, code="duplicate_key").json()["items"]} == {"100%_X", "100AX"}
    p = get(api, base, limit=3, offset=2).json()
    assert [e["key"] for e in p["items"]] == ["100%_X", "SUPOFF", "P2"] and p["total"] == 7
    assert get(api, base, outcome="bogus").status_code == 422 and get(api, base, limit=201).status_code == 422 and get(api, base, q="x" * 101).status_code == 422
    assert get(api, f"/api/v1/push/runs/{uuid.uuid4()}/entries").status_code == 404


def test_entries_q_is_case_insensitive_contains_with_escaped_wildcards(api):
    base = f"/api/v1/push/runs/{seed_entries()}/entries"
    keys = lambda **p: {e["key"] for e in get(api, base, **p).json()["items"]}  # noqa: E731
    assert keys(q="sup-") == {"SUP-A"} and keys(q="V2") == {"SUPOFF"}  # key / ref
    assert keys(q="sentinel-item") == {"P1"}  # label, case-insensitive
    assert keys(q="100%") == {"100%_X"} and keys(q="%") == {"100%_X"}  # a literal %, not a wildcard
    assert keys(q="_x") == {"100%_X"} and keys(q="%_") == {"100%_X"} and keys(q="_") == {"100%_X"}  # a literal _
    assert keys(q="nothing-like-this") == set()
    assert get(api, base, q="sentinel-i", outcome="excluded").json()["total"] == 1 and get(api, base, q="sentinel-i").json()["counts"] == {"failed": 1, "skipped": 0, "excluded": 1}


def test_entries_page_resolves_labels_with_one_query(api):
    rid = seed_entries()
    seen = []
    eng = get_engine()
    hook = lambda conn, cur, stmt, *a: seen.append(stmt)  # noqa: E731
    event.listen(eng, "before_cursor_execute", hook)
    try:
        assert get(api, f"/api/v1/push/runs/{rid}/entries", limit=200).json()["total"] == 7
    finally:
        event.remove(eng, "before_cursor_execute", hook)
    assert len([q for q in seen if "FROM qb_records" in q]) == 1


def test_every_code_of_the_closed_sets_is_explained(api):
    rid = mk_run()
    with session_scope() as s:
        s.add_all([PushItem(run_id=rid, entity="product", key=f"i-{c}", status="failed", error_code=c) for c in sorted(ERROR_CODES)])
        s.add_all([PushExclusion(run_id=rid, entity="product", key=f"x-{c}", code=c) for c in EXCLUSION_CODES])
    items = get(api, f"/api/v1/push/runs/{rid}/entries", limit=200).json()["items"]
    assert len(items) == len(ERROR_CODES) + len(EXCLUSION_CODES)
    for e in items:
        assert e["reason"]["title"] and e["reason"]["explanation"] and e["reason"]["next_step"], e["code"]
        assert e["reason"] != UNKNOWN and e["reason"]["title"] == CATALOG[e["code"]]["title"], e["code"]


# ---- CSV
def test_csv_bom_quoting_injection_and_label(api):
    rid = seed_entries()
    r = get(api, f"/api/v1/push/runs/{rid}/entries.csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv") and r.headers["x-truncated"] == "false"
    assert f"entries-{str(rid)[:8]}.csv" in r.headers["content-disposition"] and r.content.startswith(b"\xef\xbb\xbfoutcome,")
    rows = list(csv.reader(io.StringIO(r.content.decode("utf-8-sig"))))
    assert rows[0][:6] == ["outcome", "entity", "action", "key", "ref", "qb_label"] and len(rows) == 8
    by = {x[3]: x for x in rows[1:]}
    assert "'=cmd" in by and by["'=cmd"][5] == "'=HYPERLINK(1)"  # key and label both neutralised
    assert by["SUP-A"][5] == "SENTINEL-vendor-1" and by["SUP-A"][9] == "Email;Phone" and by["SUP-A"][8] == "400" and by["SUP-A"][10] == "2"
    only = list(csv.reader(io.StringIO(get(api, f"/api/v1/push/runs/{rid}/entries.csv", outcome="failed").content.decode("utf-8-sig"))))
    assert len(only) == 3


def test_csv_cell_quoting_and_injection_unit():
    for bad in ("=1+1", "+1", "-1", "@SUM(A1)", "\tx", "\rx"):
        assert entries_mod._cell(bad) == "'" + bad
    assert entries_mod._cell("ok") == "ok" and entries_mod._cell(None) == "" and entries_mod._cell(400) == 400
    buf = io.StringIO(newline="")
    csv.writer(buf).writerow(["a,b", 'say "hi"', "line\nbreak"])
    assert list(csv.reader(io.StringIO(buf.getvalue()))) == [["a,b", 'say "hi"', "line\nbreak"]]


def test_csv_is_capped_and_streams_in_chunks(api, monkeypatch):
    rid = mk_run()
    with session_scope() as s:
        s.add_all([PushExclusion(run_id=rid, entity="product", key=f"k{i:04d}", code="empty_key", created_at=at(i)) for i in range(2500)])
    r = get(api, f"/api/v1/push/runs/{rid}/entries.csv")
    assert len(r.content.decode("utf-8-sig").splitlines()) == 2501 and r.headers["x-truncated"] == "false"  # more than one 1000-row chunk
    monkeypatch.setattr(entries_mod, "MAX_CSV_ROWS", 1200)
    r = get(api, f"/api/v1/push/runs/{rid}/entries.csv")
    assert len(r.content.decode("utf-8-sig").splitlines()) == 1201 and r.headers["x-truncated"] == "true"
    monkeypatch.undo()
    assert entries_mod.MAX_CSV_ROWS == 200_000


# ---- reason codes, settings, overview, leakage
def test_reason_codes_is_the_whole_catalog(api):
    d = get(api, "/api/v1/reason-codes").json()
    assert d == {k: v for k, v in CATALOG.items()} and all(set(v) == {"title", "explanation", "next_step"} for v in d.values())


def test_settings_has_no_secrets(make_env, tmp_path, clean_db):
    from fastapi.testclient import TestClient  # noqa: PLC0415
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware  # noqa: PLC0415

    st = Settings(_env_file=None, qbwc_username="qb", qbwc_password="qbwc-pw-SECRET1", admin_token=TOKEN, jwt_secret="jwt-SECRET-" + "j" * 30, qwc_owner_id=uuid.uuid4(), qwc_file_id=uuid.uuid4(),
                  database_url=clean_db, fixtures_dir=tmp_path / "fx", log_dir=tmp_path / "logs", stocktrim_base_url="https://api.example.test/v1", stocktrim_auth_id="st-auth-ID-SECRET2",
                  stocktrim_auth_signature="st-sig-SECRET3", alert_webhook_url="https://hooks.example.test/T000/SECRET4", scheduler_read_jobs="customers,vendors")
    env = Env(st)
    c = TestClient(ProxyHeadersMiddleware(env.app, trusted_hosts="*"), base_url="https://testserver", client=("203.0.113.9", 1), headers=XFF)
    add_user()
    r = c.get("/api/v1/settings", headers=bearer(token_of(login(c))))
    d = r.json()
    for secret in ("qbwc-pw-SECRET1", TOKEN, "jwt-SECRET-", "st-auth-ID-SECRET2", "st-sig-SECRET3", "SECRET4", "hooks.example.test", "argon2", "password_hash", "/v1"):
        assert secret not in r.text, secret
    assert d["stocktrim_host"] == "api.example.test" and d["alert_webhook_configured"] is True and d["read_jobs"] == ["customers", "vendors"]
    assert d["stocktrim_writes_enabled"] is False and d["scheduler_enabled"] is False and d["environment"] == "test" and d["sync_max_planned_calls"] == 2000
    assert [(u["username"], u["is_active"]) for u in d["users"]] == [("alice", True)] and set(d["users"][0]) == {"username", "is_active", "last_login_at"}
    assert d["users"][0]["last_login_at"] is not None


def test_overview_shape_and_running_state(env, api):
    env.app.state.scheduler.clock = lambda: T0
    o = get(api, "/api/v1/overview").json()
    assert set(o) == {"database", "scheduler", "last_cycle", "last_qbwc_contact", "last_qbwc_contact_age_seconds", "jobs", "snapshot", "running_cycle", "running_push", "last_real_push_run"}
    assert o["database"] is True and o["running_cycle"] is None and o["running_push"] is None and o["last_real_push_run"] is None and o["last_cycle"] is None
    cid, run, real = uuid.uuid4(), mk_run(dry=True, n=1, status="running"), mk_run(dry=False, n=0)
    with session_scope() as s:
        s.add(SyncCycle(id=cid, trigger="manual", status="running", started_at=at(0)))
        s.add_all([Job(job_type="customers", status="queued"), Job(job_type="vendors", status="running"), QBRecord(entity="Vendor", qb_id="V1", payload={}, synced_at=at(5))])
        s.add(PushItem(run_id=real, entity="product", key="K", status="done"))
        s.flush()
        s.add_all([RunEvent(cycle_id=cid, code="cycle_started"), RunEvent(cycle_id=cid, code="reads_queued"), RunEvent(run_id=run, code="run_started")])
    o = get(api, "/api/v1/overview").json()
    assert o["running_cycle"]["id"] == str(cid) and o["running_cycle"]["phase"] == "reads_queued" and o["running_push"]["id"] == str(run) and o["running_push"]["phase"] == "run_started"
    assert o["jobs"] == {"queued": 1, "running": 1} and o["snapshot"]["Vendor"]["records"] == 1 and o["last_cycle"]["status"] == "running"
    assert o["last_real_push_run"]["run_id"] == str(real) and o["last_real_push_run"]["counts"] == {"done": 1}
    assert o["scheduler"]["next_run_at"] is None  # scheduler disabled in tests


def test_sentinel_names_leave_only_through_the_entries_label(env, api):
    rid = seed_entries()
    cid = uuid.uuid4()
    with session_scope() as s:
        s.add(SyncCycle(id=cid, trigger="manual", status="done", started_at=at(0), push_run_id=rid, summary={"read_jobs": {"queued": 1}}))
        s.flush()
        s.add(RunEvent(cycle_id=cid, run_id=rid, code="cycle_started"))
    quiet = ["/api/v1/events", "/api/v1/settings", "/api/v1/overview", "/api/v1/cycles", f"/api/v1/cycles/{cid}", "/api/v1/push/runs", f"/api/v1/push/runs/{rid}", "/api/v1/reason-codes"]
    for p in quiet:
        r = get(api, p)
        assert r.status_code == 200 and "SENTINEL" not in r.text, p
    assert "SENTINEL" in get(api, f"/api/v1/push/runs/{rid}/entries").text and "SENTINEL" in get(api, f"/api/v1/push/runs/{rid}/entries.csv").text
    assert "SENTINEL" not in json.dumps(get(api, f"/api/v1/push/runs/{rid}/entries").json()["items"][0]["reason"])


# ---- OpenAPI
def test_cli_openapi_covers_exactly_the_proxy_reachable_routes(capsys):
    cli_main(["openapi"])
    spec = json.loads(capsys.readouterr().out)
    got = {(m.upper(), p) for p, ops in spec["paths"].items() for m in ops}
    assert got == PROXY_REACHABLE


def test_new_routes_have_typed_response_models(env):
    spec = env.app.openapi()
    for m, p in NEW_ROUTES:
        schema = spec["paths"][p][m.lower()]["responses"]["200"]["content"]
        if not p.endswith(".csv"):
            assert "application/json" in schema and schema["application/json"]["schema"], p


# ---- alert webhook: one decision for the sender, the scheduler and /settings
@pytest.mark.parametrize("value,expected", [(None, False), ("", False), ("   ", False), ("\n\t", False), ("https://hooks.example.test/T0/SECRETWEBHOOK", True), ("  https://hooks.example.test/x  ", True)])
def test_alert_webhook_is_configured_only_for_a_non_blank_value(tmp_path, clean_db, value, expected):
    from fastapi.testclient import TestClient  # noqa: PLC0415
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware  # noqa: PLC0415

    from app.scheduler import send_alert  # noqa: PLC0415

    st = Settings(_env_file=None, qbwc_username="qb", qbwc_password="pw-x-1234", admin_token=TOKEN, jwt_secret="j" * 40, qwc_owner_id=uuid.uuid4(), qwc_file_id=uuid.uuid4(),
                  database_url=clean_db, fixtures_dir=tmp_path / "fx", log_dir=tmp_path / "logs", alert_webhook_url=value)
    assert (st.alert_webhook is not None) is expected and (not expected or st.alert_webhook == st.alert_webhook.strip())
    posts = []
    import httpx  # noqa: PLC0415
    send_alert(st, "failed", "crashed", transport=httpx.MockTransport(lambda r: posts.append(str(r.url)) or httpx.Response(200)))
    assert bool(posts) is expected  # an empty value sends nothing
    env = Env(st)
    c = TestClient(ProxyHeadersMiddleware(env.app, trusted_hosts="*"), base_url="https://testserver", client=("203.0.113.9", 1), headers=XFF)
    add_user()
    r = c.get("/api/v1/settings", headers=bearer(token_of(login(c))))
    assert r.json()["alert_webhook_configured"] is expected and "SECRETWEBHOOK" not in r.text and "hooks.example" not in r.text


def test_overview_works_for_the_cli_with_x_admin_token(env):
    r = env.get("/api/v1/overview")  # direct loopback + X-Admin-Token, as `python -m app.cli GET /overview`
    assert r.status_code == 200 and r.json()["database"] is True
    assert env.get("/api/v1/settings").status_code == 200
