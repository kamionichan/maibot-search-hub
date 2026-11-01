"""插件配置（D35/D39：能力默认开启，是否可用由 key / 地址自然决定）。

WebUI 展示规则（已核实 MaiBot 1.3.18 dashboard `routes/plugin-config.tsx`、SDK 2.19 `config.py`）：
- 字段标题用 `label`，说明文字用 `hint`；`description` 不显示，只留作代码文档。
- 配置节标题用 `__ui_label__`，标题下的说明用类的 docstring。
- 下拉框选项来自 `Literal[...]`，按原值显示，所以选项直接用中文。
- 标签页布局与默认折叠由 `decorate_schema()` 加到 schema 上（插件覆盖 get_webui_config_schema）。
  标签页布局下只渲染列在标签页里的配置节，所以每个配置节都必须出现在 CONFIG_TABS 中（有单测检查）。

TOML 里的配置节和键名与 0.5.0 完全相同（见 migrate_config 的说明：Host 升级配置时会丢掉挪了位置的字段）。
只有共存模式的选项值从英文改为中文，旧值由 CoexistSection 的校验器自动转换。
"""

from __future__ import annotations

import copy
from typing import Any, Literal, Mapping

from pydantic import ConfigDict, field_validator

from maibot_sdk import Field, PluginConfigBase


def F(default: Any, label: str, hint: str = "", *, secret: bool = False, placeholder: str = "",
      hidden: bool = False, **kw: Any) -> Any:
    """带中文标题和说明的配置字段。"""
    extra: dict[str, Any] = {"label": label}
    if hint:
        extra["hint"] = hint
    if placeholder:
        extra["placeholder"] = placeholder
    if hidden:
        extra["hidden"] = True
    if secret:
        extra.update({"input_type": "password", "x-widget": "password"})
        kw.setdefault("repr", False)
    return Field(default=default, description=hint or label, json_schema_extra=extra, **kw)


# ---------------------------------------------------------------- 常用
class PluginSection(PluginConfigBase):
    """插件开关。"""
    __ui_label__ = "插件"
    config_version: str = F("0.5.2", "配置结构版本", hidden=True)
    enabled: bool = F(True, "启用搜索中枢")


class SearchSection(PluginConfigBase):
    """搜索来源由插件按问题自动挑选；想指定顺序时再填“优先使用的来源”。"""
    __ui_label__ = "文字搜索"
    model_config = ConfigDict(validate_assignment=True, extra="ignore")

    allow_text_outbound: bool = F(True, "允许文字搜索", "关闭后，麦麦不会再把搜索词发给任何搜索服务（以图搜图另有开关）")
    proxy: str = F("", "HTTP 代理", "文字搜索和 Yandex、Bing 都会走这个代理；留空直连", placeholder="例：http://127.0.0.1:7890")
    priority: str = F("", "优先使用的来源（可选）", "留空按问题的语言自动排序。填来源的英文名，用英文逗号分隔",
                      placeholder="例：baidu,exa")
    cache_enabled: bool = F(True, "缓存搜索结果", "短时间内问同样的问题，直接用上次的结果")
    max_results_per_source: int = F(5, "每个来源返回条数", "条数越多越全，回复也越长", ge=1, le=10)
    # ---- 等待与并发：TOML 仍在 [search] 下（0.5.0 的配置无需改动）；配置页上由 decorate_schema 拆到「高级」 ----
    sync_window_seconds: float = F(4.0, "转后台前等待（秒）", "超过这个时间，搜索转到后台，麦麦先继续聊天，查到后自动发回对话",
                                   ge=1.0, le=20.0)
    job_timeout_seconds: float = F(60.0, "单次搜索时限（秒）", "以图搜的时限自动放宽到 2 倍", ge=10.0, le=300.0)
    stale_minutes: float = F(15.0, "后台结果过时时间（分钟）", "超过后不再主动发回，等下次有人问到相关内容时再带出",
                             ge=1.0, le=240.0)
    per_session_running: int = F(2, "每个聊天同时进行的后台搜索（个）", ge=1, le=5)
    per_session_queue: int = F(2, "每个聊天排队等待的后台搜索（个）", "超出的搜索会被拒绝", ge=0, le=5)
    deep_width: int = F(3, "深度搜索同时查询的来源（个）", ge=2, le=5)
    deep_read_top: int = F(3, "深度搜索自动读取原文（篇）", "填 0 不读", ge=0, le=5)
    subagent_sync_window_seconds: float = F(20.0, "子代理转后台前等待（秒）", "子代理是麦麦派到后台独立做事的助手，它调用搜索时用这个等待时间",
                                            ge=1.0, le=50.0)
    subagent_max_searches: int = F(3, "子代理每个任务最多搜索（次）", "超过后提示它用已有结果总结，避免一个任务连搜十几次",
                                   ge=1, le=20)


