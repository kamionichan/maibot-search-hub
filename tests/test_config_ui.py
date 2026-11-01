"""配置页：中文标题、标签页覆盖全部配置节、迁移不丢 key、0.5.0 旧配置照常加载。"""

import re

import plugin as P
from hub.config import CONFIG_TABS, HubConfig, decorate_schema, migrate_config


def schema(cfg=None):
    p = P.SearchHubPlugin()
    if cfg is not None:
        p._plugin_config_instance = cfg  # type: ignore[attr-defined]
    return p.get_webui_config_schema(plugin_id="kamionichan.search_hub", plugin_name="x", plugin_version="0.5.1")


def test_every_section_in_a_tab_and_every_field_has_chinese_label():
    s = schema()
    assert s["layout"]["type"] == "tabs"
    in_tabs = [n for t in s["layout"]["tabs"] for n in t["sections"]]
    # 标签页布局下，未列入标签页的配置节不会显示；只有 [plugin]（页头已有启用开关）故意不列
    assert sorted(in_tabs + ["plugin"]) == sorted(s["sections"])
    assert len(in_tabs) == len(set(in_tabs))
    tuning = s["sections"]["search_tuning"]
    assert tuning["name"] == "search" and "sync_window_seconds" in tuning["fields"]
    assert "sync_window_seconds" not in s["sections"]["search"]["fields"]
    assert set(tuning["fields"]) | set(s["sections"]["search"]["fields"]) == set(HubConfig().search.model_dump())
    img = [n for n, sec in s["sections"].items() if sec.get("name", n) == "image"]
    assert sorted(img) == ["image", "image_anime", "image_container", "image_web"]
    got = set().union(*(s["sections"][n]["fields"] for n in img))
    assert got == set(HubConfig().image.model_dump()), "以图搜字段拆分后不能丢"
    for n, sec in s["sections"].items():
        assert sec["fields"], f"{n} 是空的配置节"
    for name, sec in s["sections"].items():
        assert sec["title"] != name, name
        assert sec["description"], f"{name} 缺少说明"
        assert "bot" not in sec["description"].lower().replace("maibot", ""), name
        for fn, f in sec["fields"].items():
            assert f["label"] != fn and not re.fullmatch(r"[a-z0-9_]+", f["label"]), (name, fn)   # 不再显示英文字段名
    assert s["sections"]["plugin"]["fields"]["config_version"]["hidden"] is True
    assert s["sections"]["tavily"]["fields"]["api_key"]["ui_type"] == "password"
    assert s["sections"]["coexist"]["fields"]["host_tools_mode"]["choices"] == ["隐藏", "观察", "不处理"]
    assert "runtime_widgets" not in s     # 未运行（没有 on_load）的实例不声明运行状态面板


def test_runtime_widgets_only_when_running():
    """刚装好未重启/已停用时 Host 用临时实例出配置页；此时声明面板会让前端每 2 秒在加载中和
    “插件未运行”报错之间切换，整页跳动（用户反馈，1.3.18 实测复现）。"""
    from hub.ledger import Ledger
    p = P.SearchHubPlugin()
    kw = dict(plugin_id="kamionichan.search_hub", plugin_name="x", plugin_version="0.5.1")
    assert "runtime_widgets" not in p.get_webui_config_schema(**kw)
    p.ledger = Ledger(None, per_session=10)
    ids = [w["id"] for w in p.get_webui_config_schema(**kw)["runtime_widgets"]]
    assert ids == ["hub_status", "clear_all"]


def test_unfilled_keyed_sources_collapsed_and_badge():
    s = schema()
    assert s["sections"]["exa"]["collapsed"] is True and s["sections"]["search"]["collapsed"] is False
    assert s["sections"]["storage"]["collapsed"] is True
    keyed = next(t for t in s["layout"]["tabs"] if t["id"] == "keyed")
    assert "badge" not in keyed
    cfg = HubConfig()
    cfg.exa.api_key = "x" * 20
    cfg.baidu.api_key = "y" * 20
    s2 = schema(cfg)
    assert s2["sections"]["exa"]["collapsed"] is False and s2["sections"]["brave"]["collapsed"] is True
    assert s2["sections"]["image_container"]["collapsed"] is True
    assert next(t for t in s2["layout"]["tabs"] if t["id"] == "keyed")["badge"] == "已填 2"
    orders = [s2["sections"][n]["order"] for t in CONFIG_TABS for n in t[2] if n in s2["sections"]]
    assert orders == sorted(orders)


def test_050_config_layout_unchanged_and_values_survive_host_rebuild():
    """Host 升级 config_version 时按新默认值重建、只保留同名同位置的旧值。模拟这一步，确认 0.5.0 的值一个不丢。"""
    import sys
    from pathlib import Path
    sys.path.insert(0, "/tmp/mb")
    old = {"plugin": {"config_version": "0.3.0", "enabled": True},
           "search": {"allow_text_outbound": True, "sync_window_seconds": 6.0, "deep_width": 4, "proxy": "http://p:1"},
           "coexist": {"host_tools_mode": "off", "other_tools_mode": "hide"},
           "exa": {"enabled": True, "api_key": "EXA-OLD-KEY-123456"},
           "image": {"saucenao_key": "SN-OLD"}}
    p = P.SearchHubPlugin()
    default = p.get_default_config()

    def rebuild(target, source):          # 与 Host runner_main._overlay_plugin_config_fields 相同的规则
        for k, v in source.items():
            if k not in target or k == "config_version":
                continue
            if isinstance(target[k], dict) and isinstance(v, dict):
                rebuild(target[k], v)
            elif type(target[k]) is type(v) or (isinstance(target[k], float) and isinstance(v, int)):
                target[k] = v
    import copy
    rebuilt = copy.deepcopy(default)
    rebuild(rebuilt, old)
    cfg, _ = p.normalize_plugin_config(rebuilt)
    assert cfg["search"]["sync_window_seconds"] == 6.0 and cfg["search"]["deep_width"] == 4
    assert cfg["search"]["proxy"] == "http://p:1"
    assert cfg["exa"]["api_key"] == "EXA-OLD-KEY-123456" and cfg["image"]["saucenao_key"] == "SN-OLD"
    hc = HubConfig.model_validate(cfg)
    assert hc.coexist.host_tools_mode == "不处理" and hc.coexist.other_tools_mode == "隐藏"
    # 0.5.0 的每个配置节、每个字段在新版里都还在同一个位置
    v050 = {"plugin", "search", "host_web", "keenable", "tavily", "searxng", "langsearch", "bocha", "baidu", "qwen",
            "exa", "brave", "parallel", "linkup", "firecrawl", "serper", "perplexity", "image", "coexist", "read",
            "reply", "storage"}
    assert v050 <= set(default)


def test_migrate_old_sources_still_works():
    old = {"plugin": {"config_version": "0.1.0"}, "sources": {"tavily": {"api_key": "tvly-OLD"}}}
    m = migrate_config(old)
    assert m["tavily"]["api_key"] == "tvly-OLD" and "sources" not in m
    plain = {"plugin": {"config_version": "0.5.1"}, "search": {"proxy": ""}}
    assert migrate_config(plain) is plain


def test_decorate_schema_tolerates_empty():
    assert decorate_schema({}, None) == {}
