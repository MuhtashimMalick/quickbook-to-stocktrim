import json

from .conftest import PASSWORD, QB


def test_jsonl_log(make_env, tmp_path):
    env = make_env(log_dir=tmp_path / "lg")
    env.queue("customers")
    res = env.connector(http=True).run_update(QB())
    from app.main import build_app
    from app.qbwc_service import logger
    build_app(env.settings)  # rebuilding must not stack handlers
    assert sum(type(h).__name__ == "_DirHandler" for h in logger.handlers) == 1
    raw = (tmp_path / "lg" / "qbwc.jsonl").read_text(encoding="utf-8")
    lines = [json.loads(x) for x in raw.splitlines()]
    events = [l["event"] for l in lines if l["direction"] == "in"]
    order = ["clientVersion", "authenticate", "sendRequestXML", "receiveResponseXML", "closeConnection"]
    idx = [events.index(e) for e in order]
    assert idx == sorted(idx)
    assert all(l["ts"].endswith("+00:00") and l["direction"] in ("in", "out") for l in lines)
    assert any(l.get("root") for l in lines)
    assert PASSWORD not in raw and res.ticket not in raw and "<QBXML" not in raw
