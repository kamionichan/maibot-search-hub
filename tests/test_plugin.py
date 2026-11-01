"""插件层测试：授权 Hook、伪造身份、请求类型识别、进度注入。用假 ctx，不连 Host。"""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

import plugin as P
from hub.providers import Hit, ProviderInfo


class FakeSend:
    def __init__(self):
        self.sent = []

    async def text(self, text, stream_id, **kw):
        self.sent.append((stream_id, text))
        return True


class FakeMaisaka:
    def __init__(self):
        self.cards = []

    async def trigger_proactive(self, stream_id, intent, **kw):
        self.cards.append((stream_id, intent))
        return {"success": True, "queued": True}


class FakeProvider:
    def __init__(self, delay=0.0):
        self.info = ProviderInfo("keenable", "keenable", "fast", False, True, False)
        self.delay = delay

    async def search(self, q, *, max_results):
        await asyncio.sleep(self.delay)
        return [Hit(f"{q} 结果{i}", f"https://s{i}.example.com/a", f"{q} 的内容 {i}") for i in range(4)]


@pytest.fixture
async def plug(tmp_path: Path):
    p = P.SearchHubPlugin()
    ctx = SimpleNamespace(
        paths=SimpleNamespace(data_dir=tmp_path), send=FakeSend(), maisaka=FakeMaisaka(),
        logger=SimpleNamespace(warning=lambda *a, **k: None),
    )
    p._ctx = ctx  # type: ignore[attr-defined]
    type(p).ctx = property(lambda self: self._ctx)  # type: ignore[assignment]
    p._plugin_config_instance = P.HubConfig()  # type: ignore[attr-defined]
    await p.on_load()
    p._providers = [FakeProvider()]
    yield p
    await p.on_unload()


def _tools(*names):
    return [{"type": "function", "function": {"name": n}} for n in names]


def _call(args, name="search"):
    return {"item_type": "FunctionCallItem", "meta": {"logical_turn_id": "t1"},
            "tool_call": {"call_id": "c1", "func_name": name, "args": args}}


async def _grant(plug, session="sess-A", caller_tools=("reply", "search"), args=None):
    await plug.hub_context(items=[], tool_definitions=_tools(*caller_tools), session_id=session)
    out = await plug.hub_grant(output_items=[_call(args or {"query": "python 最新版本"})], session_id=session)
    return out["modified_kwargs"]["output_items"][0]["tool_call"]["args"]


async def test_grant_strips_forged_identity_and_binds_session(plug):
    args = await _grant(plug, args={"query": "python", "stream_id": "sess-EVIL", "_hub_grant": "x"})
    assert "stream_id" not in args and args["_hub_grant"] != "x"
    # Host 会把模型填的 stream_id 保留下来再传给工具：模拟这种情况
    res = await plug.search(**args, stream_id="sess-EVIL", chat_id="sess-EVIL")
    assert res["success"]
    assert plug.ledger.jobs("sess-A") and not plug.ledger.jobs("sess-EVIL")


async def test_missing_or_reused_grant_rejected(plug):
    res = await plug.search(query="x", stream_id="sess-A")
    assert not res["success"] and "授权" in res["error"]
    args = await _grant(plug)
    assert (await plug.search(**args))["success"]
    assert not (await plug.search(**args))["success"]   # 一次性


async def test_other_tools_untouched(plug):
    out = await plug.hub_grant(output_items=[_call({"msg_id": "1"}, name="reply")], session_id="s")
    assert out == {"action": "continue"}


async def test_subagent_detection_and_window(plug):
    plug._providers = [FakeProvider(delay=6)]   # 超过主 Planner 的 4 秒，但在子代理的 20 秒内
    args = await _grant(plug, caller_tools=("search",))
    res = await plug.search(**args)
    assert "已转为后台" not in res["content"] and "S1" in res["content"]


async def test_planner_backgrounds_and_progress_injection(plug):
    plug._providers = [FakeProvider(delay=4.6)]
    args = await _grant(plug)
    res = await plug.search(**args)
    assert "已转为后台任务" in res["content"]
    # 主 Planner 请求：注入进度；辅助请求（无工具）不注入
    out = await plug.hub_context(items=[], tool_definitions=_tools("reply", "search"), session_id="sess-A")
    texts = [it["parts"][0]["text"] for it in out["modified_kwargs"]["items"]]
    assert any("不要说“不知道”" in t for t in texts)
    out2 = await plug.hub_context(items=[], tool_definitions=[], session_id="sess-A")
    assert out2 == {"action": "continue"}
    sub = await plug.hub_context(items=[], tool_definitions=_tools("search"), session_id="sess-A")
    assert any("结果会送到主对话" in it["parts"][0]["text"] for it in sub["modified_kwargs"]["items"])
    await asyncio.sleep(3.0)
    assert len(plug.ctx.maisaka.cards) == 1 and plug.ctx.maisaka.cards[0][0] == "sess-A"


