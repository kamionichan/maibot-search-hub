"""以图搜编排（第 4 阶段；IMAGE_SEARCH_SURVEY 4.2、IMPLEMENTATION_PLAN 第 7 条）。

路线（按 intent；auto 时走"出处链"）：
- identify_scene      trace.moe → SauceNAO → 容器
- identify_character  AnimeTrace → SauceNAO → 容器
- image_source / auto SauceNAO → trace.moe → Yandex → AnimeTrace → Bing → 容器
- identify_person / similar_image  Yandex → Bing → 容器（D54：Yandex、Bing 已内置）
- 每一步拿到 level=hit 就停（quick 拿到 likely 也停）；deep 档继续补一个不同性质的引擎做印证。
- 引擎串行：trace.moe 并发 1、SauceNAO 30 秒 4 次、容器同时只跑一个（HOST_FACTS），不能并发打过去。
- 容器候选不带相似度：下载候选图（SSRF 防护，最多 5 张、每张 ≤5MB）算 dHash，
  距离 ≤8 升为 hit（同图）、≤14 升为 likely，其余保持 weak（ARCHITECTURE「以图搜用引擎自带的相似度」）。

结果仍输出成 RunResult/Merged，复用文字搜索的任务、送回、账本与结果卡（ImageHit 的判定写进 snippet 开头）。
"""

from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from .executor import Attempt, RunResult
from .health import Health
from .image import dhash_bytes, hamming
from .image_sources import ImageHit
from .merge import Merged, normalize_url
from .net import Outcome, SourceError

ROUTES = {
    # "web" = 其余内置抓网页引擎（按配置顺序），"container" = 容器里的引擎；写了具体名字的排在指定位置
    "identify_scene": ("tracemoe", "saucenao", "img_yandex"),
    "identify_character": ("animetrace", "saucenao", "img_yandex", "img_bing"),
    "image_source": ("saucenao", "tracemoe", "img_yandex", "animetrace", "web", "container"),
    "auto": ("saucenao", "tracemoe", "img_yandex", "animetrace", "web", "container"),
    "identify_person": ("web", "container"),
    "similar_image": ("web", "container"),
}
STEPS = {"quick": 1, "normal": 4, "deep": 6}
LEVEL_TEXT = {"hit": "命中", "likely": "疑似", "weak": "弱线索"}
KIND_TEXT = {"scene": "番剧截图", "character": "角色", "source": "出处", "visual": "相似图片", "guess": "识别关键词"}
DHASH_HIT, DHASH_LIKELY = 8, 14


@dataclass
class ImagePlan:
    depth: str
    intent: str
    steps: list[Any]                      # 按顺序尝试的引擎
    image: Any                            # PreparedImage
    skipped: list[tuple[str, str]] = field(default_factory=list)
    primary: list[Any] = field(default_factory=list)   # 兼容 JobManager 的 eta 估算


def make_image_plan(providers: list[Any], health: Health, *, depth: str, intent: str, image: Any,
                    provider: str = "", container_order: list[str] | None = None) -> ImagePlan:
    route = ROUTES.get(intent, ROUTES["auto"])
    by_name = {p.info.name: p for p in providers}
    containers = [p for p in providers if p.info.extra.get("container")]
    webs = [p for p in providers if p.info.extra.get("web")]
    if container_order:
        rank = {f"imgc_{n}": i for i, n in enumerate(container_order)}
        containers.sort(key=lambda p: rank.get(p.info.name, 99))
    steps: list[Any] = []
    skipped: list[tuple[str, str]] = []
    if provider:
        p = by_name.get(provider) or by_name.get(f"img_{provider}")
        if p is None:
            raise SourceError(Outcome.NOT_SENT, f"以图搜来源 {provider} 未启用")
        if not health.available(p.info.name):
            raise SourceError(Outcome.NOT_SENT, f"以图搜来源 {provider} 暂不可用：{health.cooldown_reason(p.info.name)}")
        steps.append(p)
    for step in route:
        cands = (containers if step == "container" else webs if step == "web"
                 else [by_name[step]] if step in by_name else [])
        for p in cands:
            if p in steps:
                continue
            if not health.available(p.info.name):
                skipped.append((p.info.name, health.cooldown_reason(p.info.name) or "冷却中"))
                continue
            steps.append(p)
    if not steps:
        why = "；".join(f"{n}：{r}" for n, r in skipped)
        need = ("Yandex/Bing 未开启且没有部署以图搜容器" if intent in ("identify_person", "similar_image")
                else "没有可用的以图搜来源")
        raise SourceError(Outcome.NOT_SENT, need + (f"（{why}）" if why else ""))
    steps = steps[: STEPS.get(depth, 3) + (1 if provider else 0)]
    return ImagePlan(depth, intent, steps, image, skipped, primary=steps[:1])


