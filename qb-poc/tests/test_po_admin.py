import logging
import re

import pytest
from qbwc_kit import soap

from app.auth import DbAuthenticator
from app.tasks import READ_JOB_TYPES

from .conftest import HEADERS, PASSWORD, QB, USER, make_records

PO = {"vendor_list_id": "V1", "item_list_id": "I1", "quantity": "5", "rate": "2.5", "idempotency_key": "k1"}


# ---- create_test_po -------------------------------------------------------

def test_po_forbidden_when_writes_disabled(env):
    assert env.queue("create_test_po", PO).status_code == 403


def test_po_created_once_with_ref_memo_and_ids(make_env):
    env = make_env(writes=True)
    job = env.queue("create_test_po", PO).json()
    assert re.fullmatch(r"ST-[0-9A-F]{8}", job["ref_number"])
    qb = QB()
    assert env.run(qb).close_message == "OK"
    done = env.job(job["id"])
    assert done["status"] == "done"
    assert done["result"]["txn_id"] and done["result"]["edit_sequence"] == "1"
    assert done["result"]["ref_number"] == job["ref_number"]
    assert "<PurchaseOrderQueryRq>" in qb.seen[0] and "<PurchaseOrderAddRq" in qb.seen[1]
    assert "<Memo>Created by integration - Review Required</Memo>" in qb.seen[1]
    assert "<Quantity>5</Quantity><Rate>2.50</Rate>" in qb.seen[1]
    assert len(qb.entities["PurchaseOrder"]) == 1
    assert len(env.records("PurchaseOrder")) == 1


def test_po_defaults_to_first_synced_vendor_item_and_409_when_none(make_env):
    env = make_env(writes=True)
    assert env.queue("create_test_po", {}).status_code == 409
    env.queue("vendors"), env.queue("items")
    env.run(QB(entities={"Vendor": make_records("Vendor", 3), "ItemInventory": make_records("ItemInventory", 3)}))
    job = env.queue("create_test_po", {}).json()
    assert job["params"]["vendor_list_id"] == "Vendor-0" and job["params"]["item_list_id"] == "ItemInventory-0"


def test_po_lost_add_response_is_not_duplicated_on_retry(make_env):
    env = make_env(writes=True)
    job = env.queue("create_test_po", PO).json()
    qb = QB()

    def lossy(req: str) -> str:
        resp = qb(req)
        return "" if "<PurchaseOrderAddRq" in req else resp

    env.run(lossy)
    failed = env.job(job["id"])
    assert failed["status"] == "error" and "lost" in failed["status_message"]
    assert len(qb.entities["PurchaseOrder"]) == 1  # QB did create it

    again = env.queue("create_test_po", PO)  # same idempotency key
    assert again.status_code == 200 and again.json()["id"] == job["id"] and again.json()["status"] == "queued"
    env.run(qb)
    done = env.job(job["id"])
    assert done["status"] == "done" and "already existed" in done["status_message"]
    assert done["result"]["txn_id"] == qb.entities["PurchaseOrder"][0]["TxnID"]
    assert len(qb.entities["PurchaseOrder"]) == 1
    assert sum("<PurchaseOrderAddRq" in r for r in qb.seen) == 1
    assert env.queue("create_test_po", PO).json()["status"] == "done"  # done -> returned unchanged


# ---- auth / session -------------------------------------------------------

def test_wrong_password_and_no_jobs(env):
    env.queue("customers")
    assert env.connector(password="nope").run_update(QB()).last_error == "nvu"
    env.run(QB())  # drains the queue
    assert env.run(QB()).last_error == "none"


def test_write_jobs_first(make_env):
    env = make_env(writes=True)
    env.queue("customers")
    env.queue("create_test_po", PO)
    env.queue("vendors")
    tasks = DbAuthenticator(env.settings).tasks_for(USER)
    assert [t.job_type for t in tasks] == ["create_test_po", "customers", "vendors"]


def test_qb_hresult_marks_running_job_error(env):
    job_id = env.queue("customers").json()["id"]
    svc = env.app.state.qbwc_service
    call = lambda m, p: svc.dispatch(soap.build_request(m, p))  # noqa: E731
    ticket = re.findall(r"<string>(.*?)</string>", call("authenticate", [("strUserName", USER), ("strPassword", PASSWORD)]))[0]
    call("sendRequestXML", [("ticket", ticket), ("strHCPResponse", ""), ("strCompanyFileName", "x"), ("qbXMLCountry", "US"), ("qbXMLMajorVers", "13"), ("qbXMLMinorVers", "0")])
    assert env.job(job_id)["status"] == "running"
    call("receiveResponseXML", [("ticket", ticket), ("response", ""), ("hresult", "0x80040400"), ("message", "parse error")])
    call("closeConnection", [("ticket", ticket)])
    job = env.job(job_id)
    assert job["status"] == "error" and "0x80040400" in job["status_message"]


def test_callbacks_logged_without_password(env, caplog):
    caplog.set_level(logging.INFO)
    env.queue("customers")
    res = env.run(QB(entities={"Customer": make_records("Customer", 1)}), http=True)
    env.connector(http=True).last_error(res.ticket)
    for method in ("authenticate", "sendRequestXML", "receiveResponseXML", "getLastError", "closeConnection"):
        assert f"QBWC -> {method}" in caplog.text, method
    assert PASSWORD not in caplog.text


# ---- admin API ------------------------------------------------------------

ROUTES = [("get", "/qwc"), ("post", "/jobs/customers"), ("post", "/jobs/all-reads"), ("get", "/jobs"),
          ("get", "/jobs/1"), ("get", "/records/customers"), ("get", "/log")]


@pytest.mark.parametrize("method,path", ROUTES)
@pytest.mark.parametrize("headers", [{}, {"X-Admin-Token": "wrong"}])
def test_admin_requires_token(env, method, path, headers):
    assert getattr(env.client, method)(path, headers=headers).status_code == 401


def test_qwc_download(env):
    r = env.get("/qwc")
    s = env.settings
    assert r.headers["content-disposition"] == 'attachment; filename="SpecialtyArchery-StockTrim.qwc"'
    assert "<AppURL>http://localhost:8000/qbwc</AppURL>" in r.text
    assert "<OwnerID>{%s}</OwnerID>" % str(s.qwc_owner_id).upper() in r.text
    assert "<FileID>{%s}</FileID>" % str(s.qwc_file_id).upper() in r.text
    assert "<RunEveryNSeconds>120</RunEveryNSeconds>" in r.text and f"<UserName>{USER}</UserName>" in r.text


def test_jobs_records_log_endpoints(env):
    assert env.queue("nonsense").status_code == 404
    jobs = env.queue("all-reads").json()
    assert [j["job_type"] for j in jobs] == READ_JOB_TYPES
    env.run(QB(entities={"Customer": make_records("Customer", 3)}))
    assert len(env.records("customers")) == len(env.records("Customer")) == 3
    assert len(env.get("/jobs").json()) == 7
    assert len(env.get("/jobs?status=done").json()) == 7
    assert env.get(f"/log?job_id={jobs[0]['id']}").json()
    assert env.get("/jobs/999").status_code == 404
