"""合并（URL 规范化去重 + 加权 RRF）与"结果不足"判定（D24、D36、D37）。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .providers import Hit

_TRACKING = re.compile(r"^(utm_|spm$|from$|share|ref$|ref_|fbclid$|gclid$|_hsenc$|mc_)", re.I)


def normalize_url(url: str) -> str:
    p = urlsplit(url.strip())
    host = (p.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    try:
        port_n = p.port
    except ValueError:   # F05（0.5.2）：非法端口不能让整次合并抛异常
        port_n = None
    port = f":{port_n}" if port_n and port_n not in (80, 443) else ""
    path = re.sub(r"/+", "/", p.path or "/")
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    query = urlencode(sorted((k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not _TRACKING.match(k)))
    return urlunsplit(("https" if p.scheme in ("http", "https") else p.scheme, host + port, path, query, ""))


def domain_of(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


@dataclass
class Merged:
    key: str
    url: str
    title: str
    snippet: str
    published_at: str | None
    sources: list[str] = field(default_factory=list)   # 命中的来源名（保序）
    families: set[str] = field(default_factory=set)
    score: float = 0.0


def rrf_merge(batches: list[tuple[str, str, float, list[Hit]]], k: int = 60) -> list[Merged]:
    """batches: [(来源名, 索引族, 权重, hits)]。同族多次命中只计最好名次（D37：不算多源印证）。"""
    merged: dict[str, Merged] = {}
    best_rank: dict[tuple[str, str], int] = {}
    family_weight: dict[str, float] = {}
    for name, family, weight, hits in batches:
        family_weight[family] = max(family_weight.get(family, 0.0), weight)
        for rank, h in enumerate(hits, start=1):
            key = normalize_url(h.url)
            m = merged.get(key)
            if m is None:
                m = merged[key] = Merged(key, h.url, h.title, h.snippet, h.published_at)
            else:
                if len(h.snippet) > len(m.snippet):
                    m.snippet = h.snippet
                if not m.published_at and h.published_at:
                    m.published_at = h.published_at
            if name not in m.sources:
                m.sources.append(name)
            m.families.add(family)
            fk = (key, family)
            if fk not in best_rank or rank < best_rank[fk]:
                best_rank[fk] = rank
    for (key, family), rank in best_rank.items():
        merged[key].score += family_weight[family] / (k + rank)
    return sorted(merged.values(), key=lambda m: m.score, reverse=True)


# ---------------- 结果不足判定 ----------------

_STOP = set("的 了 是 在 和 与 及 吗 呢 啊 吧 什么 怎么 如何 哪个 哪些 有 没有 一下 现在 最新 一个 这个 那个 the a an of to in on for and or is are what how".split())


def _terms(query: str) -> list[str]:
    try:
        import jieba  # Host 自带
        words = [w.strip().lower() for w in jieba.lcut(query)]
    except Exception:  # noqa: BLE001
        words = [w.lower() for w in re.findall(r"[A-Za-z0-9.+#]+|[\u4e00-\u9fff]", query)]
    return [w for w in words if w and w not in _STOP and not re.fullmatch(r"\W+", w)]


def coverage(query: str, text: str) -> float:
    terms = _terms(query)
    if not terms:
        return 1.0
    low = text.lower()
    return sum(1 for t in terms if t in low) / len(terms)


THRESHOLDS = {
    "quick": {"n": 2, "domains": 1, "top": 5},
    "normal": {"n": 3, "domains": 2, "top": 5},
    "deep": {"n": 5, "domains": 3, "top": 10},
}


def _parse_date(s: str | None) -> datetime | None:
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d",
                "%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S %z", "%b %d, %Y"):
        try:
            d = datetime.strptime(s.replace("Z", "+00:00") if "%z" in fmt else s, fmt)
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def judge(query: str, results: list[Merged], *, depth: str, freshness: str, intent: str) -> list[str]:
    """返回缺口列表；空列表表示"足够"。"""
    th = THRESHOLDS.get(depth, THRESHOLDS["normal"])
    top = [m for m in results[: th["top"]] if m.title and m.url]
    missing: list[str] = []
    if len(top) < th["n"]:
        missing.append("few")
    if len({domain_of(m.url) for m in top}) < th["domains"]:
        missing.append("diversity")
    if top and max((coverage(query, m.title + " " + m.snippet) for m in top[:3]), default=0) < 0.5:
        missing.append("relevance")
    if freshness in ("recent", "today") or intent == "news":
        window = timedelta(hours=48) if freshness == "today" else timedelta(days=30)
        now = datetime.now(timezone.utc)
        skew = timedelta(hours=36)   # F14（0.5.2）：时区与发布时间误差的容忍度；超过的未来日期不算“新内容”
        if not any((d := _parse_date(m.published_at)) and -skew <= now - d <= window for m in top):
            missing.append("fresh")
    return missing


MISSING_TEXT = {
    "image_weak": "以图搜没有达到“命中”的结果，下面只是相似线索",
    "few": "结果偏少",
    "diversity": "来源网站单一",
    "relevance": "前几条与问题的关键词对不上",
    "fresh": "没有找到时间范围内的新内容",
}
