"""D72：自动读原文 + 结果核查。"""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from hub import verify
from hub.verify import Check, Review, TimeNeed, chat_context, merge_checks, parse_reply, rule_check, time_need

from test_plugin import FakeChain, FakeProvider, _grant, plug  # noqa: F401  (fixture)
from hub.providers import Hit

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)


# ---------------- 时间要求 ----------------
def test_time_need():
    assert time_need("EDG 分裂", "", "any", "auto", NOW).window is None
    assert time_need("EDG 最近 分裂", "", "any", "auto", NOW).window == timedelta(days=60)
    assert time_need("x", "", "today", "auto", NOW).window == timedelta(hours=48)
    assert time_need("x", "", "any", "news", NOW).window is not None
    assert time_need("今年 双十一 销量", "", "any", "auto", NOW).year == 2026
    assert time_need("2024 无畏契约 冠军赛", "", "any", "auto", NOW).year == 2024
    assert time_need("EDG", "群友问 EDG 最近是不是要解散", "any", "auto", NOW).window is not None   # goal 也算


# ---------------- 规则 ----------------
def test_rule_garbled_title_is_invalid():
    c = rule_check("EDG 分裂", TimeNeed(), title='{\\"code\\":0,\\"stg_vers', snippet="Edg真的要分开了吗?",
                   body=None, published_at="2026-10-26", now=NOW)
    assert c.verdict == verify.INVALID


def test_rule_login_wall_and_empty_page():
    assert rule_check("x 价格", TimeNeed(), title="t", snippet="短", body="请先登录后查看", published_at=None,
                      now=NOW).verdict == verify.INVALID
    assert rule_check("x 价格", TimeNeed(), title="t", snippet="短", body="", published_at=None,
                      now=NOW).verdict == verify.INVALID


def test_rule_stale_by_window_and_year():
    need = time_need("EDG 最近 分裂", "", "any", "auto", NOW)
    c = rule_check("EDG 最近 分裂", need, title="EDG赢比赛,圣枪哥却被骂了", snippet="队内舆论分裂严重",
                   body=None, published_at="2022-06-11", now=NOW)
    assert c.verdict == verify.STALE and "2022-06-11" in c.reason
    c2 = rule_check("2026 新番", time_need("2026 新番", "", "any", "auto", NOW), title="2025 新番盘点", snippet="新番",
                    body=None, published_at="2025-12-01", now=NOW)
    assert c2.verdict == verify.STALE


def test_rule_no_time_need_does_not_mark_old_stale():
    """没有时间要求时，旧文章不算过时（问历史、问梗都很正常）。"""
    c = rule_check("蚁人去哪了 梗", TimeNeed(), title="蚁人去哪了是什么梗", snippet="出自复联", body=None,
                   published_at="2019-05-01", now=NOW)
    assert c.verdict == ""


def test_rule_page_date_from_body():
    body = "发布时间：2023-06-20 10:00\n" + "EDG 13:9 PRX " * 30
    need = time_need("EDG 最近", "", "any", "auto", NOW)
    c = rule_check("EDG 最近", need, title="赛果", snippet="", body=body, published_at=None, now=NOW)
    assert c.verdict == verify.STALE and c.page_date == "2023-06-20"


def test_rule_off_topic():
    c = rule_check("鬼脑 是什么", TimeNeed(), title="天气预报", snippet="明天多云", body=None, published_at=None, now=NOW)
    assert c.verdict == verify.OFF


# ---------------- 模型输出解析与合并 ----------------
def test_parse_reply_tolerant():
    text = '```json\n{"items":[{"sn":"S3","v":"direct","r":"官宣"},{"sn":4,"v":"bogus"},{"sn":99,"v":"off"}],"gap":"缺时间"}\n```'
    checks, gap = parse_reply(text, {3, 4})
    assert set(checks) == {3} and checks[3].verdict == "direct" and gap == "缺时间"
    with pytest.raises(ValueError):
        parse_reply("我觉得都挺好", {1})
    with pytest.raises(ValueError):
        parse_reply('{"items":[]}', {1})


