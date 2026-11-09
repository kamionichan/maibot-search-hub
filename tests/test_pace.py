"""D84：本机网页/浏览器访问的节流（间隔 + 随机、同时只一个、每小时上限、排队超时跳过）与“都到限制了”的说明。"""

import asyncio
import json
import time

import pytest

from hub import cards
from hub.health import Health
from hub.image_exec import ImageExecutor, make_image_plan
from hub.net import Outcome, SourceError
from hub.pace import Pacer

from test_image import FakeImg, Prep
from test_plugin import plug  # noqa: F401  (fixture)


def P(**kw):
    cfg = dict(min_gap=0.0, jitter=0.0, per_hour=0, max_wait=5.0, step_range=(0.0, 0.0))
    cfg.update(kw)
    return Pacer("测试引擎", **cfg)


async def test_min_gap_and_jitter_between_uses():
    p = P(min_gap=0.3, jitter=0.2, max_wait=2.0)
    t = []
    for _ in range(3):
        async with p.slot():
            t.append(time.monotonic())
    gaps = [b - a for a, b in zip(t, t[1:])]
    assert all(0.29 <= g <= 0.55 for g in gaps), gaps


async def test_one_at_a_time():
    p = P(max_wait=3.0)
    active, peak = 0, 0

    async def use():
        nonlocal active, peak
        async with p.slot():
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.05)
            active -= 1
    await asyncio.gather(*(use() for _ in range(4)))
    assert peak == 1


async def test_too_long_wait_is_skipped_with_reason():
    p = P(min_gap=60.0, max_wait=2.0)
    async with p.slot():
        pass
    with pytest.raises(SourceError) as ei:
        async with p.slot():
            pass
    e = ei.value
    assert e.budget and e.outcome is Outcome.NOT_SENT and "两次之间要隔一段时间" in e.message and "约 60 秒后可用" in e.message


async def test_queue_timeout_when_busy():
    p = P(max_wait=0.2)
    gate = asyncio.Event()

    async def hold():
        async with p.slot():
            await gate.wait()
    t = asyncio.create_task(hold())
    await asyncio.sleep(0.02)
    with pytest.raises(SourceError, match="正在被别的搜索使用"):
        async with p.slot():
            pass
    gate.set()
    await t


async def test_step_is_random_pause():
    p = P(step_range=(0.05, 0.1))
    t0 = time.monotonic()
    await p.step()
    assert 0.045 <= time.monotonic() - t0 <= 0.2


def test_status_text():
    p = P(per_hour=6)
    assert "近一小时 0/6 次" in p.status()


# ---------------- 回退：受限的跳过、换别的引擎；都受限时明确说 ----------------
class Limited(FakeImg):
    async def search_image(self, jpeg, *, max_results=3):
        self.calls += 1
        raise SourceError(Outcome.NOT_SENT, "Yandex 刚用过，两次之间要隔一段时间，约 8 秒后可用（本插件限速）", budget=True)


async def test_limited_engine_falls_back_to_next():
    from test_image import ih
    a = Limited("img_yandex", web=True)
    b = FakeImg("img_baidu", web=True, hits=[ih("hit", kind="visual")])
    r = await ImageExecutor(Health()).run(make_image_plan([a, b], Health(), depth="normal", intent="similar_image",
                                                          image=Prep()), "q")
    assert b.calls == 1 and r.used_sources == ["img_baidu"] and not r.complete
    assert r.attempts[0].outcome == "skipped"


async def test_all_limited_says_so():
    a, b = Limited("img_yandex", web=True), Limited("img_baidu", web=True)
    r = await ImageExecutor(Health()).run(make_image_plan([a, b], Health(), depth="normal", intent="similar_image",
                                                          image=Prep()), "q")
    assert r.error_kind == "limited" and "约 8 秒后可用" in r.error

    class J:
        job_id, image_hash, result = "J9", "h", r
    text = cards.failure(J())
    assert "达到了使用限制" in text and "不要说“没找到出处”" in text