async def test_history_grant_is_scrubbed(plug):
    items = [_call({"query": "a", "_hub_grant": "secret"})]
    out = await plug.hub_context(items=items, tool_definitions=[], session_id="s")
    assert "_hub_grant" not in out["modified_kwargs"]["items"][0]["tool_call"]["args"]
    assert items[0]["tool_call"]["args"]["_hub_grant"] == "secret"   # 不改原对象


async def test_commands(plug):
    args = await _grant(plug)
    await plug.search(**args)
    await plug.cmd_jobs(stream_id="sess-A")
    await plug.cmd_trace(stream_id="sess-A", matched_groups={"job": "J1"})
    await plug.cmd_status(stream_id="sess-A")
    sent = [t for _, t in plug.ctx.send.sent]
    assert "J1" in sent[0] and "keenable ok" in sent[1] and "来源" in sent[2]


def _msg(text):
    return {"item_type": "UserMessageItem", "meta": {"item_id": "x"}, "parts": [{"type": "text", "text": text}]}


USER = _msg('<message msg_id="a" user="测试员" is_self_message="false"> 原神更新了什么')
CARD = _msg('<plugin_proactive_task id="p" plugin_id="kamionichan.search_hub"> 插件请求你主动处理一轮聊天：[后台搜索 J1 完成]')


async def test_card_triggered_turn_allows_one_followup_then_refuses(plug):
    async def grant_with(items):
        await plug.hub_context(items=items, tool_definitions=_tools("reply", "search"), session_id="sess-A")
        out = await plug.hub_grant(output_items=[_call({"query": "q" + str(len(items))})], session_id="sess-A")
        return out["modified_kwargs"]["output_items"][0]["tool_call"]["args"]

    assert (await plug.search(**await grant_with([USER])))["success"]
    assert (await plug.search(**await grant_with([USER, CARD])))["success"]      # 第一次追加：允许
    res = await plug.search(**await grant_with([USER, CARD, CARD]))
    assert not res["success"] and "本次没有发起搜索" in res["error"]
    assert (await plug.search(**await grant_with([USER, CARD, CARD, USER])))["success"]  # 新的真人消息：重置


async def test_caller_pairing_survives_interleaving(plug):
    main = dict(session_id="sess-A", selected_history_count=20, built_message_count=25, selection_reason="main")
    sub = dict(session_id="sess-A", selected_history_count=0, built_message_count=1, selection_reason="sub")
    await plug.hub_context(items=[], tool_definitions=_tools("reply", "search"), **main)
    await plug.hub_context(items=[], tool_definitions=_tools("search"), **sub)        # 子代理插进来
    out = await plug.hub_grant(output_items=[_call({"query": "a"})], **main)          # 主 Planner 的响应
    tok = out["modified_kwargs"]["output_items"][0]["tool_call"]["args"]["_hub_grant"]
    assert plug.grants._items[tok].caller == "planner"
    out = await plug.hub_grant(output_items=[_call({"query": "b"})], **sub)
    tok = out["modified_kwargs"]["output_items"][0]["tool_call"]["args"]["_hub_grant"]
    assert plug.grants._items[tok].caller == "subagent"


SUBAGENT_DEF = {"type": "function", "function": {"name": "subagent", "description": "把一个自包含的查询交给子代理。",
                                                  "parameters": {"type": "object"}}}


async def test_subagent_description_patched_only_for_main_planner(plug):
    defs = [*_tools("reply", "search"), SUBAGENT_DEF]
    out = await plug.hub_context(items=[], tool_definitions=defs, session_id="s")
    new = {d["function"]["name"]: d for d in out["modified_kwargs"]["tool_definitions"]}
    assert "直接调用 search" in new["subagent"]["function"]["description"]
    assert "直接调用 search" not in SUBAGENT_DEF["function"]["description"]       # 不改原对象
    out2 = await plug.hub_context(items=[], tool_definitions=[*_tools("reply"), SUBAGENT_DEF], session_id="s")
    assert out2 == {"action": "continue"}                                             # 本插件工具不在：不动