def test_merge_checks_priorities():
    rule = {1: Check(verify.INVALID, "乱码", "rule"), 2: Check(verify.STALE, "发布于 2022", "rule"), 3: Check()}
    model = {1: Check(verify.DIRECT, "x", "model"), 2: Check(verify.DIRECT, "讲分裂", "model"),
             3: Check(verify.OFF, "讲的是比赛", "model")}
    m = merge_checks(rule, model)
    assert m[1].verdict == verify.INVALID                 # 规则判无效一票否决
    assert m[2].verdict == verify.STALE and "讲分裂" in m[2].reason
    assert m[3].verdict == verify.OFF and m[3].by == "model"


# ---------------- 聊天上下文 ----------------
def test_chat_context_anonymizes():
    items = [{"parts": [{"text": '<message msg_id="123456789" time="09:11" user="亚托莉" is_self_message="true">\n我是机器人'}]},
             {"parts": [{"text": '<message msg_id="987654321" time="09:13" user="张三" group_card="QQ 10001234">\n'
                                 'EDG 是不是要解散了？我QQ是 123456789'}]}]
    out = chat_context(items)
    assert "你：我是机器人" in out and "对方：EDG 是不是要解散了" in out
    assert "张三" not in out and "亚托莉" not in out and "123456789" not in out and "10001234" not in out


# ---------------- 插件层：EDG 场景 ----------------
class EdgProvider(FakeProvider):
    async def search(self, q, *, max_results):
        return [
            Hit("计划有变!首尔冠军赛胜决:EDG逆转战胜LEV", "https://baijiahao.baidu.com/s?id=1", "EDG 2-1逆转", "2024-08-23"),
            Hit('{\\"code\\":0,\\"stg_vers', "https://www.douyin.com/video/2", "Edg真的要分开了吗?", "2026-10-26"),
            Hit("EDG赢比赛,圣枪哥却被骂了!队内舆论分裂严重", "https://baijiahao.baidu.com/s?id=3", "舆论分裂", "2022-06-11"),
            Hit("EDG 无畏契约分部宣布重组", "https://news.example.com/edg", "EDG 分部 调整", "2026-11-05"),
        ]


class FakeLLM:
    def __init__(self, reply=None, fail=None, delay=0.0):
        self.reply, self.fail, self.delay, self.prompts, self.kw = reply, fail, delay, [], []

    async def generate(self, prompt, **kw):
        self.prompts.append(prompt)
        self.kw.append(kw)
        await asyncio.sleep(self.delay)
        if self.fail:
            return {"success": False, "error": self.fail}
        return {"success": True, "response": self.reply}


class BodyChain(FakeChain):
    def __init__(self, bodies):
        super().__init__()
        self.bodies = bodies

    async def read(self, url):
        from hubplugin.hub.reader import Page
        self.urls.append(url)
        return Page(url, "", self.bodies.get(url, "正文" * 50), "builtin")


EDG_BODIES = {"https://news.example.com/edg": "2026-11-05 EDG 官方宣布无畏契约分部重组，多名选手离队，" * 10}


async def _edg_search(plug, llm, goal="群友问 EDG 最近是不是要分裂", query="EDG 分裂"):
    plug._providers = [EdgProvider()]
    plug._reader = BodyChain(EDG_BODIES)
    plug.ctx.llm = llm
    plug.config.search.sync_window_seconds = 10.0
    args = await _grant(plug, args={"query": query, "goal": goal})
    return await plug.search(**args)


async def test_edg_model_review_marks_direct_and_hides_noise(plug):
    reply = json.dumps({"items": [{"sn": 1, "v": "off", "r": "讲的是 2024 年比赛"},
                                  {"sn": 3, "v": "partial", "r": "2022 年的舆论"},
                                  {"sn": 4, "v": "direct", "r": "官宣重组"}], "gap": ""}, ensure_ascii=False)
    llm = FakeLLM(reply)
    res = await _edg_search(plug, llm)
    c = res["content"]
    assert res["success"]
    # 乱码标题的抖音结果被规则判无效、折叠；讲比赛的被模型判无关、折叠
    assert "已略去" in c and "页面无效" in c and "讲的是 2024 年比赛" in c
    assert "【直接回答：官宣重组】" in c and "S4 直接回答了问题" in c
    # 2022 年的结果：问题要“最近”，规则判过时，即使模型说部分相关
    assert "【可能过时" in c
    assert "足以回答" not in c
    # 核查用了 goal 和指定的模型任务；抖音那条没有交给模型
    assert "群友问 EDG" in llm.prompts[0] and llm.kw[0]["task_name"] == "utils"
    assert "<S2>" not in llm.prompts[0]
    # normal 档读了前 2 篇
    assert len(plug._reader.urls) == 2
    # 回复参考资料跳过无关、无效
    ev = plug.ledger.recent("sess-A", 0)
    from hubplugin.hub import cards
    block = cards.evidence_block(ev)
    assert "首尔冠军赛" not in block and "stg_vers" not in block and "【直接回答】" in block