async def test_partial_limited_note_in_card():
    from hub.jobs import Job
    from hub.ledger import Ledger
    from test_image import ih
    a = Limited("img_yandex", web=True)
    b = FakeImg("img_baidu", web=True, hits=[ih("weak", kind="visual")])
    r = await ImageExecutor(Health()).run(make_image_plan([a, b], Health(), depth="normal", intent="similar_image",
                                                          image=Prep()), "q")
    j = Job("J1", "s", "以图搜", "normal", "similar_image", "any", "planner")
    j.image_hash, j.result = "h", r
    j.entries = Ledger(None).record("s", r.results)
    text = cards.sync_result(j)
    assert "这次有引擎因为限制没查" in text and "Yandex" in text and "不要只说“没找到”" in text
    assert text.index("这次有引擎因为限制没查") < text.index("不足：")
    assert "这次只查了一部分引擎" in text



async def test_status_shows_pacing(plug):
    text = plug._status_text()
    assert "限速：近一小时" in text          # Yandex / 百度识图 / Bing 默认开启



async def test_pacing_survives_restart():
    from hub import pace
    pace.reset_for_tests()
    p = pace.pacer("img_x", "X", min_gap=120.0, jitter=0.0, per_hour=2, max_wait=1.0)
    async with p.slot():
        pass
    saved = pace.dump()
    pace.reset_for_tests()
    pace.load(saved)
    p2 = pace.pacer("img_x", "X", min_gap=120.0, jitter=0.0, per_hour=2, max_wait=1.0)
    assert "近一小时 1/2 次" in p2.status()
    with pytest.raises(SourceError, match="两次之间要隔一段时间"):
        async with p2.slot():
            pass



# ---------------- 0.6.1 复审修复 R62-07/08/09/10/11 ----------------
async def test_r62_07_queue_and_gap_share_one_budget():
    p = P(min_gap=0.12, max_wait=0.15)

    async def hold():
        async with p.slot():
            await asyncio.sleep(0.10)
    t = asyncio.create_task(hold())
    await asyncio.sleep(0.001)
    t0 = time.monotonic()
    with pytest.raises(SourceError):
        async with p.slot():
            pass
    assert time.monotonic() - t0 <= 0.17           # 原来要等到 0.22 秒才放行
    await t


async def test_r62_08_cooldown_set_while_queued_is_respected():
    from hub.image_exec import ImageExecutor, make_image_plan
    from hub.net import SourceError as SE
    h = Health()
    shared = P(max_wait=3.0)
    gate = asyncio.Event()
    sent = []

    class Eng(FakeImg):
        def __init__(self, tag):
            super().__init__("img_yandex", web=True)
            self.pacer, self.tag = shared, tag

        async def search_image(self, jpeg, *, max_results=3, ready=None):
            async with self.pacer.slot(ready):
                sent.append(self.tag)
                if self.tag == "A":
                    await gate.wait()
                    raise SE(Outcome.REJECTED, "要求验证", status=429, retry_after=1800)
                return []
    a, b = Eng("A"), Eng("B")
    ta = asyncio.create_task(ImageExecutor(h).run(make_image_plan([a], h, depth="normal", intent="similar_image",
                                                                    image=Prep()), "q"))
    await asyncio.sleep(0.02)
    tb = asyncio.create_task(ImageExecutor(h).run(make_image_plan([b], h, depth="normal", intent="similar_image",
                                                                    image=Prep()), "q"))
    await asyncio.sleep(0.02)
    gate.set()
    await ta
    rb = await tb
    assert sent == ["A"] and rb.attempts[-1].outcome == "skipped" and "排队期间进入冷却" in rb.attempts[-1].note
    assert not h.available("img_yandex")         # 冷却没有被 B 的“成功”清掉


