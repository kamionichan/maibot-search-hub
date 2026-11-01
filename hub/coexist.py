"""L4 共存管理（第 5 阶段，D2、D43、IMPLEMENTATION_PLAN 第 2、5、11 条）。

三种模式：
- 观察（observe）：读取历史里其他搜索工具的输出，提取网址、标题、摘要收进账本，
  之后 read_source、Replyer 证据注入都能用到。只读，不改请求。
- 隐藏（hide）：从本次请求的 tool_definitions 中删掉重复的搜索工具。只影响提示，不是强制拦截，每轮都要重做。
  Host 的 deferred 提醒在 hook 之前就已经拼进消息，所以还要从那段提醒里删掉对应的行，否则模型会去 tool_search。
- 关闭（off）：什么都不做。

"接管"（after_response 改写模型的调用）按 ARCHITECTURE 默认关闭、标为实验功能，第一版不做。

默认值：Host 自带的 web_search / web_fetch 隐藏（D43：本插件已经通过 ctx.web 用的是同一个服务）；
其他插件的搜索工具观察。只在**主 Planner** 且本插件工具也在本次请求里时才动手（0.5.2，F11）：
子代理的工具是主 Planner 明确授权给它的，不能替它隐藏；condenser 等辅助请求一律不改。

工具改名（0.5.2，K1/F11）：跨来源重名时 Host 把后注册的改名为 `{provider_type}_{来源}_{原名}`
（src/core/tooling.py `_list_routes`；插件为 `plugin_<plugin_id>_`，MCP 为 `mcp_<server>_`）。
Host 的工具先于插件注册，所以 Host 的 web_search 保持原名，插件的同名工具被改名。
这里按这个格式还原原名再判断。超过 64 字符时 Host 会截断并加哈希，那种名字认不出来，只能由管理员写进 extra_tools。
注意：两个插件之间重名时，Host 直接丢弃后一个（component_query.get_llm_available_tool_specs），不会改名。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from .merge import Merged, normalize_url

HOST_TOOLS = ("web_search", "web_fetch")
OLD_PLUGIN_ID = "kamionichan.search"
OLD_PLUGIN_TOOLS = ("text_web_search", "image_search_engines", "image_publication_search")

# 市场插件里出现过的搜索/读取类工具名（MARKET_SURVEY；docs 中有出处），用于默认识别
KNOWN_SEARCH_TOOLS = frozenset({
    "web_search", "web_fetch", "search_web", "fetch_url", "fetch_page", "search_and_summarize",
    "tavily_web_search", "doubao_web_search", "deepseek_search", "deepseek_fetch", "baidu_web_search",
    "anysearch_search", "anysearch_extract", "anysearch_batch_search", "qwen_web_search", "grounded_search",
    "search_query", "x_search", "image_search", "doubao_image_search", "recent_image_search",
    *OLD_PLUGIN_TOOLS,
})
_URL = re.compile(r"https?://[^\s<>\"'`）)\]】，。；]+", re.I)
_MD_LINK = re.compile(r"\[([^\]\n]{2,160})\]\((https?://[^)\s]+)\)")
MODES = ("observe", "hide", "off")
_RENAMED = re.compile(r"^(?:plugin|mcp)_[A-Za-z0-9_-]+?_(" + "|".join(
    sorted((re.escape(n) for n in KNOWN_SEARCH_TOOLS), key=len, reverse=True)) + r")$")


def original_name(name: str) -> str:
    """被 Host 改名的已知搜索工具 → 原名；其他名字原样返回。Host 自带工具不会被改名，所以只认插件和 MCP 前缀。"""
    m = _RENAMED.match(name)
    return m.group(1) if m else name


@dataclass
class CoexistPolicy:
    host_mode: str = "hide"
    other_mode: str = "observe"
    extra_tools: tuple[str, ...] = ()            # 管理员追加的工具名
    ignore_tools: tuple[str, ...] = ()           # 管理员排除的工具名

    def mode_for(self, name: str) -> str:
        base = original_name(name)
        if name in self.ignore_tools or base in self.ignore_tools:
            return "off"
        if name in self.extra_tools:
            return self.other_mode
        if name in HOST_TOOLS:   # 原名的 web_search 一定是 Host 自带的（插件同名会被改名）
            return self.host_mode
        if base in KNOWN_SEARCH_TOOLS or base in self.extra_tools:
            return self.other_mode
        return "off"


@dataclass
class CoexistReport:
    hidden: list[str] = field(default_factory=list)
    observed: list[str] = field(default_factory=list)
    seen_tools: list[str] = field(default_factory=list)   # 本次请求里出现的其他搜索工具


def split_names(text: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(n.strip() for n in re.split(r"[,，\s]+", text or "") if n.strip()))


def _fn_name(tool_def: Any) -> str:
    if isinstance(tool_def, dict):
        fn = tool_def.get("function") if isinstance(tool_def.get("function"), dict) else tool_def
        n = fn.get("name") if isinstance(fn, dict) else None
        return n if isinstance(n, str) else ""
    return ""


def hide_tools(defs: list[Any], policy: CoexistPolicy) -> tuple[list[Any], list[str]]:
    keep, hidden = [], []
    for d in defs:
        n = _fn_name(d)
        if n and policy.mode_for(n) == "hide":
            hidden.append(n)
        else:
            keep.append(d)
    return keep, hidden


_REMINDER_LINE = re.compile(r"^(\d+)\.\s([A-Za-z0-9_\-.:]+)(?::\s|$)")
_REMINDER_HEAD = "<system-reminder>\n以下工具当前未直接暴露给你，但可以通过 tool_search 工具发现"
_REMINDER_TAIL = "\n</system-reminder>"


def strip_deferred_reminder(items: list[Any], names: Iterable[str] | Callable[[str], bool]) -> tuple[list[Any], list[str]]:
    """从 Host 的 deferred 提醒（<system-reminder> … tool_search …）中删掉被隐藏的工具行。

    依据 1.3.18 `runtime.build_deferred_tools_reminder` 的格式（0.5.2 收紧，F11）：
    整条消息必须以标准开头起、以 `</system-reminder>` 结尾；只改第二行之后、第一个空行之前的工具列表区。
    格式对不上就原样保留，不猜。删光了就整条提醒删掉。
    names：要删的确切工具名，或判断函数（插件传 policy.mode_for(n) == "hide"，改名后的工具按各自模式判断）。
    """
    if callable(names):
        hide = names
    else:
        wanted = set(names)
        if not wanted:
            return items, []
        hide = wanted.__contains__
    out: list[Any] = []
    removed_names: list[str] = []
    for it in items:
        parts = it.get("parts") if isinstance(it, dict) and it.get("item_type") == "UserMessageItem" else None
        if not isinstance(parts, list) or len(parts) != 1 or not isinstance(parts[0], dict):
            out.append(it)
            continue
        text = parts[0].get("text")
        if not isinstance(text, str) or not text.startswith(_REMINDER_HEAD) or not text.endswith(_REMINDER_TAIL):
            out.append(it)
            continue
        lines = text.split("\n")
        try:
            end = lines.index("", 2)        # 工具列表区：第 3 行到第一个空行
        except ValueError:
            out.append(it)
            continue
        region = lines[2:end]
        if not region or not all(_REMINDER_LINE.match(ln) for ln in region):
            out.append(it)
            continue
        kept, removed = [], 0
        for ln in region:
            m = _REMINDER_LINE.match(ln)
            if m and hide(m.group(2)):
                removed += 1
                removed_names.append(m.group(2))
                continue
            kept.append(ln)
        if not removed:
            out.append(it)
            continue
        if not kept:
            continue
        # 重新编号，保持格式与 Host 一致
        renum = [f"{i}." + ln[_REMINDER_LINE.match(ln).end(1) + 1:] for i, ln in enumerate(kept, start=1)]  # type: ignore[union-attr]
        new = dict(it)
        new["parts"] = [{**parts[0], "text": "\n".join([*lines[:2], *renum, *lines[end:]])}]
        out.append(new)
    return out, removed_names


def extract_results(tool_name: str, output: Any, call_args: dict | None = None) -> list[Merged]:
    """从其他搜索工具的输出里提取 (网址, 标题, 摘要)。

    - Host web_search：输出是 "搜索：…\n[外部网页资料]\n{JSON 预览}"，JSON 可能被截断，逐条用正则兜底。
    - Host web_fetch：第一行标题、第二行网址。
    - 其他插件：Markdown 链接或裸网址，取网址所在行作摘要。
    只收公开 http(s) 网址；最多 8 条；不保存整页正文（D31）。
    """
    text = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False) if output else ""
    if not text:
        return []
    src = f"other:{tool_name}"
    found: dict[str, Merged] = {}

    def add(url: str, title: str, snippet: str) -> None:
        url = url.rstrip(".,;:!?")
        if not url.lower().startswith(("http://", "https://")):
            return
        try:
            k = normalize_url(url)
        except ValueError:
            return
        if not k or k in found or len(found) >= 8:
            return
        found[k] = Merged(k, url, " ".join(title.split())[:120] or url, " ".join(snippet.split())[:300], None, [src])

    if tool_name == "web_fetch":
        lines = text.split("\n", 2)
        if len(lines) >= 2 and lines[1].startswith(("http://", "https://")):
            body = lines[2] if len(lines) > 2 else ""
            add(lines[1].strip(), lines[0], body.replace("[外部网页资料]", "")[:300])
        return list(found.values())
    # JSON 结果（Host web_search 与不少插件）：逐个对象用正则取，截断的 JSON 也能用
    for m in re.finditer(r'"title"\s*:\s*"((?:[^"\\]|\\.)*)"\s*,\s*"url"\s*:\s*"((?:[^"\\]|\\.)*)"'
                         r'(?:\s*,\s*"snippet"\s*:\s*"((?:[^"\\]|\\.)*)")?', text):
        try:
            title, url, snip = (json.loads(f'"{g}"') if g else "" for g in m.groups())
        except ValueError:
            continue
        add(url, title, snip)
    for m in _MD_LINK.finditer(text):
        line = text[text.rfind("\n", 0, m.start()) + 1: text.find("\n", m.end()) if "\n" in text[m.end():] else len(text)]
        add(m.group(2), m.group(1), _MD_LINK.sub(r"\1", line))
    if not found:
        for m in _URL.finditer(text):
            s, e = text.rfind("\n", 0, m.start()) + 1, text.find("\n", m.end())
            line = text[s: e if e >= 0 else len(text)]
            add(m.group(0), line.replace(m.group(0), "").strip(" -：:|") or m.group(0), line)
    return list(found.values())


def observe(items: list[Any], policy: CoexistPolicy, seen: set[str]) -> list[tuple[str, list[Merged]]]:
    """找出历史里尚未收录的其他搜索工具输出。seen 记录已处理的 call_id（调用方按会话保存）。"""
    names: dict[str, tuple[str, dict]] = {}
    for it in items:
        tc = it.get("tool_call") if isinstance(it, dict) else None
        if isinstance(tc, dict) and isinstance(tc.get("call_id"), str):
            args = tc.get("args")
            names[tc["call_id"]] = (str(tc.get("func_name") or ""), args if isinstance(args, dict) else {})
    out = []
    for it in items:
        if not isinstance(it, dict) or it.get("item_type") != "FunctionCallOutputItem" or not it.get("success", True):
            continue
        cid = it.get("call_id")
        if not isinstance(cid, str) or cid in seen:
            continue
        name = str(it.get("tool_name") or names.get(cid, ("", {}))[0])
        if policy.mode_for(name) != "observe":
            continue
        seen.add(cid)
        got = extract_results(name, it.get("output"), names.get(cid, ("", {}))[1])
        if got:
            out.append((name, got))
    return out


def old_plugin_hint(loaded: Iterable[str]) -> str:
    if OLD_PLUGIN_ID in set(loaded):
        return (f"检测到旧插件 {OLD_PLUGIN_ID}（MaiBot 聚合搜索）仍在运行：两个插件会抢着搜索。"
                "建议在 WebUI 插件管理里停用它；它的工具在停用前按“观察”处理。")
    return ""