# 0.5.6（S4）：解码并发上限由线程池本身保证（2 个工作线程）。之前用 asyncio 信号量包住 run_in_executor，
# 协程被取消时槽位提前释放，后台线程却还在跑，并发会超过 2。专用线程池不受取消影响：线程跑完才空出来。
_DECODE_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="hub-image")


async def run_image_cpu(fn: Any, *args: Any) -> Any:
    """图片相关的 CPU 活（输入清洗、候选 dHash）都走这个专用线程池，最多同时 2 个，不占 MaiBot 的默认线程池。"""
    return await asyncio.get_running_loop().run_in_executor(_DECODE_POOL, fn, *args)


class ImageExecutor:
    def __init__(self, health: Health, reader: Any = None, *, verify_limit: int = 5) -> None:
        self.health = health
        self.reader = reader
        self.verify_limit = verify_limit
        self._state: tuple[list[ImageHit], list[Attempt], list[str], list[SourceError]] | None = None

    def partial(self) -> RunResult | None:
        """F05（0.5.2）：任务超时或被取消时，交付已经跑完的引擎的结果。"""
        if self._state is None:
            return None
        hits, attempts, used, errors = self._state
        if not used and not errors:
            return None
        r = _to_result(list(hits), list(attempts), list(used), list(errors))
        r.complete = False
        return r

    async def run(self, plan: ImagePlan, query: str, *, freshness: str = "any", intent: str = "auto") -> RunResult:
        attempts = [Attempt(n, time.monotonic(), outcome="skipped", note=why) for n, why in plan.skipped]
        hits: list[ImageHit] = []
        used: list[str] = []
        errors: list[SourceError] = []
        self._state = (hits, attempts, used, errors)
        kinds_done: set[str] = set()
        for p in plan.steps:
            a = Attempt(p.info.name, time.monotonic())
            attempts.append(a)
            try:
                many = p.info.extra.get("container") or p.info.extra.get("web")
                got = await p.search_image(plan.image.jpeg, max_results=5 if many else 3)
            except SourceError as err:
                a.outcome, a.note, a.seconds = err.outcome.value, err.message, time.monotonic() - a.started
                self.health.failure(p.info.name, err, p.info.extra.get("key_gen"))
                errors.append(err)
                continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                err = SourceError(Outcome.UNKNOWN, type(exc).__name__)
                a.outcome, a.note, a.seconds = "unknown", err.message, time.monotonic() - a.started
                self.health.failure(p.info.name, err, p.info.extra.get("key_gen"))
                errors.append(err)
                continue
            a.seconds = time.monotonic() - a.started
            a.outcome, a.n = "ok", len(got)
            self.health.success(p.info.name, a.seconds)
            used.append(p.info.name)
            # 0.5.6（S4）：先保存候选，再做验证。验证期间被取消或超时，已经拿到的候选仍在部分结果里
            #（验证字段原地更新，没验证完的就是“未验证”）。
            hits.extend(got)
            if got and self.reader is not None and (many or p.info.extra.get("verify_thumbs")):
                await self._verify(plan.image.dhash, got, only_thumbs=not many)
            best = _best_level(got)
            kinds_done.update(h.kind for h in got)
            if best == "hit" and plan.depth != "deep":
                break
            if best == "likely" and plan.depth == "quick":
                break
            if plan.depth == "deep" and best == "hit" and len(kinds_done) >= 2:
                break
        return _to_result(hits, attempts, used, errors)

    async def _verify(self, target: int, hits: list[ImageHit], *, only_thumbs: bool = False) -> None:
        """候选图下载后算 dHash。下载失败只是"未验证"，不能判成"不符"（IMAGE_SEARCH_SURVEY 4.4）。
        Yandex/容器：所有带图的候选；SauceNAO：只有 80–90% 的疑似项（only_thumbs）。dHash 只升级、不降级。"""
        cands = [h for h in hits if h.extra.get("thumbnail") or (not only_thumbs and _looks_like_image(h.url))]
        cands = [h for h in cands if h.kind in ("visual", "source", "scene", "character")][: self.verify_limit]

        async def one(h: ImageHit) -> None:
            url = h.extra.get("thumbnail") or h.url
            try:
                data = await asyncio.wait_for(self.reader.fetch_bytes(url, max_bytes=5 * 1024 * 1024, accept=("image/",)),
                                              timeout=10)
            except Exception:  # noqa: BLE001
                h.extra["verify"] = "未验证"
                return
            # F12：解码放到专用线程池（最多 2 个线程），不占事件循环
            dh = await run_image_cpu(dhash_bytes, data)
            if dh is None:
                h.extra["verify"] = "未验证"
                return
            d = hamming(target, dh)
            h.extra["dhash_distance"] = d
            if d <= DHASH_HIT:
                h.level, h.extra["verify"] = "hit", "同图"
            elif d <= DHASH_LIKELY:
                if h.level == "weak":
                    h.level = "likely"
                h.extra["verify"] = "疑似"
            else:
                h.extra["verify"] = "缩略图不符"

        await asyncio.gather(*(one(h) for h in cands))


