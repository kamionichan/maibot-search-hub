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
    assert "群友问 EDG" in llm.prompts[0] and llm.kw[0]["task_name"] == "fast_model"
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


# ---------------- 1.4.3 测试实例实测后补充 ----------------
def test_future_year_in_query_does_not_mark_today_stale():
    """“2026 11月 … 2027”：问的是下赛季，2027 不能当发布日期下限，否则今天的新闻全判过时。"""
    need = time_need("EDG 无畏契约 2026 11月 解散 离队 官方 公告 2027", "", "recent", "news", NOW)
    assert need.year == 2026
    c = rule_check("EDG 无畏契约 解散", need, title="EDG冠军班底彻底散伙", snippet="EDG 解散", body=None,
                   published_at="2026-11-06T18:26:00+08:00", now=NOW)
    assert c.verdict == ""
    assert time_need("2027 新番", "", "any", "auto", NOW).year is None


async def test_model_check_retries_when_reasoning_ate_the_output():
    calls = []

    async def gen(prompt, **kw):
        calls.append(kw["max_tokens"])
        if kw["max_tokens"] < 4000:
            return {"success": True, "response": ""}
        return {"success": True, "response": '{"items":[{"sn":1,"v":"direct","r":"ok"}],"gap":""}'}

    checks, _ = await verify.model_check(gen, "p", {1}, task="utils", timeout=20)
    assert calls == [2000, 4000] and checks[1].verdict == "direct"

    async def empty(prompt, **kw):
        return {"success": True, "response": ""}

    with pytest.raises(ValueError, match="输出为空"):
        await verify.model_check(empty, "p", {1}, task="utils", timeout=20)


async def test_verify_falls_back_to_utils_when_task_missing(plug):
    """默认 fast_model；Host 没有这个任务（报“未找到名为 … 的模型配置”）时退回 utils 再试一次。"""
    class PickyLLM(FakeLLM):
        async def generate(self, prompt, **kw):
            self.prompts.append(prompt)
            self.kw.append(kw)
            if kw["task_name"] != "utils":
                return {"success": False, "error": "未找到名为 `fast_model` 的模型配置"}
            return {"success": True, "response": self.reply}

    llm = PickyLLM(json.dumps({"items": [{"sn": 4, "v": "direct", "r": "官宣重组"}], "gap": ""}, ensure_ascii=False))
    res = await _edg_search(plug, llm)
    assert res["success"] and [k["task_name"] for k in llm.kw] == ["fast_model", "utils"]
    assert "【直接回答：官宣重组】" in res["content"]



# ---------------- D80：R6 实测（2026-11-09 正式实例 J62/J63） ----------------
def test_similar_query_detects_rephrase_with_year():
    import plugin as P
    # 只加了年份、调换词序：拦
    assert P._similar_query("彩虹六号围攻 最新版本 更新内容 赛季", "彩虹六号围攻 最新版本 2024 更新内容")
    assert P._similar_query("Steam Deck OLED battery replacement", "Steam Deck OLED battery replacement 2026")
    # R61-07：多了新词就放行
    assert not P._similar_query("Steam Deck OLED battery replacement", "Steam Deck OLED screen replacement")
    assert not P._similar_query("原神 4.8 剧情", "原神 4.8 兑换码")
    assert not P._similar_query("彩虹六号围攻 最新版本 更新内容", "原神 最新版本 角色")
    assert not P._similar_query("python", "rust")


async def test_rephrased_search_while_running_is_not_sent(plug):
    from test_plugin import FakeProvider
    plug._providers = [FakeProvider(delay=3.0)]
    plug.config.search.sync_window_seconds = 1.0
    r1 = await plug.search(**await _grant(plug, args={"query": "彩虹六号围攻 最新版本 更新内容 赛季"}))
    assert "已转为后台任务" in r1["content"]
    r2 = await plug.search(**await _grant(plug, args={"query": "彩虹六号围攻 更新内容 2024 最新版本", "depth": "quick"}))
    assert r2["success"] and "没有重复发起" in r2["content"] and "不要在搜索词里自己加年份" in r2["content"]
    assert len(plug.jobs.active("sess-A")) == 1
    # deep 档视为有意补查，放行
    r3 = await plug.search(**await _grant(plug, args={"query": "彩虹六号围攻 最新赛季 更新内容 对比", "depth": "deep"}))
    assert "没有重复发起" not in (r3.get("content") or "")


