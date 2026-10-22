import logging

import pytest

from app.tasks import READ_JOB_TYPES

from .conftest import QB, make_records, rs_error

# job type -> [(QB entity, paginated)]
ENTITIES = {
    "customers": [("Customer", True)],
    "vendors": [("Vendor", True)],
    "items": [("ItemInventory", True)],
    "assemblies": [("ItemInventoryAssembly", True)],
    "sites": [("InventorySite", False), ("ItemSites", True)],
    "sales_orders": [("SalesOrder", True)],
    "purchase_orders": [("PurchaseOrder", True)],
}


def seen_for(qb: QB, entity: str) -> list[str]:
    return [r for r in qb.seen if f"<{entity}QueryRq" in r]


def test_all_read_types_covered():
    assert list(ENTITIES) == READ_JOB_TYPES


@pytest.mark.parametrize("job_type", READ_JOB_TYPES)
def test_pagination_250_records(env, job_type):
    qb = QB(entities={e: make_records(e, 250) for e, _ in ENTITIES[job_type]})
    job_id = env.queue(job_type).json()["id"]
    res = env.run(qb)
    assert res.close_message == "OK"
    job = env.job(job_id)
    assert job["status"] == "done", job
    for entity, paginated in ENTITIES[job_type]:
        reqs = seen_for(qb, entity)
        if paginated:
            assert len(reqs) == 3
            assert sum('iterator="Start"' in r for r in reqs) == 1
            assert sum('iterator="Continue"' in r for r in reqs) == 2
            assert all("<MaxReturned>100</MaxReturned>" in r for r in reqs)
        else:
            assert len(reqs) == 1 and "iterator" not in reqs[0]
        assert len(env.records(entity)) == 250
        assert job["result"]["counts"][entity] == 250


@pytest.mark.parametrize("job_type", READ_JOB_TYPES)
def test_empty_result_is_done(env, job_type):
    job_id = env.queue(job_type).json()["id"]
    assert env.run(QB()).close_message == "OK"
    job = env.job(job_id)
    assert job["status"] == "done"
    assert all(env.records(e) == [] for e, _ in ENTITIES[job_type])
    assert any(row["status_code"] == 1 for row in env.get("/log").json())


@pytest.mark.parametrize("job_type", READ_JOB_TYPES)
def test_unsupported_then_next_job_runs(env, job_type):
    first = env.queue(job_type).json()["id"]
    second = env.queue("customers").json()["id"]
    qb = QB(entities={"Customer": make_records("Customer", 2)}, supported={"Customer"})
    res = env.run(qb)
    assert res.close_message == "OK"
    if job_type == "customers":
        assert env.job(first)["status"] == "done"
    else:
        j = env.job(first)
        assert j["status"] == "unsupported" and "[3100]" in j["status_message"]
    assert env.job(second)["status"] == "done"


def test_unsupported_all_entities(env):
    ids = [env.queue(t).json()["id"] for t in READ_JOB_TYPES]
    env.run(QB(supported=set()))
    assert [env.job(i)["status"] for i in ids] == ["unsupported"] * 7


def test_3250_is_unsupported_and_other_code_is_error_and_session_continues(env):
    sites, customers, vendors = (env.queue(t).json()["id"] for t in ("sites", "customers", "vendors"))
    qb = QB(entities={"Vendor": make_records("Vendor", 2)})

    def responder(req: str) -> str:
        if "<InventorySiteQueryRq" in req:
            return rs_error("InventorySiteQueryRs", 3250)
        if "<CustomerQueryRq" in req:
            return rs_error("CustomerQueryRs", 3120)
        return qb(req)

    assert env.run(responder).close_message == "OK"
    assert env.job(sites)["status"] == "unsupported"
    j = env.job(customers)
    assert j["status"] == "error" and "[3120]" in j["status_message"]
    assert env.job(vendors)["status"] == "done"


@pytest.mark.parametrize("version,expected", [((17, 0), "16.0"), ((13, 0), "13.0"), ((0, 0), "13.0"), ((16, 0), "16.0"), ((14, 1), "14.1")])
def test_version_negotiation(env, caplog, version, expected):
    caplog.set_level(logging.INFO)
    env.queue("customers")
    res = env.run(QB(entities={"Customer": make_records("Customer", 1)}), version=version)
    assert res.requests and all(f'<?qbxml version="{expected}"?>' in r for r in res.requests)
    assert f"using qbXML {expected}" in caplog.text


@pytest.mark.parametrize("job_type,entity", [("sales_orders", "SalesOrder"), ("purchase_orders", "PurchaseOrder")])
def test_include_line_items_after_max_returned(env, job_type, entity):
    env.queue(job_type)
    qb = QB(entities={entity: make_records(entity, 1)})
    env.run(qb)
    assert "<MaxReturned>100</MaxReturned><IncludeLineItems>true</IncludeLineItems>" in qb.seen[0]


def test_assembly_tree_multi_level(env):
    ref = lambda i, n: {"ItemInventoryRef": {"ListID": i, "FullName": n}}  # noqa: E731
    assemblies = [
        {"ListID": "A", "EditSequence": "1", "FullName": "A",
         "ItemInventoryAssemblyLine": [{**ref("B", "B"), "Quantity": "2"}, {**ref("Y", "Y"), "Quantity": "3"}]},
        {"ListID": "B", "EditSequence": "1", "FullName": "B",
         "ItemInventoryAssemblyLine": {**ref("X", "X"), "Quantity": "4"}},  # single line -> dict
    ]
    job_id = env.queue("assemblies").json()["id"]
    env.run(QB(entities={"ItemInventoryAssembly": assemblies}))
    job = env.job(job_id)
    assert job["status"] == "done"
    (root,) = job["result"]["tree"]
    assert root["full_name"] == "A"
    b, y = root["components"]
    assert (b["full_name"], b["quantity"], b["is_assembly"]) == ("B", "2", True)
    assert [(c["full_name"], c["quantity"]) for c in b["components"]] == [("X", "4")]
    assert (y["quantity"], y["is_assembly"], y["components"]) == ("3", False, [])
    log = "\n".join(r["message"] for r in env.get("/log").json())
    assert "A\n  - 2 x B [assembly]\n      - 4 x X\n  - 3 x Y" in log


def test_sites_unsupported_when_only_item_sites_supported(env):
    job_id = env.queue("sites").json()["id"]
    qb = QB(entities={"ItemSites": make_records("ItemSites", 1)}, supported={"ItemSites"})
    res = env.run(qb)
    assert res.close_message == "OK"
    j = env.job(job_id)
    assert j["status"] == "unsupported" and j["status_message"]
    assert "iterator" not in qb.seen[0]


def test_fixtures_saved_over_http(env):
    env.queue("customers")
    env.run(QB(entities={"Customer": make_records("Customer", 2)}), http=True)
    fx = env.settings.fixtures_dir
    assert "<CustomerQueryRq" in (fx / "Customer_rq.xml").read_text()
    assert "<CustomerRet>" in (fx / "Customer_rs.xml").read_text()