async def test_r62_09_dormant_engine_state_survives_another_restart():
    from hub import pace
    pace.reset_for_tests()
    p = pace.pacer("img_x", "X", min_gap=120.0, jitter=0.0, per_hour=5, max_wait=1.0)
    async with p.slot():
        pass
    saved = pace.dump()
    pace.reset_for_tests()
    pace.load(saved)                             # 第二次启动：img_x 被关了，没建 Pacer
    saved2 = pace.dump()
    assert "img_x" in saved2 and len(saved2["img_x"]["hour"]) == 1
    pace.reset_for_tests()
    pace.load(saved2)                            # 第三次启动：又开了
    p3 = pace.pacer("img_x", "X", min_gap=120.0, jitter=0.0, per_hour=5, max_wait=1.0)
    assert "近一小时 1/5 次" in p3.status()


async def test_r62_10_unload_saves_gap_set_by_last_request(plug):
    import sys
    pace = sys.modules["hubplugin.hub.pace"]          # 插件用的那一份模块
    p = pace.pacer("img_yandex", "Yandex", min_gap=60.0, jitter=0.0, per_hour=0, max_wait=1.0)
    entered = asyncio.Event()

    async def use():
        async with p.slot():
            entered.set()
            await asyncio.sleep(10)
    t = asyncio.create_task(use())
    plug.jobs._tails.add(t)                       # JobManager 托管的在途请求：卸载时会被取消
    await entered.wait()
    written = {}
    orig = plug.ledger.kv_set
    plug.ledger.kv_set = lambda k, v: (written.__setitem__(k, v), orig(k, v))[1]
    await plug.on_unload()
    saved = json.loads(written["pace"])
    assert saved["img_yandex"]["next"] - time.time() > 50     # 收尾时设的 60 秒间隔存进去了
    plug.on_unload = lambda: asyncio.sleep(0)     # fixture 收尾时不要再卸载一次


async def test_r62_11_clock_step_back_is_capped():
    from hub import pace
    pace.reset_for_tests()
    p = pace.pacer("img_x", "X", min_gap=120.0, jitter=0.0, per_hour=5, max_wait=1.0)
    async with p.slot():
        pass
    saved = pace.dump()
    real = time.time
    pace.reset_for_tests()
    try:
        time.time = lambda: real() - 7200
        pace.load(saved)
        p2 = pace.pacer("img_x", "X", min_gap=120.0, jitter=0.0, per_hour=5, max_wait=1.0)
        assert p2._next - time.monotonic() <= 121
        assert len(p2._hour) == 0                   # “未来”的记录不算
    finally:
        time.time = real



# ---------------- 候选图按网站限速（用户 2026-11-09 同意） ----------------
async def test_site_pacer_serializes_same_site_parallel_across_sites():
    from hub.pace import SitePacer
    sp = SitePacer(gap=0.2, jitter=0.0)
    log = []

    async def get(url):
        async with sp.slot(url, time.monotonic() + 5) as ok:
            assert ok
            log.append((sp.site(url), time.monotonic()))
            await asyncio.sleep(0.05)
    t0 = time.monotonic()
    await asyncio.gather(get("https://i.example.com/1.jpg"), get("https://cdn.example.com/2.jpg"),
                         get("https://other.test/3.jpg"))
    ex = sorted(t for s, t in log if s == "example.com")
    ot = [t for s, t in log if s == "other.test"]
    assert ex[1] - ex[0] >= 0.24                  # 同站：前一张下完再隔 0.2 秒
    assert ot[0] - t0 < 0.05                      # 别的站不用等


async def test_site_pacer_gives_up_at_deadline():
    from hub.pace import SitePacer
    sp = SitePacer(gap=5.0, jitter=0.0)
    async with sp.slot("https://a.example.com/x", time.monotonic() + 1) as ok:
        assert ok
    async with sp.slot("https://a.example.com/y", time.monotonic() + 0.3) as ok2:
        assert ok2 is False