async def test_edg_nothing_answers(plug):
    reply = json.dumps({"items": [{"sn": n, "v": "off", "r": "无关"} for n in (1, 3, 4)], "gap": "没有分裂的消息"},
                       ensure_ascii=False)
    res = await _edg_search(plug, FakeLLM(reply))
    assert "都没有回答问题" in res["content"] and "没有分裂的消息" in res["content"]


async def test_model_failure_falls_back_to_rules(plug):
    res = await _edg_search(plug, FakeLLM(fail="model not found"))
    c = res["content"]
    assert res["success"] and "页面无效" in c and "没有核查过是否真正回答了问题" in c
    tr = plug.ledger._db.execute("SELECT review FROM trace").fetchone()[0]
    assert "model not found" in tr


async def test_model_timeout_falls_back(plug):
    plug.config.verify.timeout_seconds = 3.0
    res = await _edg_search(plug, FakeLLM("{}", delay=5.0))
    assert res is not None   # 同步窗口 10 秒内：读正文 + 3 秒核查超时后返回
    assert "没有核查过" in (res.get("content") or "")


async def test_verify_disabled_no_model_call(plug):
    plug.config.verify.enabled = False
    llm = FakeLLM("{}")
    res = await _edg_search(plug, llm)
    assert llm.prompts == [] and res["success"]


async def test_quick_does_not_read_or_call_model(plug):
    llm = FakeLLM("{}")
    plug._providers = [EdgProvider()]
    plug._reader = BodyChain(EDG_BODIES)
    plug.ctx.llm = llm
    res = await plug.search(**await _grant(plug, args={"query": "EDG 分裂", "depth": "quick"}))
    assert res["success"] and llm.prompts == [] and plug._reader.urls == []
    assert "页面无效" in res["content"]          # 规则核查照常


async def test_read_disabled_still_rule_checks(plug):
    plug.config.read.enabled = False
    llm = FakeLLM(json.dumps({"items": [{"sn": 4, "v": "direct", "r": "ok"}], "gap": ""}))
    res = await _edg_search(plug, llm)
    assert plug._reader.urls == [] and "S4 直接回答了问题" in res["content"]


async def test_chat_context_not_sent_to_search_provider(plug):
    seen = []

    class Spy(EdgProvider):
        async def search(self, q, *, max_results):
            seen.append(q)
            return await super().search(q, max_results=max_results)

    from test_plugin import _call, _tools
    items = [{"parts": [{"text": '<message msg_id="1" user="甲">\nEDG 是不是要散伙了'}]}]
    # 发起搜索的那次 Planner 请求本身带着聊天历史
    await plug.hub_context(items=items, tool_definitions=_tools("reply", "search"), session_id="sess-A")
    assert "散伙" in plug._chat_ctx["sess-A"]
    out = await plug.hub_grant(output_items=[_call({"query": "EDG 分裂", "goal": "想知道 EDG 是否解散"})],
                               session_id="sess-A")
    args = out["modified_kwargs"]["output_items"][0]["tool_call"]["args"]
    plug._providers = [Spy()]
    plug._reader = BodyChain(EDG_BODIES)
    llm = FakeLLM(json.dumps({"items": [{"sn": 4, "v": "direct", "r": "ok"}], "gap": ""}))
    plug.ctx.llm = llm
    plug.config.search.sync_window_seconds = 10.0
    await plug.search(**args)
    assert seen == ["EDG 分裂"]
    # D73：模型写了 goal，就只用 goal，不带聊天
    assert "想知道 EDG 是否解散" in llm.prompts[0] and "散伙" not in llm.prompts[0]