async def test_subagent_search_cap_per_task(plug):
    task_a = [_msg("[派发给你的任务] 查原神 7.1 更新内容")]
    task_b = [_msg("[派发给你的任务] 查 B 站新闻")]
    sub = dict(session_id="sess-A", selected_history_count=0, built_message_count=1, selection_reason="sub")

    async def sub_search(items, q):
        await plug.hub_context(items=items, tool_definitions=_tools("search"), **sub)
        out = await plug.hub_grant(output_items=[_call({"query": q})], **sub)
        return await plug.search(**out["modified_kwargs"]["output_items"][0]["tool_call"]["args"])

    for i in range(3):
        assert (await sub_search(task_a, f"a{i}"))["success"]
    res = await sub_search(task_a, "a3")
    assert not res["success"] and "达到上限" in res["error"]
    assert (await sub_search(task_b, "b0"))["success"]                               # 另一个子代理任务不受影响


# ---------------- 第 2 阶段 ----------------
async def _grant_read(plug, session="sess-A", args=None):
    await plug.hub_context(items=[], tool_definitions=_tools("reply", "search", "read_source"), session_id=session)
    out = await plug.hub_grant(output_items=[_call(args or {"ref": "S1"}, name="read_source")], session_id=session)
    return out["modified_kwargs"]["output_items"][0]["tool_call"]["args"]


class FakeChain:
    def __init__(self, text="段落一\n关于新角色的说明 上线日期 9月23日\n段落三", fail=None):
        self.text, self.fail, self.urls = text, fail, []

    async def read(self, url):
        from hubplugin.hub.reader import Page
        self.urls.append(url)
        if self.fail:
            raise self.fail
        return Page(url, "公告", self.text, "builtin")


async def test_read_source_by_sn_and_grant_is_tool_bound(plug):
    args = await _grant(plug)
    await plug.search(**args)
    plug._reader = FakeChain()
    rargs = await _grant_read(plug, args={"ref": "S2", "focus": "上线日期"})
    res = await plug.read_source(**rargs)
    assert res["success"] and "上线日期" in res["content"] and "S2" in res["content"]
    assert plug._reader.urls == ["https://s1.example.com/a"]
    # search 的授权不能拿来调用 read_source
    sargs = await _grant(plug)
    assert not (await plug.read_source(ref="S1", **{"_hub_grant": sargs["_hub_grant"]}))["success"]


async def test_read_source_unknown_sn_and_bad_ref(plug):
    plug._reader = FakeChain()
    assert "没有这个编号" in (await plug.read_source(**await _grant_read(plug, args={"ref": "S99"})))["error"]
    assert "S 编号或" in (await plug.read_source(**await _grant_read(plug, args={"ref": "file:///etc/passwd"})))["error"]


async def test_replyer_evidence_injected_before_final_instruction(plug):
    args = await _grant(plug)
    await plug.search(**args)
    items = [_msg("系统"), _msg("历史"), _msg("回复参考"), _msg("当前时间：2026-10-29"), _msg("风格要求")]
    out = await plug.hub_evidence(items=items, session_id="sess-A")
    new = out["modified_kwargs"]["items"]
    texts = [it["parts"][0]["text"] for it in new]
    i = next(i for i, t in enumerate(texts) if t.startswith("[搜索中枢 · 本轮检索到的资料"))
    assert texts[i + 1].startswith("当前时间：") and len(texts[i]) <= 1700
    assert (await plug.hub_evidence(items=items, session_id="sess-OTHER")) == {"action": "continue"}


async def test_parked_result_surfaces_once(plug):
    plug.jobs.stale_seconds = 0.0
    plug._providers = [FakeProvider(delay=4.4)]
    args = await _grant(plug)
    await plug.search(**args)
    await asyncio.sleep(1.0)
    assert plug.ctx.maisaka.cards == []                  # 过时：没有主动送回
    out = await plug.hub_context(items=[], tool_definitions=_tools("reply", "search"), session_id="sess-A")
    note = next(it["parts"][0]["text"] for it in out["modified_kwargs"]["items"] if "话题已经过去" in it["parts"][0]["text"])
    assert "S1 " in note and "s1.example.com" in note
    out2 = await plug.hub_context(items=[], tool_definitions=_tools("reply", "search"), session_id="sess-A")
    assert out2 == {"action": "continue"}                # 只提示一次