class CoexistSection(PluginConfigBase):
    """怎样对待麦麦自带的联网工具和其他插件的搜索工具。"""
    __ui_label__ = "与其他搜索工具共存"
    host_tools_mode: Literal["隐藏", "观察", "不处理"] = F(
        "隐藏", "麦麦自带的联网工具",
        "推荐“隐藏”：本插件已经在用同一个联网服务，不必让麦麦再看到 web_search、web_fetch")
    other_tools_mode: Literal["观察", "隐藏", "不处理"] = F(
        "观察", "其他插件的搜索工具",
        "推荐“观察”：不干预它们，只把它们搜到的网址收进本插件，之后麦麦可以读取原文")
    extra_tools: str = F("", "额外的搜索工具（可选）", "本插件没认出来的搜索工具，按“其他插件的搜索工具”处理", placeholder="例：my_search")
    ignore_tools: str = F("", "不处理的工具（可选）", "这些工具保持原样，优先于上面两项", placeholder="例：x_search")

    @field_validator("host_tools_mode", "other_tools_mode", mode="before")
    @classmethod
    def _legacy_mode(cls, v: Any) -> Any:
        # 0.5.0 用英文值（hide/observe/off）
        return {"hide": "隐藏", "observe": "观察", "off": "不处理"}.get(v, v) if isinstance(v, str) else v


MODE_VALUE = {"隐藏": "hide", "观察": "observe", "不处理": "off"}


# ---------------------------------------------------------------- 免费来源
class KeenableSection(PluginConfigBase):
    """不填 Key 也能用（每个 IP 每小时 1000 次）；填 Key 后每月 10 万次。"""
    __ui_label__ = "Keenable"
    enabled: bool = F(True, "启用")
    api_key: str = F("", "API Key（可选）", "申请：https://keenable.ai", secret=True)


class TavilySection(PluginConfigBase):
    """不填 Key 也能用（免 Key 模式，限流不公开）；填 Key 后每月 1000 次。"""
    __ui_label__ = "Tavily"
    enabled: bool = F(True, "启用")
    api_key: str = F("", "API Key（可选）", "申请：https://app.tavily.com", secret=True)
    daily_limit: int = F(33, "每日上限（次）", "填了 Key 后生效，填 0 不限", ge=0, le=100000)


class HostWebSection(PluginConfigBase):
    """麦麦自己配置的联网服务（模型管理 → 联网服务）。没配置时自动跳过。"""
    __ui_label__ = "麦麦联网服务"
    enabled: bool = F(True, "作为来源之一")
    backend: str = F("", "联网服务的厂商（可选）", "避免和本插件里同一家的来源重复搜索；留空自动猜测",
                      placeholder="tavily")


class SearxngSection(PluginConfigBase):
    """自己部署的 SearXNG 元搜索。填了地址才启用。"""
    __ui_label__ = "SearXNG（自建）"
    enabled: bool = F(True, "启用")
    base_url: str = F("", "SearXNG 地址", "需要在 settings.yml 里开启 json 格式", placeholder="http://127.0.0.1:8080")
    engines: str = F("", "只用这些引擎（可选）", "用英文逗号分隔；留空用 SearXNG 的默认设置", placeholder="bing,duckduckgo")


