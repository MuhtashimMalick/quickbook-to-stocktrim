import logging
import threading
import time

import httpx
import pytest

from app.config import Settings
from app.stocktrim.api import StockTrimApi
from app.stocktrim.client import StockTrimClient
from app.stocktrim.dto import ProductRequest
from app.stocktrim.limiter import RateLimiter, shared_limiter
from app.stocktrim.target import stocktrim_target


def client(url="https://a.test", rps=5.0):
    return StockTrimClient(url, "id", "sig", max_rps=rps, transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"productId": "A"})))


def fake_clock(lim: RateLimiter):
    now, sleeps = [0.0], []
    lim._clock = lambda: now[0]
    lim._sleep = lambda s: (sleeps.append(s), now.__setitem__(0, now[0] + s))
    return sleeps


def test_registry_one_limiter_per_base_url():
    assert client()._limiter is client()._limiter is shared_limiter("https://a.test/", 5)  # trailing slash is the same URL
    assert client("https://b.test")._limiter is not client()._limiter


def test_two_clients_share_the_spacing():
    c1, c2 = client(), client()
    sleeps = fake_clock(c1._limiter)
    p = ProductRequest(product_id="A", product_code_readable="A", stock_on_hand=0)
    for c in (c1, c2, c1, c2):
        StockTrimApi(c).post_product(p)
    assert sleeps == pytest.approx([0.2, 0.2, 0.2])  # 4 calls from two clients: one stream at 5 rps, not 2 x 5


def test_different_base_urls_are_independent():
    a, b = client("https://a.test"), client("https://b.test")
    sa, sb = fake_clock(a._limiter), fake_clock(b._limiter)
    p = ProductRequest(product_id="A", product_code_readable="A", stock_on_hand=0)
    StockTrimApi(a).post_product(p)
    StockTrimApi(b).post_product(p)
    assert sa == [] and sb == []  # each had its own free first slot


def test_stricter_rps_tightens_never_loosens():
    assert shared_limiter("https://c.test", 5).interval == 0.2
    assert shared_limiter("https://c.test", 2).interval == 0.5
    assert shared_limiter("https://c.test", 50).interval == 0.5


def test_defer_pushes_the_next_slot_forward():
    now, sleeps = [0.0], []
    lim = RateLimiter(5, lambda: now[0], lambda s: (sleeps.append(s), now.__setitem__(0, now[0] + s)))
    lim.acquire()  # free slot, next at 0.2
    lim.defer(3)
    lim.defer(1)  # never moves the slot backwards
    lim.acquire()
    assert sleeps == pytest.approx([3.0])
    lim.acquire()
    assert sleeps == pytest.approx([3.0, 0.2])


def test_threads_reserve_strictly_spaced_slots_with_fake_clock():
    slots: list[float] = []
    lim = RateLimiter(5, lambda: 0.0, slots.append)  # the clock never moves: each sleep is exactly the reserved slot
    threads = [threading.Thread(target=lim.acquire) for _ in range(10)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(slots) == pytest.approx([0.2 * i for i in range(1, 10)])  # the 1st caller is free, the rest 0.2 s apart


BASE = dict(database_url="postgresql+psycopg://u:p@h/d", qbwc_username="u", qbwc_password="p", admin_token="t",
            qwc_owner_id="977e21e5-98f1-440d-ac64-461878a8262c", qwc_file_id="21dfbdc4-f9b7-44bc-aed1-605d1aa21cc0")


def test_from_settings_clients_share_one_limiter():
    s = Settings(_env_file=None, **BASE, stocktrim_auth_id="id", stocktrim_auth_signature="sig", stocktrim_base_url="https://API.Example.test/")
    assert StockTrimClient.from_settings(s)._limiter is StockTrimClient.from_settings(s)._limiter is shared_limiter("https://api.example.test", 5)


def test_tightening_logs_one_warning(caplog):
    shared_limiter("https://w.test", 5)
    with caplog.at_level(logging.WARNING, logger="app.stocktrim"):
        shared_limiter("https://w.test", 5)
        shared_limiter("https://w.test", 50)
        shared_limiter("https://w.test", 2)
        shared_limiter("https://w.test", 2)
    assert [r.levelno for r in caplog.records] == [logging.WARNING]


def test_stocktrim_target_is_scoped_and_hides_the_id():
    def t(**kw):
        return stocktrim_target(Settings(_env_file=None, **BASE, stocktrim_auth_signature="s", **kw))

    a = t(stocktrim_auth_id="secret-id-1")
    assert a.startswith("test|https://api.stocktrim.com|") and "secret-id-1" not in a
    assert len({a, t(stocktrim_auth_id="secret-id-2"), t(stocktrim_auth_id="secret-id-1", stocktrim_environment="production")}) == 3
    assert t(stocktrim_auth_id="secret-id-1", stocktrim_base_url="https://API.stocktrim.com/") == a


def test_thread_safety_smoke():
    lim = RateLimiter(200)
    stamps: list[float] = []

    def work():
        for _ in range(5):
            lim.acquire()
            stamps.append(time.monotonic())

    threads = [threading.Thread(target=work) for _ in range(8)]
    t0 = time.monotonic()
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(stamps) == 40 and time.monotonic() - t0 >= 39 / 200 * 0.9  # 40 acquires take >= 39 intervals