async def test_chat_context_fallback_when_no_goal(plug):
    from test_plugin import _call, _tools
    items = [{"parts": [{"text": '<message msg_id="1" user="甲">\nEDG 是不是要散伙了'}]}]
    await plug.hub_context(items=items, tool_definitions=_tools("reply", "search"), session_id="sess-A")
    out = await plug.hub_grant(output_items=[_call({"query": "EDG 分裂"})], session_id="sess-A")
    args = out["modified_kwargs"]["output_items"][0]["tool_call"]["args"]
    plug._providers = [EdgProvider()]
    plug._reader = BodyChain(EDG_BODIES)
    llm = FakeLLM(json.dumps({"items": [{"sn": 4, "v": "direct", "r": "ok"}], "gap": ""}))
    plug.ctx.llm = llm
    plug.config.search.sync_window_seconds = 10.0
    await plug.search(**args)
    assert "对方：EDG 是不是要散伙了" in llm.prompts[0] and "提问者想弄清" not in llm.prompts[0]
    # 关掉兜底就不带聊天
    plug.config.verify.use_chat_context = False
    plug.config.search.cache_enabled = False
    await plug.hub_context(items=items, tool_definitions=_tools("reply", "search"), session_id="sess-A")
    out = await plug.hub_grant(output_items=[_call({"query": "EDG 分裂"})], session_id="sess-A")
    await plug.search(**out["modified_kwargs"]["output_items"][0]["tool_call"]["args"])
    assert "散伙" not in llm.prompts[-1]


async def test_cache_hit_rule_review_only(plug):
    llm = FakeLLM(json.dumps({"items": [{"sn": 4, "v": "direct", "r": "ok"}], "gap": ""}))
    plug.config.search.sync_window_seconds = 10.0
    r1 = await _edg_search(plug, llm)
    plug.ledger.cache_put  # 第一次结果完整就会进缓存
    n = len(llm.prompts)
    r2 = await plug.search(**await _grant(plug, args={"query": "EDG 分裂"}))
    assert r1["success"] and r2["success"] and len(llm.prompts) == n


def test_ledger_verdict_columns_migrate(tmp_path):
    import sqlite3
    from hub.ledger import Ledger
    p = tmp_path / "h.sqlite3"
    db = sqlite3.connect(p)
    db.executescript("""CREATE TABLE entries (session_id TEXT NOT NULL, sn INTEGER NOT NULL, url_key TEXT NOT NULL,
      url TEXT NOT NULL, title TEXT, snippet TEXT, published_at TEXT, sources TEXT, first_seen REAL, last_seen REAL,
      hits INTEGER DEFAULT 1, used INTEGER DEFAULT 0, reads INTEGER DEFAULT 0, PRIMARY KEY (session_id, sn));
      CREATE TABLE trace (job_id TEXT, t REAL, session_id TEXT, query TEXT, depth TEXT, intent TEXT, caller TEXT,
      outcome TEXT, sync INTEGER, seconds REAL, attempts TEXT, missing TEXT, used_sns TEXT);""")
    db.close()
    led = Ledger(p)
    cols = {r[1] for r in led._db.execute("PRAGMA table_info(entries)")}
    tcols = {r[1] for r in led._db.execute("PRAGMA table_info(trace)")}
    assert "verdict" in cols and "review" in tcols
    led.trace({"job_id": "J1", "t": 1.0, "session_id": "s", "query": "q", "depth": "normal", "intent": "auto",
               "caller": "planner", "outcome": "finished", "sync": None, "seconds": 1.0, "attempts": [],
               "missing": [], "used_sns": []})
    led.close()


def test_review_summary_shape():
    r = Review(checks={1: Check(verify.DIRECT, "x", "model")}, gap="g", by_model=True)
    s = r.summary()
    assert s["v"] == {"1": "direct"} and s["model"] is True


# ---------------- D72 补充：读不到就换下一条；404 不再换读取方式 ----------------
class FlakyChain(FakeChain):
    """按网址决定：抛错 / 返回登录墙 / 返回正文。"""

    def __init__(self, plan, delay=0.0):
        super().__init__()
        self.plan, self.delay = plan, delay

    async def read(self, url):
        from hubplugin.hub.reader import Page
        from hubplugin.hub.net import Outcome, SourceError
        self.urls.append(url)
        await asyncio.sleep(self.delay)
        kind = self.plan.get(url, "ok")
        if kind == "404":
            raise SourceError(Outcome.REJECTED, "网页返回 HTTP 404", status=404)
        if kind == "timeout":
            raise asyncio.TimeoutError()
        if kind == "wall":
            return Page(url, "", "请先登录后查看", "builtin")
        return Page(url, "", "EDG 分部重组 正文内容 " * 30, "builtin")