# ---------------------------------------------------------------- 填 key 的来源
def _keyed(cls_name: str, label: str, doc: str, url: str, daily: int, extra: dict | None = None) -> type:
    """填 key 即启用的来源（D35、D39）。daily_limit 按免费额度折算（D42），0 表示不限。"""
    ns: dict = {
        "__annotations__": {"enabled": bool, "api_key": str, "daily_limit": int},
        "__ui_label__": label,
        "__doc__": doc,
        "enabled": F(True, "启用", "填了 Key 就会使用，想暂时停用就关掉"),
        "api_key": F("", "API Key", f"申请：{url}", secret=True),
        "daily_limit": F(daily, "每日上限（次）", "按免费额度折算，填 0 不限", ge=0, le=100000),
    }
    for k, (typ, fld) in (extra or {}).items():
        ns["__annotations__"][k] = typ
        ns[k] = fld
    return type(cls_name, (PluginConfigBase,), ns)


BaiduSection = _keyed("BaiduSection", "百度千帆",
                      "中文效果最好。每天约 50 次免费，按天发放，需要实名。", "https://console.bce.baidu.com/qianfan", 50)
ExaSection = _keyed("ExaSection", "Exa",
                    "英文、技术类效果最好。每月 $10 免费额度（本插件用的 Auto 模式约 1400 次），不用绑卡。",
                    "https://dashboard.exa.ai", 45)
FirecrawlSection = _keyed("FirecrawlSection", "Firecrawl",
                          "每月 1000 credits（约 500 次搜索），不用绑卡。", "https://www.firecrawl.dev", 16)
LangSearchSection = _keyed("LangSearchSection", "LangSearch",
                           "免费，每天重置。中文效果一般，作为兜底。", "https://langsearch.com", 0)
BochaSection = _keyed("BochaSection", "博查",
                      "中文。注册送 1000 次，一次性，3 个月有效。", "https://open.bochaai.com", 30)
LinkupSection = _keyed("LinkupSection", "Linkup",
                       "免费额度以官网为准（本插件作者没有申请）。", "https://app.linkup.so", 60)
SerperSection = _keyed("SerperSection", "Serper（Google 结果）",
                       "一次性 2500 次免费；转售 Google 搜索结果。", "https://serper.dev", 40)
BraveSection = _keyed("BraveSection", "Brave（需绑卡）",
                      "独立英文索引。每月 $5 免费额度，但必须绑信用卡。", "https://api-dashboard.search.brave.com", 33)
ParallelSection = _keyed("ParallelSection", "Parallel（需绑卡）",
                         "每月 $5 免费额度，但要绑卡才发放；不绑卡只会返回额度不足。", "https://platform.parallel.ai", 160)
PerplexitySection = _keyed("PerplexitySection", "Perplexity（付费）",
                           "没有免费额度，按次计费。", "https://www.perplexity.ai/settings/api", 0)
QwenSection = _keyed(
    "QwenSection", "千问联网搜索（付费）",
    "阿里云百炼，按次计费（约 ¥3.4/千次）。只在其他来源不够时补充，只用它找到的网页。",
    "https://bailian.console.aliyun.com", 30,
    extra={
        "base_url": (str, F("", "接口地址（可选）", "控制台要求填业务空间地址时才需要；留空用默认地址",
                            placeholder="https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com")),
        "model": (str, F("qwen3.7-flash", "模型", "需要支持联网搜索；默认的 qwen3.7-flash 最便宜")),
    },
)

SOURCE_SECTIONS = ("host_web", "keenable", "tavily", "searxng", "langsearch", "bocha", "baidu", "qwen", "exa", "brave",
                   "parallel", "linkup", "firecrawl", "serper", "perplexity")
KEYED_SECTIONS = ("baidu", "exa", "firecrawl", "langsearch", "bocha", "linkup", "serper", "brave", "parallel",
                  "perplexity", "qwen")