async def test_read_cap_per_human_turn_and_page_cache(plug):
    plug._reader = FakeChain()
    plug.config.read.max_reads_per_turn = 2
    turn1 = [_msg('<message msg_id="1" is_self_message="false">原神新角色？</message>')]

    async def read(items, ref):
        await plug.hub_context(items=items, tool_definitions=_tools("reply", "search", "read_source"), session_id="sess-A")
        out = await plug.hub_grant(output_items=[_call({"ref": ref}, name="read_source")], session_id="sess-A")
        return await plug.read_source(**out["modified_kwargs"]["output_items"][0]["tool_call"]["args"])

    assert (await read(turn1, "https://a.example.com/x"))["success"]
    assert (await read(turn1, "https://a.example.com/x"))["success"]
    assert plug._reader.urls == ["https://a.example.com/x"]          # 第二次命中缓存
    assert "达到上限" in (await read(turn1, "https://b.example.com/y"))["error"]
    turn2 = turn1 + [_msg('<message msg_id="2" is_self_message="false">那新区域呢？</message>')]
    assert (await read(turn2, "https://b.example.com/y"))["success"]  # 新问题重新计数


# ---------------- 第 3 阶段：插件层 ----------------
def test_build_providers_enables_by_key(plug):
    cfg = P.HubConfig()
    names0 = [p.info.name for p in P.build_providers(cfg, plug._http, plug.ctx)]
    assert names0 == ["keenable", "tavily", "host_web"]
    cfg.sources.bocha.api_key = "b"
    cfg.sources.qwen.api_key = "q"
    cfg.sources.searxng.base_url = "http://127.0.0.1:8080"
    cfg.sources.exa.api_key = "e"
    cfg.sources.exa.enabled = False
    names1 = [p.info.name for p in P.build_providers(cfg, plug._http, plug.ctx)]
    assert {"bocha", "qwen", "searxng"} <= set(names1) and "exa" not in names1


async def test_result_cache_hit_returns_without_calling(plug):
    calls = []

    class Counting(FakeProvider):
        async def search(self, q, *, max_results):
            calls.append(q)
            return await super().search(q, max_results=max_results)

    plug._providers = [Counting()]
    q = {"query": "python 最新 版本", "depth": "quick"}
    r1 = await plug.search(**await _grant(plug, args=q))
    r2 = await plug.search(**await _grant(plug, args=q))
    assert r1["success"] and r2["success"] and len(calls) == 1
    assert " · 缓存" not in r1["content"] and " · 缓存" in r2["content"]
    r3 = await plug.search(**await _grant(plug, args={**q, "freshness": "today"}))   # 不同时效档不共用缓存
    assert len(calls) == 2 and " · 缓存" not in r3["content"]


async def test_deep_enrich_reads_top_results(plug):
    plug._reader = FakeChain(text="无关段落\n缓存 测试 的正文：这里有具体细节 9月23日\n结尾")
    plug.config.search.deep_read_top = 2
    res = await plug.search(**await _grant(plug, args={"query": "缓存 测试", "depth": "deep"}))
    assert res["success"] and "（已读正文）" in res["content"] and "9月23日" in res["content"]
    assert len(plug._reader.urls) == 2


def test_status_lists_idle_keyed_sources(plug):
    text = plug._status_text()
    assert "未启用" in text and "博查" in text and "Exa" in text


def test_migrate_old_sources_layout_keeps_keys():
    old = {"plugin": {"config_version": "0.1.0", "enabled": True},
           "sources": {"tavily": {"enabled": True, "api_key": "tvly-OLD"}, "keenable": {"enabled": False}}}
    new = P.migrate_sources_layout(old)
    assert "sources" not in new and new["tavily"]["api_key"] == "tvly-OLD" and new["keenable"]["enabled"] is False
    p = P.SearchHubPlugin()
    cfg, changed = p.normalize_plugin_config(old)
    assert changed and cfg["tavily"]["api_key"] == "tvly-OLD" and "bocha" in cfg


# ---------------- 第 4 阶段：以图搜（插件层） ----------------
def _png(color=(200, 50, 50)):
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), color).save(buf, "PNG")
    return buf.getvalue()


class FakeContext:
    def __init__(self, images):
        self.images, self.calls = images, []

    async def resolve_image(self, stream_id, source_id, index=0):
        import base64
        self.calls.append((stream_id, source_id, index))
        data = self.images.get((stream_id, source_id, index))
        if data is None:
            return {"success": False, "error": f"没有找到消息：msg_id={source_id}"}
        return {"success": True, "image": {"binary_data_base64": base64.b64encode(data).decode(), "binary_hash": "h"}}