def _looks_like_image(url: str) -> bool:
    return url.lower().split("?")[0].endswith((".jpg", ".jpeg", ".png", ".webp", ".gif"))


def _best_level(hits: list[ImageHit]) -> str:
    order = {"hit": 0, "likely": 1, "weak": 2}
    return min((h.level for h in hits), key=lambda x: order[x], default="none")


def _to_result(hits: list[ImageHit], attempts: list[Attempt], used: list[str], errors: list[SourceError]) -> RunResult:
    order = {"hit": 0, "likely": 1, "weak": 2}
    hits.sort(key=lambda h: (order[h.level], -(h.score or 0) if h.score is not None and h.score <= 1 else -(h.score or 0) / 100))
    merged: dict[str, Merged] = {}
    for i, h in enumerate(hits):
        key = normalize_url(h.url)
        tag = f"[{LEVEL_TEXT[h.level]}·{KIND_TEXT.get(h.kind, h.kind)}" + (
            f"·{h.extra['verify']}" if h.extra.get("verify") else "") + ("·成人向" if h.nsfw else "") + "] "
        m = merged.get(key)
        if m is None:
            merged[key] = Merged(key, h.url, h.title, tag + h.snippet, h.published_at, [h.source], {h.source},
                                 score=1.0 / (i + 1))
        else:
            if h.source not in m.sources:
                m.sources.append(h.source)
                m.families.add(h.source)
    results = list(merged.values())
    best = _best_level(hits)
    missing = [] if best == "hit" else (["few"] if not hits else ["image_weak"])
    if not used:
        kinds = {e.outcome for e in errors}
        kind = "unknown" if Outcome.UNKNOWN in kinds else "rejected" if Outcome.REJECTED in kinds else "not_sent"
        msg = "；".join(dict.fromkeys(e.message for e in errors)) or "没有可用的以图搜来源"
        return RunResult([], ["few"], attempts, used, complete=False, error=msg, error_kind=kind)
    return RunResult(results, missing, attempts, used, complete=not errors)