def test_evidence_drops_stale_when_fresh_exists_and_warns_when_all_stale():
    from hub import cards
    fresh = {"sn": 8, "url": "https://news.17173.com/x", "title": "Y11S3.3 更新", "snippet": "传奇分段", "published_at": "2026-11-03",
             "last_seen": 0, "verdict": None}
    old = [{"sn": n, "url": f"https://old{n}.example.com/", "title": "碰撞点行动", "snippet": "黑胡子重做", "published_at": "2024-11-18",
            "last_seen": 0, "verdict": "stale"} for n in (13, 14, 15)]
    block = cards.evidence_block(old + [fresh])
    assert "Y11S3.3" in block and "碰撞点行动" not in block
    block2 = cards.evidence_block(old)
    assert "碰撞点行动" in block2 and "都是较早的资料" in block2
    off = dict(fresh, verdict="off")
    block3 = cards.evidence_block(old + [off])       # 只剩无关的“新”条目时，仍按全部过时提醒
    assert "都是较早的资料" in block3 and "Y11S3.3" not in block3


def test_review_line_all_stale():
    from hub import cards
    from hubplugin.hub.verify import Check, Review

    class J:
        review = Review(checks={1: Check("stale", "发布于 2024"), 2: Check("stale", ""), 3: Check("off", "")})
    assert "都是较早的资料" in cards.review_line(J())
    J.review.checks[4] = Check("", "")
    assert "都是较早的资料" not in cards.review_line(J())



# ---------------- D82：正式实例 J73（个人账号发的“官方解散公告”被当成官宣） ----------------
def test_prompt_mentions_unofficial_publisher_and_conflict():
    p = verify.build_prompt("EDG 解散", "", "", TimeNeed(), "2026-11-09", [(1, "解散公告", "sina.cn", "2026-08-11", "x")])
    assert "发布者不是官方" in p and '"conflict"' in p


def test_conflict_downgrades_direct():
    text = json.dumps({"items": [{"sn": 1, "v": "direct", "r": "解散公告"}, {"sn": 2, "v": "partial", "r": "合同到期"}],
                       "conflict": True, "gap": "官方公告与后续报道矛盾"}, ensure_ascii=False)
    checks, gap = parse_reply(text, {1, 2})
    assert checks[1].verdict == "partial" and gap.conflict and str(gap) == "官方公告与后续报道矛盾"
    checks2, gap2 = parse_reply(text.replace('"conflict": true', '"conflict": false'), {1, 2})
    assert checks2[1].verdict == "direct" and not gap2.conflict


async def test_conflict_card_says_no_conclusion(plug):
    reply = json.dumps({"items": [{"sn": 4, "v": "direct", "r": "官宣重组"}, {"sn": 3, "v": "partial", "r": "爆料"}],
                        "conflict": True, "gap": "官方公告与爆料矛盾"}, ensure_ascii=False)
    res = await _edg_search(plug, FakeLLM(reply))
    c = res["content"]
    assert "说法互相矛盾" in c and "不要下定论" in c and "直接回答了问题" not in c
    tr = json.loads(plug.ledger._db.execute("SELECT review FROM trace").fetchone()[0])
    assert tr["conflict"] is True



# ---------------- 0.6.1 审查修复（R61-02/04/05/06/08/09/27） ----------------
def test_r61_04_captcha_tutorial_is_not_a_wall():
    body = "Nginx 如何关闭验证码：在 server 块里删掉 captcha 模块的 include，然后 nginx -s reload 即可。" * 3
    assert verify.invalid_reason("Nginx 关闭验证码教程", "教程", body) == ""
    assert verify.invalid_reason("x", "y", "请先登录后查看完整内容。" + "页脚" * 40) == "页面要求登录或验证"
    assert verify.invalid_reason("x", "y", "Access denied. " + "Please contact the site owner. " * 3) == "页面要求登录或验证"


def test_r61_05_event_date_in_body_is_not_publish_date():
    now = datetime(2026, 11, 9, tzinfo=timezone.utc)
    need = time_need("Node.js 最新 LTS 状态", "", "recent", "auto", now)
    body = "Node.js 20 于 2024-04-24 首发。本文持续更新，介绍当前 LTS 维护状态和最新补丁。" * 3
    c = rule_check("Node.js LTS", need, title="Node.js 维护状态", snippet="当前维护", body=body, published_at=None, now=now)
    assert c.verdict == "" and c.page_date == "2024-04-24"          # 只作线索交给模型
    body2 = "发布时间：2024-04-24\n" + body
    c2 = rule_check("Node.js LTS", need, title="t", snippet="s", body=body2, published_at=None, now=now)
    assert c2.verdict == "stale"


