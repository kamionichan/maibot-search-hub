"""第 5 阶段：L4 共存（隐藏、观察）、改名、旧插件提示、配置页控件。"""

import json

import pytest

from hub import coexist
from hub.coexist import CoexistPolicy, extract_results, hide_tools, observe, strip_deferred_reminder


def tdef(name):
    return {"type": "function", "function": {"name": name, "description": "d", "parameters": {"type": "object"}}}


REMINDER = ("<system-reminder>\n以下工具当前未直接暴露给你，但可以通过 tool_search 工具发现并在后续轮次中使用：\n"
            "1. web_search: 联网搜索\n2. mute_user: 禁言\n3. web_fetch: 读取网页\n\n"
            "如需其中某个工具，请先调用 tool_search。tool_search 只负责发现工具，不直接执行。\n</system-reminder>")


def umsg(text):
    return {"item_type": "UserMessageItem", "meta": {}, "parts": [{"type": "text", "text": text}]}


def test_policy_defaults():
    p = CoexistPolicy()
    assert p.mode_for("web_search") == "hide" and p.mode_for("web_fetch") == "hide"
    assert p.mode_for("tavily_web_search") == "observe" and p.mode_for("text_web_search") == "observe"
    assert p.mode_for("mute_user") == "off"
    p2 = CoexistPolicy("off", "hide", extra_tools=("my_search",), ignore_tools=("x_search",))
    assert p2.mode_for("my_search") == "hide" and p2.mode_for("x_search") == "off" and p2.mode_for("web_search") == "off"


def test_hide_tools_and_reminder():
    defs, hidden = hide_tools([tdef("reply"), tdef("search"), tdef("web_search"), tdef("tavily_web_search")], CoexistPolicy())
    assert [d["function"]["name"] for d in defs] == ["reply", "search", "tavily_web_search"] and hidden == ["web_search"]
    items, changed = strip_deferred_reminder([umsg("你好"), umsg(REMINDER)], ["web_search", "web_fetch"])
    assert changed == ["web_search", "web_fetch"] and items[0]["parts"][0]["text"] == "你好"
    text = items[1]["parts"][0]["text"]
    assert "web_search" not in text and "web_fetch" not in text and "1. mute_user: 禁言" in text
    assert text.startswith("<system-reminder>") and text.endswith("</system-reminder>")
    only = REMINDER.replace("2. mute_user: 禁言\n", "").replace("3. web_fetch", "2. web_fetch")
    items2, changed2 = strip_deferred_reminder([umsg(only)], ["web_search", "web_fetch"])
    assert changed2 == ["web_search", "web_fetch"] and items2 == []                                  # 删光了就整条去掉
    same, ch = strip_deferred_reminder([umsg("1. web_search: 普通消息里提到")], ["web_search"])
    assert ch == []                                                   # 不是 Host 提醒，不碰


def test_extract_host_web_search_truncated_json():
    payload = {"query": "q", "results": [
        {"title": "Rust 1.91 发布", "url": "https://blog.rust-lang.org/2026/10/30/Rust-1.91.0/", "snippet": "今天发布……",
         "summary": "", "published_at": None},
        {"title": "第二条", "url": "https://example.com/b?utm_source=x", "snippet": "摘要二"}]}
    raw = json.dumps(payload, ensure_ascii=False, indent=2)
    out = "搜索：q\n[外部网页资料]\n" + raw[: raw.index("摘要二")] + '\n原文编号：r1。'
    got = extract_results("web_search", out)
    assert [m.title for m in got] == ["Rust 1.91 发布", "第二条"] and got[0].snippet == "今天发布……"
    assert got[0].sources == ["other:web_search"]


def test_extract_fetch_markdown_and_bare_urls():
    f = extract_results("web_fetch", "Rust Blog\nhttps://blog.rust-lang.org/\n[外部网页资料]\n正文……")
    assert len(f) == 1 and f[0].title == "Rust Blog" and "正文" in f[0].snippet
    md = extract_results("tavily_web_search", "1. [原神 6.2 版本](https://ys.mihoyo.com/news/1) — 新角色上线\n2. 无链接")
    assert md[0].url == "https://ys.mihoyo.com/news/1" and md[0].title == "原神 6.2 版本"
    bare = extract_results("x_search", "参考 https://a.example.org/p1。另见 https://a.example.org/p1 和 javascript:alert(1)")
    assert [m.url for m in bare] == ["https://a.example.org/p1"]
    assert extract_results("x_search", "") == [] and extract_results("x_search", "没有链接") == []


def test_observe_only_new_successful_calls():
    items = [
        {"item_type": "FunctionCallItem", "tool_call": {"call_id": "c1", "func_name": "tavily_web_search", "args": {"q": "x"}}},
        {"item_type": "FunctionCallOutputItem", "call_id": "c1", "tool_name": "tavily_web_search", "success": True,
         "output": "[标题](https://example.com/a)"},
        {"item_type": "FunctionCallOutputItem", "call_id": "c2", "tool_name": "tavily_web_search", "success": False,
         "output": "https://example.com/err"},
        {"item_type": "FunctionCallOutputItem", "call_id": "c3", "tool_name": "mute_user", "success": True,
         "output": "https://example.com/not-search"},
        {"item_type": "FunctionCallOutputItem", "call_id": "c4", "tool_name": "web_search", "success": True,
         "output": "https://example.com/hidden-host"},     # Host 工具默认隐藏，不观察
    ]
    seen: set[str] = set()
    got = observe(items, CoexistPolicy(), seen)
    assert [(n, [m.url for m in ms]) for n, ms in got] == [("tavily_web_search", ["https://example.com/a"])]
    assert observe(items, CoexistPolicy(), seen) == []                # 同一个 call 只收一次
    got2 = observe(items, CoexistPolicy(host_mode="observe"), set())
    assert {n for n, _ in got2} == {"tavily_web_search", "web_search"}


def test_old_plugin_hint():
    assert "kamionichan.search" in coexist.old_plugin_hint(["a", "kamionichan.search", "kamionichan.search_hub"])
    assert coexist.old_plugin_hint(["kamionichan.search_hub"]) == ""