class _SourcesView:
    """兼容旧代码的 cfg.sources.xxx 访问方式。WebUI 只把顶层配置节渲染成表单，嵌套两层会变成 JSON 文本框，
    所以每个来源都放在顶层：[bocha]、[exa]……"""

    def __init__(self, cfg: "HubConfig") -> None:
        self._cfg = cfg

    def __getattr__(self, name: str):
        if name in SOURCE_SECTIONS:
            return getattr(self._cfg, name)
        raise AttributeError(name)


# ---------------------------------------------------------------- 以图搜图
class ImageSection(PluginConfigBase):
    """图片会先去掉拍摄信息等元数据、重新编码，再依次发给开启的服务，拿到确定结果就停。"""
    __ui_label__ = "以图搜图"
    enabled: bool = F(True, "启用以图搜图", "关闭后，聊天里的图片不会发给任何服务")
    allow_adult: bool = F(False, "允许成人向结果", "关闭时过滤 trace.moe 的成人作品和 SauceNAO 标为隐藏的结果；打开时保留并标注“成人向”")
    tracemoe_enabled: bool = F(True, "trace.moe：番剧截图", "哪部番、第几集、几分几秒。每天 100 次")
    tracemoe_key: str = F("", "trace.moe Key（可选）", "赞助者 Key，可以提高额度", secret=True)
    animetrace_enabled: bool = F(True, "AnimeTrace：角色识别", "动漫、游戏角色")
    animetrace_model: str = F("", "AnimeTrace 模型（可选）", "留空用服务默认，一般不用填")
    saucenao_key: str = F("", "SauceNAO Key：插画出处", "填了才启用。免费注册：https://saucenao.com/user.php", secret=True)
    web_enabled: bool = F(True, "启用 Yandex、Bing", "不想让图片上传到这两个网站就关掉")
    web_engines: str = F("yandex,bing", "使用顺序", "可选 yandex、bing，用英文逗号分隔；只留一个就只用一个")
    verify_dhash: bool = F(True, "下载候选图核对", "下载搜到的候选图，确认是不是同一张图")
    container_url: str = F("", "容器地址", placeholder="http://picimagesearch-api:8080")
    container_key: str = F("", "容器 Key", "容器的 PICSEARCH_API_KEY", secret=True)
    container_engines: str = F("google_lens,baidu,tineye", "容器引擎的使用顺序", "用英文逗号分隔")


# ---------------------------------------------------------------- 高级
class ReadSection(PluginConfigBase):
    """麦麦读取网页原文时的设置。"""
    __ui_label__ = "读取网页原文"
    enabled: bool = F(True, "允许读取网页原文")
    use_host_fetch: bool = F(True, "优先用麦麦联网服务读取", "需要麦麦已配置联网服务")
    use_fetch_url_plugin: bool = F(True, "其次用 fetch-url 插件读取", "装了 fetch-url 插件时生效")
    timeout_seconds: float = F(15.0, "单次读取超时（秒）", ge=3.0, le=60.0)
    excerpt_chars: int = F(2200, "每次最多返回（字）", ge=500, le=2800)
    max_reads_per_turn: int = F(4, "每个问题最多读取（篇）", "超过后提示麦麦先用已有资料回答", ge=1, le=20)
    page_cache_minutes: float = F(15.0, "网页缓存时间（分钟）", "缓存期内再读同一网页不重新下载", ge=0.0, le=240.0)


class ReplySection(PluginConfigBase):
    """把刚搜到的资料交给回复模型，让回答带上出处。"""
    __ui_label__ = "回复时带上出处"
    inject_evidence: bool = F(True, "回复时附上刚搜到的资料")
    evidence_window_minutes: float = F(3.0, "“刚搜到”的时间范围（分钟）", ge=0.5, le=30.0)


class StorageSection(PluginConfigBase):
    """搜索记录保存在插件数据目录的 hub.sqlite3；不保存网页全文和图片。
    0.5.2 去掉了“保存到磁盘”开关（D60）：旧配置里的 enabled 会被忽略（extra=ignore），记录始终保存。"""
    __ui_label__ = "搜索记录"
    keep_days: int = F(30, "搜索记录保留（天）", "S 编号对应的网址、标题、摘要，超过天数自动删除", ge=1, le=365)
    trace_keep_days: int = F(7, "调用轨迹保留（天）", ge=1, le=90)
    per_session_entries: int = F(200, "每个聊天最多保留（条）", ge=20, le=2000)