class FakeImageEngine:
    def __init__(self, name, level="hit", kind="scene"):
        from hub.providers import ProviderInfo
        self.info = ProviderInfo(name, name, "medium", False, True, False, extra={"image": True})
        self.level, self.kind, self.jpegs = level, kind, []

    async def search_image(self, jpeg, *, max_results=3):
        from hub.image_sources import ImageHit
        self.jpegs.append(jpeg)
        return [ImageHit("请问您今天要来点兔子吗", "https://anilist.co/anime/21034", "第 1 集；约 4:39 处", self.kind,
                         0.99, self.level, self.info.name)]


async def test_image_search_uses_granted_session_only(plug):
    plug.ctx.maisaka.context = FakeContext({("sess-A", "m12", 1): _png()})
    eng = FakeImageEngine("tracemoe")
    plug._image_providers = [eng]
    args = await _grant(plug, args={"image": "m12", "image_index": 1, "intent": "identify_scene", "stream_id": "sess-EVIL"})
    res = await plug.search(**args, stream_id="sess-EVIL")
    assert res["success"] and plug.ctx.maisaka.context.calls == [("sess-A", "m12", 1)]
    assert "[命中·番剧截图]" in res["content"] and "trace.moe" in res["content"] and "分析式措辞" in res["content"]
    assert "不代表是原作者或首发" in res["content"]
    assert eng.jpegs[0][:2] == b"\xff\xd8"                    # 外发的是重新编码的 JPEG，不是原始 PNG


async def test_image_input_errors_are_not_search_failures(plug):
    plug.ctx.maisaka.context = FakeContext({})
    plug._image_providers = [FakeImageEngine("tracemoe")]
    res = await plug.search(**await _grant(plug, args={"image": "m404", "intent": "identify_scene"}))
    assert not res["success"] and "没能拿到图片" in res["error"] and "不是搜索失败" in res["error"]
    res2 = await plug.search(**await _grant(plug, args={"intent": "identify_scene"}))
    assert not res2["success"] and "image" in res2["error"]


async def test_image_disabled_and_weak_result_wording(plug):
    plug.ctx.maisaka.context = FakeContext({("sess-A", "m1", 0): _png()})
    plug._image_providers = [FakeImageEngine("tracemoe", level="weak")]
    res = await plug.search(**await _grant(plug, args={"image": "m1", "intent": "identify_scene"}))
    assert "[弱线索" in res["content"] and "没有找到确定的出处" in res["content"]
    plug.config.search.allow_text_outbound = False          # 只关文字搜索：以图搜照常
    res_img = await plug.search(**await _grant(plug, args={"image": "m1", "intent": "identify_scene"}))
    assert res_img["success"]
    res_txt = await plug.search(**await _grant(plug, args={"query": "python"}))
    assert not res_txt["success"] and "文字搜索" in res_txt["error"]
    plug.config.image.enabled = False
    res2 = await plug.search(**await _grant(plug, args={"image": "m1"}))
    assert not res2["success"] and "以图搜" in res2["error"]


async def test_same_image_twice_merges_while_running(plug):
    plug.ctx.maisaka.context = FakeContext({("sess-A", "m1", 0): _png(), ("sess-A", "m2", 0): _png()})

    class Slow(FakeImageEngine):
        async def search_image(self, jpeg, *, max_results=3):
            await asyncio.sleep(5)
            return await super().search_image(jpeg, max_results=max_results)

    plug._image_providers = [Slow("tracemoe")]
    plug.config.search.sync_window_seconds = 1.0
    r1 = await plug.search(**await _grant(plug, args={"image": "m1", "intent": "identify_scene"}))
    r2 = await plug.search(**await _grant(plug, args={"image": "m2", "intent": "identify_scene"}))   # 同一张图
    assert "已转为后台任务" in r1["content"] and "合并" in r2["content"]


# ---------------- 第 5 阶段：共存、改名、状态、配置页控件 ----------------
_REMINDER = ("<system-reminder>\n以下工具当前未直接暴露给你，但可以通过 tool_search 工具发现并在后续轮次中使用：\n"
             "1. web_search: 联网搜索\n2. mute_user: 禁言\n\n如需其中某个工具，请先调用 tool_search。\n</system-reminder>")


def _u(text):
    return {"item_type": "UserMessageItem", "meta": {}, "parts": [{"type": "text", "text": text}]}