def test_r61_06_recent_prefers_fresh_before_limit():
    from hub.ledger import Ledger
    from hub.merge import Merged
    from hub import cards
    import time as _t
    lg = Ledger(None, per_session=200)
    lg.record("s", [Merged("k0", "https://new.example.com/", "新版本", "新内容", "2026-11-03")])
    _t.sleep(0.01)
    ents = lg.record("s", [Merged(f"k{i}", f"https://old{i}.example.com/", "旧", "旧内容", "2024-11-18") for i in range(1, 8)])
    lg.set_verdicts("s", {e.sn: "stale" for e in ents})
    rec = lg.recent("s", 0)
    assert rec[0]["title"] == "新版本"
    blk = cards.evidence_block(rec)
    assert "新版本" in blk and "都是较早的资料" not in blk


def test_r61_08_long_single_paragraph_excerpt():
    from hub.reader import excerpt
    txt = "Nginx 关闭验证码的方法是" + "在配置里设置参数并重载服务，" * 150
    t, trunc = excerpt(txt, "Nginx 验证码", 500)
    assert t and len(t) <= 520 and trunc
    t2, _ = excerpt("开头无关。" * 100 + "这里讲 Nginx 验证码怎么关。" + "后面无关。" * 100, "Nginx 验证码", 500)
    assert "Nginx 验证码" in t2


def test_r61_27_partial_model_coverage():
    from hub import cards
    checks, _ = parse_reply('{"items":[{"sn":1,"v":"partial","r":"x"}],"gap":""}', {1, 2})
    rv = Review(checks={1: checks[1], 2: Check()}, by_model=True, model_sns={1})

    class J:
        review = rv
    line = cards.review_line(J())
    assert "没有一条直接回答" not in line and "S2 没核查到" in line


async def test_r61_02_job_stays_active_while_reading(plug):
    gate = asyncio.Event()

    class SlowChain(FakeChain):
        async def read(self, url):
            await gate.wait()
            raise RuntimeError("x")
    plug._reader = SlowChain()
    plug.config.search.sync_window_seconds = 1.0
    r1 = await plug.search(**await _grant(plug, args={"query": "彩虹六号 最新版本", "depth": "normal"}))
    assert "后台" in r1["content"]
    assert len(plug.jobs.active("sess-A")) == 1
    r2 = await plug.search(**await _grant(plug, args={"query": "彩虹六号 最新版本", "depth": "normal"}))
    assert "合并" in r2["content"]
    assert "已取消" in plug.jobs.cancel("sess-A", plug.jobs.active("sess-A")[0].job_id)
    gate.set()


async def test_r61_03_09_outer_timeout_keeps_rule_review_and_stops_reads(plug):
    started, stopped = [], []

    class Hang(FakeChain):
        async def read(self, url):
            started.append(url)
            try:
                await asyncio.sleep(30)
            finally:
                stopped.append(url)
    plug._reader = Hang()
    plug.jobs.enrich_timeout = 0.5
    plug.config.search.sync_window_seconds = 5.0
    res = await plug.search(**await _grant(plug, args={"query": "python 最新版本", "depth": "normal"}))
    assert res["success"] and "足以回答" not in res["content"] and "核查没有完成" in res["content"]
    await asyncio.sleep(0.05)
    assert started and sorted(started) == sorted(stopped)        # 子任务被取消并结束



def test_r61_23_numbers_next_to_chinese_and_at_names():
    items = [{"parts": [{"type": "text", "text": '<message msg_id="1" user="小明">请联系小明，电话13800138000，账号 123456789，@小明 看看'}]}]
    out = chat_context(items)
    assert "13800138000" not in out and "123456789" not in out and "@小明" not in out and "@某人" in out


def test_r61_20_deflate_first_chunk_one_byte():
    import zlib
    from hub.net import _Inflater
    raw = zlib.compress(b"hello world " * 50)
    for cut in (1, 2, 4096):
        inf = _Inflater("deflate")
        got = b"".join(b"".join(inf.feed(c, 1 << 20)) for c in (raw[:cut], raw[cut:])) + inf.flush(1 << 20)
        assert got == b"hello world " * 50, cut
    rawd = zlib.compressobj(wbits=-15)
    body = rawd.compress(b"abc" * 100) + rawd.flush()
    inf = _Inflater("deflate")
    assert b"".join(b"".join(inf.feed(c, 1 << 20)) for c in (body[:1], body[1:])) + inf.flush(1 << 20) == b"abc" * 100



def test_cache_hit_review_line_says_not_rechecked():
    from hub import cards

    class J:
        review = Review(checks={1: Check()})
        cached_at = 1.0
    assert "这次没有重新读原文和核查" in cards.review_line(J())



# ---------------- 0.6.1 复审修复 R62-01～06、14 ----------------
def test_r62_01_yandex_page_hint_redacted_before_clip():
    from hub import net
    from hub.image_web import parse_yandex
    key = "sk-" + "Z" * 65
    net.set_secrets([key])
    with pytest.raises(net.SourceError) as ei:
        parse_yandex(f"<html><title>{key}</title><body>" + "x" * 2000 + "</body></html>")
    assert "ZZZZ" not in ei.value.message