class HubConfig(PluginConfigBase):
    plugin: PluginSection = Field(default_factory=PluginSection)
    search: SearchSection = Field(default_factory=SearchSection)
    coexist: CoexistSection = Field(default_factory=CoexistSection)
    # ---- 来源（每个来源一个顶层配置节，WebUI 才会渲染成表单、key 才会显示为密码框） ----
    keenable: KeenableSection = Field(default_factory=KeenableSection)
    tavily: TavilySection = Field(default_factory=TavilySection)
    host_web: HostWebSection = Field(default_factory=HostWebSection)
    searxng: SearxngSection = Field(default_factory=SearxngSection)
    baidu: BaiduSection = Field(default_factory=BaiduSection)
    exa: ExaSection = Field(default_factory=ExaSection)
    firecrawl: FirecrawlSection = Field(default_factory=FirecrawlSection)
    langsearch: LangSearchSection = Field(default_factory=LangSearchSection)
    bocha: BochaSection = Field(default_factory=BochaSection)
    linkup: LinkupSection = Field(default_factory=LinkupSection)
    serper: SerperSection = Field(default_factory=SerperSection)
    brave: BraveSection = Field(default_factory=BraveSection)
    parallel: ParallelSection = Field(default_factory=ParallelSection)
    perplexity: PerplexitySection = Field(default_factory=PerplexitySection)
    qwen: QwenSection = Field(default_factory=QwenSection)
    image: ImageSection = Field(default_factory=ImageSection)
    read: ReadSection = Field(default_factory=ReadSection)
    reply: ReplySection = Field(default_factory=ReplySection)
    storage: StorageSection = Field(default_factory=StorageSection)

    @property
    def sources(self) -> _SourcesView:
        return _SourcesView(self)


# ---------------------------------------------------------------- 迁移
def migrate_config(raw: Any) -> Any:
    """0.1 → 0.3：[sources.xxx] 挪到顶层 [xxx]；旧值优先、不覆盖新位置已有的值；没改时原样返回同一个对象。

    注意（2026-10-29 实测）：config_version 升级时，Host 先用新默认值为骨架重建配置、只保留同名同位置的旧值
    （runner_main._prepare_plugin_config_for_version_update → rebuild_plugin_config_data），然后才调用插件的
    normalize_plugin_config。所以**换了配置节的字段会丢值**，迁移函数也救不回来。以后不要在配置节之间挪字段；
    要整理界面，只改标题、说明、标签页和折叠。
    """
    if not isinstance(raw, dict):
        return raw
    out = raw
    if isinstance(raw.get("sources"), dict):
        out = {k: v for k, v in raw.items() if k != "sources"}
        for name, sec in raw["sources"].items():
            if isinstance(sec, dict):
                cur = dict(out.get(name) or {})
                for k, v in sec.items():
                    cur.setdefault(k, v)
                out[name] = cur
    return out


# ---------------------------------------------------------------- WebUI 布局
CONFIG_TABS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("basic", "常用", ("search", "coexist")),
    ("free", "免费来源", ("keenable", "tavily", "host_web", "searxng")),
    ("keyed", "需要 Key 的来源", KEYED_SECTIONS),
    ("image", "以图搜图", ("image", "image_anime", "image_web", "image_container")),
    ("advanced", "高级", ("search_tuning", "read", "reply", "storage")),
)


TUNING_FIELDS = ("sync_window_seconds", "job_timeout_seconds", "stale_minutes", "per_session_running",
                 "per_session_queue", "deep_width", "deep_read_top", "subagent_sync_window_seconds",
                 "subagent_max_searches")