async def test_hook_hides_host_tools_only_with_our_tools(plug):
    out = await plug.hub_context(items=[_u(_REMINDER)], tool_definitions=_tools("reply", "search", "web_fetch"),
                                 session_id="sess-A")
    mk = out["modified_kwargs"]
    assert [d["function"]["name"] for d in mk["tool_definitions"]] == ["reply", "search"]
    assert "web_search" not in mk["items"][0]["parts"][0]["text"] and "mute_user" in mk["items"][0]["parts"][0]["text"]
    assert "最近隐藏：web_fetch、web_search" in plug._status_text()
    # condenser 等不含本插件工具的请求：不动
    out2 = await plug.hub_context(items=[_u(_REMINDER)], tool_definitions=[], session_id="sess-A")
    assert out2 == {"action": "continue"}
    # 管理员改成 off：不隐藏
    plug.config.coexist.host_tools_mode = "不处理"
    plug._apply_config()
    out3 = await plug.hub_context(items=[_u(_REMINDER)], tool_definitions=_tools("reply", "search", "web_fetch"),
                                  session_id="sess-A")
    assert out3 == {"action": "continue"}


async def test_observed_results_enter_ledger_and_are_readable(plug):
    items = [{"item_type": "FunctionCallItem", "tool_call": {"call_id": "t1", "func_name": "tavily_web_search", "args": {}}},
             {"item_type": "FunctionCallOutputItem", "call_id": "t1", "tool_name": "tavily_web_search", "success": True,
              "output": "1. [Go 1.26 发布说明](https://go.dev/doc/go1.26) 新特性……"}]
    await plug.hub_context(items=items, tool_definitions=_tools("reply", "search", "tavily_web_search"), session_id="sess-A")
    e = plug.ledger.get("sess-A", 1)
    assert e and e["url"] == "https://go.dev/doc/go1.26" and "other:tavily_web_search" in e["sources"]
    plug._reader = FakeChain()
    args = await _grant_read(plug, args={"ref": "S1"})
    res = await plug.read_source(**args)
    assert res["success"] and "go.dev" in res["content"]
    status = plug._status_text()
    assert "最近观察：tavily_web_search" in status and "共收录 1 条" in status


async def test_renamed_tools_are_rewritten_in_outputs(plug):
    renamed = ("reply", "plugin_kamionichan_search_hub_search", "plugin_kamionichan_search_hub_read_source", "search")
    await plug.hub_context(items=[], tool_definitions=_tools(*renamed), session_id="sess-A")
    out = await plug.hub_grant(output_items=[_call({"query": "python 最新版本"}, name="plugin_kamionichan_search_hub_search")],
                               session_id="sess-A")
    args = out["modified_kwargs"]["output_items"][0]["tool_call"]["args"]
    res = await plug.search(**args)
    assert res["success"]
    fixed = plug._fix_names({"content": "用 read_source 读取 S3；再调用 search（换个说法）。read_sources 不变", "x": "read_source "})
    assert fixed["content"] == ("用 plugin_kamionichan_search_hub_read_source 读取 S3；再调用 plugin_kamionichan_search_hub_search（"
                                "换个说法）。read_sources 不变") and fixed["x"] == "read_source "
    assert "被 Host 改名" in plug._status_text()


async def test_old_plugin_detection_and_widgets(plug):
    for shape in ({"success": True, "plugins": ["kamionichan.search", "kamionichan.search_hub"]},
                  ["builtin.plugin-management", "kamionichan.search", "kamionichan.search_hub"]):   # SDK 2.19 实际返回列表
        plug._old_plugin_hint = ""

        async def loaded(shape=shape):
            return shape
        plug.ctx.component = SimpleNamespace(list_loaded_plugins=loaded)
        await plug._check_old_plugin()
        assert "kamionichan.search" in plug._status_text()
    st = await plug.get_config_widget_state()
    assert set(st) == {"hub_status", "clear_all"} and st["hub_status"].tone == "warning"

    async def after_disable():
        return ["builtin.plugin-management", "kamionichan.search_hub"]
    plug.ctx.component = SimpleNamespace(list_loaded_plugins=after_disable)
    st_off = await plug.get_config_widget_state()
    assert "旧插件" not in st_off["hub_status"].text and "旧插件" not in plug._status_text()
    plug.ledger.record("sess-A", [P.Merged("k", "https://a.example.com/", "t", "s", None, ["x"])])
    await plug.on_config_widget_action("clear_all")
    assert plug.ledger.get("sess-A", 1) is None
    st2 = await plug.get_config_widget_state()
    assert "已于" in st2["clear_all"].text
    with pytest.raises(ValueError):
        await plug.on_config_widget_action("nope")