async def test_r62_01_baidu_business_error_redacted_before_clip():
    import httpx
    from hub import net
    from hub.image_more import BaiduImageProvider
    key = "sk-" + "Q" * 65
    net.set_secrets([key])

    def h(req):
        return httpx.Response(200, json={"status": 1, "msg": f"token {key} invalid for this request"})
    p = BaiduImageProvider(transport=httpx.MockTransport(h))
    with pytest.raises(net.SourceError) as ei:
        await p.search_image(b"\xff\xd8x")
    assert "QQQQ" not in ei.value.message


def test_r62_02_hit_block_not_pushed_out_by_long_context():
    from hub.reader import excerpt
    body = "这是部署背景，介绍服务器环境和历史情况" * 30 + "Nginx reload 正确配置方法是执行 nginx -s reload" + "后面是无关的文字内容" * 90
    t, _ = excerpt(body, "Nginx reload", 500)
    assert "nginx -s reload" in t and len(t) <= 520


@pytest.mark.parametrize("body", [
    "Nginx 出现 Access denied 或 403 Forbidden 的原因和处理方法：检查 location 块里的 deny 规则和文件权限，改完执行 nginx -s reload。",
    "Cloudflare 一直提示 verify you are human？解决办法：清理 cookie，关闭代理后重试。",
])
def test_r62_03_wall_word_tutorials_not_invalid(body):
    assert verify.invalid_reason("t", "s", body) == ""


@pytest.mark.parametrize("body", [
    "Verify you are human by completing the action below. www.example.com needs to review the security of your connection before proceeding.",
    "Access denied. You do not have permission to access this page on this server. Reference #18.2f3a",
    "请先登录后查看完整内容。" + "页脚 关于我们 联系方式 " * 10,
])
def test_r62_03_real_walls_still_invalid(body):
    assert verify.invalid_reason("t", "s", body) == "页面要求登录或验证"


def test_r62_04_sentence_publish_is_weak_metadata_is_strong():
    now = datetime(2026, 11, 9, tzinfo=timezone.utc)
    need = time_need("Node.js 最新状态", "", "recent", "auto", now)
    sent = "该版本发布于 2024-04-24，本文持续维护，说明当前最新状态。" * 3
    assert rule_check("Node.js 状态", need, title="Node.js", snippet="s", body=sent, published_at=None, now=now).verdict == ""
    for meta in ("发布时间：2024-04-24\n", "发布于：2024-04-24\n", "Published: 2024-04-24\n", "更新日期 2024-04-24\n"):
        c = rule_check("Node.js 状态", need, title="Node.js", snippet="s", body=meta + sent, published_at=None, now=now)
        assert c.verdict == "stale", meta


def test_r62_05_hidden_entries_dont_push_out_background():
    from hub.ledger import Ledger
    from hub.merge import Merged
    import time as _t
    lg = Ledger(None, per_session=200)
    e0 = lg.record("s", [Merged("k0", "https://old.example.com/", "旧背景", "x", "2024-01-01")])
    lg.set_verdicts("s", {e0[0].sn: "stale"})
    _t.sleep(0.01)
    es = lg.record("s", [Merged(f"k{i}", f"https://off{i}.example.com/", "无关", "x", None) for i in range(1, 7)])
    lg.set_verdicts("s", {e.sn: "off" for e in es})
    rec = lg.recent("s", 0)
    assert [r["title"] for r in rec] == ["旧背景"]


def test_r62_06_single_letter_subjects_kept():
    import plugin as P
    assert not P._similar_query("C programming memory management", "R programming memory management")
    assert P._similar_query("C programming memory management", "C programming memory management 2026")


async def test_r62_14_cancel_still_writes_rule_verdicts(plug):
    started = asyncio.Event()

    class Hang(FakeChain):
        async def read(self, url):
            started.set()
            await asyncio.sleep(30)
    plug._reader = Hang()
    plug._providers = [EdgProvider()]
    plug.config.search.sync_window_seconds = 1.0
    res = await plug.search(**await _grant(plug, args={"query": "EDG 分裂", "depth": "normal"}))
    assert "后台" in res["content"]
    await asyncio.wait_for(started.wait(), 3)
    jid = plug.jobs.active("sess-A")[0].job_id
    assert "已取消" in plug.jobs.cancel("sess-A", jid)
    await asyncio.sleep(0.1)
    v = dict(plug.ledger._db.execute("SELECT title, verdict FROM entries WHERE session_id='sess-A'").fetchall())
    assert any(x == "invalid" for x in v.values())     # 乱码标题那条：规则判无效，已写回