# 配置页上的虚拟配置节：(虚拟节 id, 实际 TOML 节, 标题, 说明, 字段)。字段按两列排，相关的两项放在同一行。
VIRTUAL_SECTIONS: tuple[tuple[str, str, str, str, tuple[str, ...]], ...] = (
    ("search_tuning", "search", "等待与并发",
     "什么时候把搜索转到后台、同时进行几个、深度搜索查多深。默认值已经实测过，一般不用改。", TUNING_FIELDS),
    ("image_anime", "image", "二次元识别", "番剧截图、角色、插画出处。trace.moe 和 AnimeTrace 免 Key；SauceNAO 填了 Key 才用。",
     ("tracemoe_enabled", "tracemoe_key", "animetrace_enabled", "animetrace_model", "saucenao_key")),
    ("image_web", "image", "相似图片（Yandex、Bing）", "照片、实物、相似图片。图片会上传到这两个网站。",
     ("web_enabled", "web_engines")),
    ("image_container", "image", "以图搜图容器（可选）",
     "自己部署了 picimagesearch-api 才需要，用于 Google Lens、百度识图、TinEye。",
     ("container_url", "container_key", "container_engines")),
)


def decorate_schema(schema: dict[str, Any], cfg: Any = None) -> dict[str, Any]:
    """在 SDK 生成的 schema 上调整配置页（只改展示，不改 TOML 结构）：
    - 标签页布局（CONFIG_TABS）；页头已有启用开关，[plugin] 节不再单独显示。
    - [search] 里的等待与并发调校项拆成一个虚拟配置节 search_tuning，放到「高级」。前端按节的 `name`
      读写数据（dashboard `SectionRenderer`：resolvedSectionName = section.name || sectionName），
      所以虚拟节的 name 仍是 "search"，保存时写回 [search]。
    - 没填 Key 的来源、没填地址的 SearXNG、「高级」里的节默认折叠；「需要 Key 的来源」标签显示已填数量。
    """
    if not isinstance(schema, dict) or not isinstance(schema.get("sections"), dict):
        return schema
    out = copy.deepcopy(schema)
    sections: dict[str, Any] = out["sections"]

    for vname, real, title, desc, fields in VIRTUAL_SECTIONS:
        src = sections.get(real)
        if not isinstance(src, dict) or not isinstance(src.get("fields"), dict):
            continue
        moved = {k: src["fields"].pop(k) for k in fields if k in src["fields"]}
        for i, f in enumerate(moved.values()):
            f["order"] = i
        sections[vname] = {"name": real, "title": title, "description": desc, "icon": None,
                           "collapsed": False, "order": 0, "fields": moved}

    def filled(name: str) -> bool:
        sec = getattr(cfg, name, None) if cfg is not None else None
        if sec is None:
            return False
        if name == "searxng":
            return bool(str(getattr(sec, "base_url", "")).strip())
        return bool(str(getattr(sec, "api_key", "")).strip())

    tabs = []
    order = 0
    for tab_order, (tab_id, title, names) in enumerate(CONFIG_TABS):
        present = [n for n in names if n in sections]
        for n in present:
            sections[n]["order"] = order
            order += 1
            sections[n]["collapsed"] = (tab_id == "advanced" or (tab_id == "keyed" and not filled(n))
                                        or (n == "searxng" and not filled(n))
                                        or (n == "image_container" and not str(getattr(getattr(cfg, "image", None),
                                                                                       "container_url", "")).strip()))
        tab: dict[str, Any] = {"id": tab_id, "title": title, "sections": present, "order": tab_order}
        if tab_id == "keyed":
            n_filled = sum(filled(n) for n in present)
            if n_filled:
                tab["badge"] = f"已填 {n_filled}"
        tabs.append(tab)
    out["layout"] = {"type": "tabs", "tabs": tabs}
    return out


def mode_value(v: str) -> str:
    """配置里的中文选项 → 内部模式名。"""
    return MODE_VALUE.get(v, v)


__all__ = ["HubConfig", "migrate_config", "decorate_schema", "mode_value", "CONFIG_TABS", "SOURCE_SECTIONS",
           "KEYED_SECTIONS", "Mapping"]