class FiveProvider(FakeProvider):
    async def search(self, q, *, max_results):
        return [Hit(f"EDG 分裂 新闻{i}", f"https://n{i}.example.com/a", f"EDG 分裂 摘要{i}", "2026-11-05")
                for i in range(1, 6)]


async def _five(plug, plan, depth="normal", delay=0.0):
    plug._providers = [FiveProvider()]
    plug._reader = FlakyChain(plan, delay)
    plug.ctx.llm = FakeLLM(json.dumps({"items": [], "gap": ""}))   # 模型输出无效 → 只用规则
    plug.config.search.sync_window_seconds = 20.0
    return await plug.search(**await _grant(plug, args={"query": "EDG 分裂", "depth": depth}))


async def test_read_replaces_failed_with_next(plug):
    plan = {"https://n1.example.com/a": "404", "https://n2.example.com/a": "wall"}
    res = await _five(plug, plan)
    urls = plug._reader.urls
    # 前两条读不到，补读第 3、4 条，凑够 2 篇
    assert urls[:2] == ["https://n1.example.com/a", "https://n2.example.com/a"]
    assert set(urls) == {f"https://n{i}.example.com/a" for i in (1, 2, 3, 4)}
    c = res["content"]
    assert c.count("（已读正文）") == 2
    assert "网页不存在或已被删除" in c          # 404 的那条判页面无效并写明原因


async def test_read_extra_is_capped(plug):
    plan = {f"https://n{i}.example.com/a": "timeout" for i in range(1, 6)}
    await _five(plug, plan)
    assert len(plug._reader.urls) == 4           # normal 2 篇 + 最多多试 2 条


async def test_read_budget(plug):
    plug.READ_BUDGET = 1.0
    res = await _five(plug, {}, delay=3.0)
    assert res["success"] and "（已读正文）" not in res["content"]   # 超过总时限：不等，保留摘要


async def test_readchain_stops_on_404():
    from hubplugin.hub.reader import PageGone, ReadChain
    from hubplugin.hub.net import Outcome, SourceError
    from types import SimpleNamespace
    calls = []

    class Web:
        async def fetch(self, url):
            calls.append("host")
            raise RuntimeError("联网服务返回 HTTP 404 Not Found")

    class Api:
        async def call(self, *a, **k):
            calls.append("fetch_url")
            return {"success": True, "result": {"content": "x"}}

    class Builtin:
        resolver = staticmethod(lambda h, p: asyncio.sleep(0, result=["93.184.216.34"]))

        async def fetch(self, url):
            calls.append("builtin")
            raise SourceError(Outcome.REJECTED, "网页返回 HTTP 404", status=404)

    chain = ReadChain(SimpleNamespace(web=Web(), api=Api()), Builtin())
    with pytest.raises(PageGone):
        await chain.read("https://example.com/missing")
    assert calls == ["host"]                       # Host 已经说 404：不再换 fetch-url、内置读取器
    calls.clear()
    chain2 = ReadChain(SimpleNamespace(web=Web(), api=Api()), Builtin(), use_host=False, use_fetch_url=False)
    with pytest.raises(PageGone):
        await chain2.read("https://example.com/missing")
    assert calls == ["builtin"]


def test_read_failure_reason():
    from hubplugin.hub.reader import read_failure_reason
    from hubplugin.hub.net import Outcome, SourceError
    assert read_failure_reason(SourceError(Outcome.REJECTED, "网页返回 HTTP 410", status=410)) == ("网页不存在或已被删除（HTTP 404/410）", True)
    assert read_failure_reason(SourceError(Outcome.REJECTED, "网页返回 HTTP 403", status=403))[0] == "网站拒绝自动访问"
    assert read_failure_reason(asyncio.TimeoutError())[0] == "网站连不上或超时"
    assert read_failure_reason(SourceError(Outcome.NOT_SENT, "连接失败"))[0] == "网站连不上或超时"
    assert "JavaScript" in read_failure_reason(SourceError(Outcome.REJECTED, "网页没有可读正文（可能需要 JavaScript）"))[0]


async def test_read_source_failure_names_reason(plug):
    from test_plugin import _grant_read
    await plug.search(**await _grant(plug))
    plug._reader = FlakyChain({"https://s1.example.com/a": "404"})
    res = await plug.read_source(**await _grant_read(plug, args={"ref": "S2"}))
    assert not res["success"] and "网页不存在或已被删除" in res["error"] and "换另一条结果" in res["error"]
