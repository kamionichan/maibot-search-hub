"""后台任务管理（D11、D16、D17、D18、D45）。

核心：所有搜索都作为 Job 启动；工具最多同步等待 W 秒。
- W 内完成 → 同步返回（owner=sync）。
- 超过 W → 立即返回"已转后台"（owner=background），Job 继续运行，不取消、不重发；完成后经 proactive 送回。
归属判定在 asyncio 单线程中"检查并设置"，中间没有 await，因此是原子的：一个结果只会以一种方式交付。

0.5.2（INDEPENDENT_REVIEW F05/F13/F10）：
- 超时或被取消时，交付/保留已经拿到的部分结果（Executor.partial），不再整个丢掉。
- 任何未预期的异常都会让任务进入终态（failed）、释放会话名额，不会一直显示“进行中”。
- 搜索结束后仍在跑的请求（D36 不取消已发出的请求）由 JobManager 托管，卸载时统一收尾。
- 插件卸载打断的任务记为 interrupted（下次加载时告知，D18），用户取消的记为 cancelled。
- 已结束的任务对象最多保留 MAX_FINISHED 个。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .executor import Executor, Plan, RunResult, attempts_summary
from .ledger import Entry, Ledger


@dataclass
class Job:
    job_id: str
    session_id: str
    query: str
    depth: str
    intent: str
    freshness: str
    caller: str
    created: float = field(default_factory=time.time)
    status: str = "queued"           # queued/running/finished/failed/cancelled/timed_out/interrupted
    owner: str | None = None          # None/sync/background
    result: RunResult | None = None
    entries: list[Entry] = field(default_factory=list)
    task: asyncio.Task | None = None
    norm_query: str = ""
    cached_at: float | None = None    # 命中结果缓存时为原检索时间
    cache_key: str = ""
    image_hash: str = ""              # 以图搜：清洗后图片的 sha1（不保存图片本身）

    def row(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id, "session_id": self.session_id, "query": self.query, "depth": self.depth,
            "intent": self.intent, "caller": self.caller, "status": self.status, "created": self.created,
            "finished": time.time() if self.status not in ("queued", "running") else None,
            "delivered": 0, "sns": [e.sn for e in self.entries],
            "missing": self.result.missing if self.result else [], "note": (self.result.error if self.result else ""),
        }


Deliver = Callable[[str, list[Job]], Awaitable[bool]]
MAX_FINISHED = 200


def _partial(executor: Any) -> RunResult | None:
    fn = getattr(executor, "partial", None)
    try:
        return fn() if fn is not None else None
    except Exception:  # noqa: BLE001
        return None


class JobManager:
    def __init__(self, ledger: Ledger, *, per_session_running: int = 2, per_session_queue: int = 2,
                 job_timeout: float = 60.0, stale_seconds: float = 900.0, merge_window: float = 2.0) -> None:
        self.ledger = ledger
        self.per_running = per_session_running
        self.per_queue = per_session_queue
        self.job_timeout = job_timeout
        self.stale_seconds = stale_seconds
        self.merge_window = merge_window
        self._jobs: dict[str, Job] = {}
        self._sem: dict[str, asyncio.Semaphore] = {}
        self._pending_delivery: dict[str, list[Job]] = {}
        self._delivery_tasks: dict[str, asyncio.Task] = {}
        self.deliver: Deliver | None = None
        # 第 3 阶段：deep 档完成后读取前几条正文（enrich）；每个任务结束后的回调（持久化健康统计等）
        self.enrich: Callable[[Job], Awaitable[None]] | None = None
        self.enrich_timeout = 20.0
        self.on_finished: Callable[[Job], None] | None = None
        self._bg: set[asyncio.Task] = set()
        self._tails: set[asyncio.Task] = set()
        self._closing = False

    # ---- 查询 ----
    def active(self, session_id: str) -> list[Job]:
        return [j for j in self._jobs.values() if j.session_id == session_id and j.status in ("queued", "running")]

    def all_active(self) -> list[Job]:
        return [j for j in self._jobs.values() if j.status in ("queued", "running")]

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    # ---- 启动 ----
    def admit(self, session_id: str, norm_query: str, depth: str) -> tuple[Job | None, str]:
        """返回 (可合并的已有任务, 拒绝原因)。"""
        act = self.active(session_id)
        for j in act:
            if j.norm_query == norm_query and j.depth == depth:
                return j, ""
        if len(act) >= self.per_running + self.per_queue:
            return None, f"本会话已有 {len(act)} 个搜索在进行（{'、'.join(j.job_id for j in act)}），请等它们完成或用 /search cancel 取消"
        return None, ""

    async def start(self, job: Job, plan: Plan, executor: Executor, window: float) -> Job:
        self._evict_finished()
        self._jobs[job.job_id] = job
        self.ledger.save_job(job.row())
        job.task = asyncio.create_task(self._run(job, plan, executor))
        await asyncio.wait({job.task}, timeout=window)
        # ---- 原子归属：以下到 return 之间没有 await ----
        if job.task.done():
            job.owner = "sync"
            self.ledger.mark_delivered(job.job_id, 1)  # 同步返回即已交付，不算 parked
        else:
            job.owner = "background"
        return job

    async def wait_existing(self, job: Job, window: float) -> bool:
        """合并到已有任务：等待最多 window 秒，返回是否已完成。"""
        if job.task is None:
            return False
        await asyncio.wait({job.task}, timeout=window)
        return job.task.done()

    def _evict_finished(self) -> None:
        done = [k for k, j in self._jobs.items() if j.status not in ("queued", "running")]
        for k in done[: max(0, len(done) - MAX_FINISHED)]:
            self._jobs.pop(k, None)

    def _adopt_tails(self, executor: Any) -> None:
        for t in list(getattr(executor, "tails", ())):
            self._tails.add(t)
            t.add_done_callback(self._tails.discard)

    def _keep_partial(self, job: Job, executor: Any, note: str) -> None:
        p = _partial(executor)
        if p is not None and p.used_sources:
            p.error = note
            job.result = p
        elif job.result is None:
            job.result = RunResult([], ["few"], p.attempts if p else [], [], complete=False, error=note,
                                   error_kind="unknown")

    async def _run(self, job: Job, plan: Plan, executor: Executor) -> None:
        sem = self._sem.setdefault(job.session_id, asyncio.Semaphore(self.per_running))
        t0 = time.monotonic()
        try:
            async with sem:
                job.status = "running"
                self.ledger.save_job(job.row())
                try:
                    # 以图搜引擎串行且慢（容器单个最长 90 秒）：时限放宽到 2 倍（D17 的 deep 档 300 秒为上限）
                    limit = self.job_timeout * (2 if job.image_hash else 1)
                    job.result = await asyncio.wait_for(
                        executor.run(plan, job.query, freshness=job.freshness, intent=job.intent),
                        timeout=min(limit, 300.0))
                    job.status = "finished" if job.result.used_sources else "failed"
                except asyncio.TimeoutError:
                    job.status = "timed_out"
                    self._keep_partial(job, executor, "搜索超过时限（以下是已拿到的部分结果）")
                finally:
                    self._adopt_tails(executor)
            if job.result and job.result.results:
                job.entries = self.ledger.record(job.session_id, job.result.results)
                if self.enrich is not None and job.depth == "deep" and job.status == "finished":
                    try:
                        await asyncio.wait_for(self.enrich(job), timeout=self.enrich_timeout)
                    except (asyncio.TimeoutError, Exception):  # noqa: BLE001 - 读正文失败不影响搜索结果
                        pass
        except asyncio.CancelledError:
            # 卸载打断 → interrupted（下次加载时告知）；用户 /search cancel → cancelled
            job.status = "interrupted" if self._closing else "cancelled"
            if not job.entries:
                # 取消命令承诺“已拿到的结果仍保留在记录里”（F05）
                self._keep_partial(job, executor, "搜索被取消（以下是已拿到的部分结果）")
                if job.result and job.result.results:
                    try:
                        job.entries = self.ledger.record(job.session_id, job.result.results)
                    except Exception:  # noqa: BLE001
                        pass
            self.ledger.save_job(job.row())
            raise
        except Exception as exc:  # noqa: BLE001
            # F05：未预期的异常也要进入终态、释放名额；不重试（可能已计费）
            job.status = "failed"
            self._keep_partial(job, executor, f"内部错误（{type(exc).__name__}），未自动重试")
            if job.result and job.result.used_sources:
                job.result.used_sources = []      # 结果不可信：按失败交付
                job.result.results = []
        finally:
            if job.status != "cancelled":
                self.ledger.save_job(job.row())
                self.ledger.trace({
                    "job_id": job.job_id, "t": time.time(), "session_id": job.session_id, "query": job.query,
                    "depth": job.depth, "intent": job.intent, "caller": job.caller, "outcome": job.status,
                    "sync": None, "seconds": round(time.monotonic() - t0, 2),
                    "attempts": attempts_summary(job.result.attempts) if job.result else [],
                    "missing": job.result.missing if job.result else [], "used_sns": [],
                })
        if self.on_finished is not None:
            try:
                self.on_finished(job)
            except Exception:  # noqa: BLE001
                pass
        # 归属检查：后台任务完成后才排队送回；同步路径由工具自己返回
        if job.owner == "background":
            self._queue_delivery(job)

    # ---- 送回（D17：2 秒合并窗口、过时改为 parked） ----
    def _queue_delivery(self, job: Job) -> None:
        if time.time() - job.created > self.stale_seconds:
            self.ledger.mark_delivered(job.job_id, 0)  # parked：留在账本，等话题再出现时带出
            job.status = job.status  # 不变
            return
        self._pending_delivery.setdefault(job.session_id, []).append(job)
        if job.session_id not in self._delivery_tasks:
            t = asyncio.create_task(self._flush_after(job.session_id))
            self._delivery_tasks[job.session_id] = t
            self._bg.add(t)
            t.add_done_callback(self._bg.discard)

    async def _flush_after(self, session_id: str) -> None:
        try:
            await asyncio.sleep(self.merge_window)
        finally:
            self._delivery_tasks.pop(session_id, None)
        jobs = self._pending_delivery.pop(session_id, [])
        if not jobs or self.deliver is None:
            return
        ok = False
        try:
            ok = await self.deliver(session_id, jobs)
        except Exception:  # noqa: BLE001
            ok = False
        for j in jobs:
            self.ledger.mark_delivered(j.job_id, 1 if ok else 0)

    # ---- 取消 / 卸载 ----
    def cancel(self, session_id: str, job_id: str) -> str:
        job = self._jobs.get(job_id)
        if job is None or job.session_id != session_id:
            return f"本会话没有任务 {job_id}"
        if job.status not in ("queued", "running") or job.task is None:
            return f"{job_id} 已经结束（{job.status}）"
        job.task.cancel()
        return f"已取消 {job_id}。已发出的请求不会追回，已拿到的结果仍保留在记录里。"

    async def shutdown(self) -> None:
        """0.5.6（S1）：先取消并等任务结束（Executor 的 finally 会在这时把未完成请求交给 _tails），
        再取消并等待全部尾部请求；最后确认没有遗留。之前是先拍 _tails 快照，漏掉了取消过程中新产生的。"""
        self._closing = True
        tasks = [j.task for j in self._jobs.values() if j.task and not j.task.done()]
        for t in tasks:
            t.cancel()
        for t in list(self._bg):
            t.cancel()
        if tasks or self._bg:
            await asyncio.gather(*tasks, *list(self._bg), return_exceptions=True)
        # 搜索结束后仍在跑的请求：插件要卸载了，一并取消（结果已无处可去；服务端是否已计费无法撤回）
        for _ in range(3):   # 取消尾部请求时不会再产生新的尾部；循环只是保险
            tails = [t for t in self._tails if not t.done()]
            if not tails:
                break
            for t in tails:
                t.cancel()
            await asyncio.gather(*tails, return_exceptions=True)
        self.ledger.mark_interrupted()
