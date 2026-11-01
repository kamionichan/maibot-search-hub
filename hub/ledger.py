"""证据账本与任务记录（D7、D12、D17、D18）。

SQLite 放在 ctx.paths.data_dir。同一会话里同一规范化 URL 只占一个 S 编号。
所有写操作在单线程 asyncio 中同步执行（sqlite3 很快，数据量小），避免并发写冲突。
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .merge import Merged
from .net import redact

SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
  session_id TEXT NOT NULL, sn INTEGER NOT NULL, url_key TEXT NOT NULL,
  url TEXT NOT NULL, title TEXT, snippet TEXT, published_at TEXT, sources TEXT,
  first_seen REAL, last_seen REAL, hits INTEGER DEFAULT 1, used INTEGER DEFAULT 0, reads INTEGER DEFAULT 0,
  PRIMARY KEY (session_id, sn)
);
CREATE UNIQUE INDEX IF NOT EXISTS entries_url ON entries(session_id, url_key);
CREATE TABLE IF NOT EXISTS jobs (
  job_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, query TEXT, depth TEXT, intent TEXT,
  caller TEXT, status TEXT, created REAL, finished REAL, delivered INTEGER DEFAULT 0,
  sns TEXT, missing TEXT, note TEXT
);
CREATE TABLE IF NOT EXISTS trace (
  job_id TEXT, t REAL, session_id TEXT, query TEXT, depth TEXT, intent TEXT, caller TEXT,
  outcome TEXT, sync INTEGER, seconds REAL, attempts TEXT, missing TEXT, used_sns TEXT
);
CREATE TABLE IF NOT EXISTS counters (name TEXT PRIMARY KEY, value INTEGER);
CREATE TABLE IF NOT EXISTS kv (name TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS cache (
  key TEXT PRIMARY KEY, t REAL NOT NULL, payload TEXT NOT NULL
);
"""


@dataclass
class Entry:
    sn: int
    url: str
    title: str
    snippet: str
    published_at: str | None
    sources: list[str]
    new: bool
    read: bool = False      # deep 档已读过正文（snippet 换成了正文摘录）


class Ledger:
    def __init__(self, path: Path | None, per_session: int = 200) -> None:
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path) if path else ":memory:", isolation_level=None)
        self._db.executescript(SCHEMA)
        cols = {r[1] for r in self._db.execute("PRAGMA table_info(entries)")}
        if "reads" not in cols:   # 0.5.6：旧库补列
            self._db.execute("ALTER TABLE entries ADD COLUMN reads INTEGER DEFAULT 0")
        self.per_session = per_session
        self._seed_counters()

    def _seed_counters(self) -> None:
        """0.5.6（S3）：0.5.1 及更早的数据库没有 S 编号计数器。打开时、在任何清理或清空之前，
        为每个已有会话按现存最大编号补上计数器，之后删掉旧记录也不会让编号从 S1 重来。
        已经在旧版本里被删掉的编号无从得知，这一点是迁移的限制。"""
        self._db.execute("BEGIN")
        try:
            self._db.execute(
                "INSERT INTO counters (name, value) SELECT 'sn:' || session_id, MAX(sn) FROM entries"
                " GROUP BY session_id ON CONFLICT(name) DO UPDATE SET value = MAX(value, excluded.value)")
            self._db.execute("COMMIT")
        except Exception:
            self._db.execute("ROLLBACK")
            raise

    def close(self) -> None:
        self._db.close()

    # ---- 账本 ----
    def record(self, session_id: str, results: list[Merged]) -> list[Entry]:
        now = time.time()
        out: list[Entry] = []
        cur = self._db.cursor()
        cur.execute("BEGIN")
        try:
            for m in results:
                # 0.5.6（B2）：所有结果都经过这里才进入账本、卡片和回复参考资料，标题和摘要在此脱敏。
                # 网址原样保存（读取原文要用）；展示时由 cards 去掉凭据参数。
                title, snippet = redact(m.title), redact(m.snippet)
                row = cur.execute("SELECT sn, hits FROM entries WHERE session_id=? AND url_key=?",
                                  (session_id, m.key)).fetchone()
                if row:
                    cur.execute("UPDATE entries SET last_seen=?, hits=hits+1, title=COALESCE(NULLIF(?, ''), title),"
                                " snippet=CASE WHEN length(?)>length(snippet) THEN ? ELSE snippet END,"
                                " published_at=COALESCE(published_at, ?) WHERE session_id=? AND sn=?",
                                (now, title, snippet, snippet, m.published_at, session_id, row[0]))
                    out.append(Entry(row[0], m.url, title, snippet, m.published_at, m.sources, False))
                    continue
                # F10：S 编号用每会话独立的递增计数器（counters 表 sn:<会话>），条目被清理/淘汰后编号也不复用，
                # 旧对话里的 S 编号不会指到别的网址。计数器在打开数据库时就为已有会话补齐（0.5.6 S3）。
                ck = f"sn:{session_id}"
                if cur.execute("SELECT 1 FROM counters WHERE name=?", (ck,)).fetchone() is None:
                    base = cur.execute("SELECT COALESCE(MAX(sn), 0) FROM entries WHERE session_id=?",
                                       (session_id,)).fetchone()[0]
                    cur.execute("INSERT INTO counters VALUES (?, ?)", (ck, base))
                cur.execute("UPDATE counters SET value=value+1 WHERE name=?", (ck,))
                sn = cur.execute("SELECT value FROM counters WHERE name=?", (ck,)).fetchone()[0]
                cur.execute("INSERT INTO entries (session_id, sn, url_key, url, title, snippet, published_at, sources,"
                            " first_seen, last_seen, hits, used) VALUES (?,?,?,?,?,?,?,?,?,?,1,0)",
                            (session_id, sn, m.key, m.url, title, snippet, m.published_at,
                             json.dumps(m.sources), now, now))
                out.append(Entry(sn, m.url, title, snippet, m.published_at, m.sources, True))
            # 每会话上限：按 last_seen 淘汰（编号不复用，见上）
            n = cur.execute("SELECT COUNT(*) FROM entries WHERE session_id=?", (session_id,)).fetchone()[0]
            if n > self.per_session:
                cur.execute("DELETE FROM entries WHERE session_id=? AND sn IN (SELECT sn FROM entries WHERE"
                            " session_id=? ORDER BY last_seen ASC LIMIT ?)", (session_id, session_id, n - self.per_session))
            cur.execute("COMMIT")
        except Exception:
            cur.execute("ROLLBACK")
            raise
        return out

    def get(self, session_id: str, sn: int) -> dict[str, Any] | None:
        row = self._db.execute("SELECT sn, url, title, snippet, published_at, sources, first_seen FROM entries"
                               " WHERE session_id=? AND sn=?", (session_id, sn)).fetchone()
        if not row:
            return None
        return {"sn": row[0], "url": row[1], "title": row[2], "snippet": row[3], "published_at": row[4],
                "sources": json.loads(row[5] or "[]"), "first_seen": row[6]}

    def recent(self, session_id: str, since: float, limit: int = 6) -> list[dict[str, Any]]:
        rows = self._db.execute("SELECT sn, url, title, snippet, published_at, last_seen FROM entries"
                                " WHERE session_id=? AND last_seen>=? ORDER BY last_seen DESC, sn ASC LIMIT ?",
                                (session_id, since, limit)).fetchall()
        keys = ("sn", "url", "title", "snippet", "published_at", "last_seen")
        return [dict(zip(keys, r)) for r in rows]

    def parked(self, session_id: str, limit: int = 3) -> list[dict[str, Any]]:
        """后台完成但没有送回（过时或送回失败）的任务。"""
        rows = self._db.execute("SELECT job_id, query, sns, finished FROM jobs WHERE session_id=? AND delivered=0"
                                " AND status IN ('finished','timed_out') ORDER BY finished DESC LIMIT ?",
                                (session_id, limit)).fetchall()
        return [{"job_id": r[0], "query": r[1], "sns": json.loads(r[2] or "[]"), "finished": r[3]} for r in rows]

    def set_page(self, session_id: str, sn: int, excerpt_text: str) -> None:
        """读过正文：用正文摘录替换较短的摘要，并计一次“读取”。0.5.6：摘录脱敏；读取次数单独计（reads），
        不再混进 used（used 只表示“进了回复参考资料”的次数）。"""
        ex = redact(excerpt_text)[:600]
        self._db.execute("UPDATE entries SET snippet=CASE WHEN length(?)>length(snippet) THEN ? ELSE snippet END,"
                         " reads=reads+1 WHERE session_id=? AND sn=?", (ex, ex, session_id, sn))

    def mark_shown(self, session_id: str, sns: list[int]) -> None:
        """进过回复参考资料：used 置 1（只记一次，同一窗口内多次注入不重复累计）。"""
        if sns:
            self._db.executemany("UPDATE entries SET used=1 WHERE session_id=? AND sn=?",
                                 [(session_id, s) for s in sns])

    def clear_session(self, session_id: str) -> int:
        n = self._db.execute("DELETE FROM entries WHERE session_id=?", (session_id,)).rowcount
        self._db.execute("DELETE FROM jobs WHERE session_id=?", (session_id,))
        self._db.execute("DELETE FROM trace WHERE session_id=?", (session_id,))
        return n

    def clear_all(self) -> int:
        """配置页“清空全部记录”：账本、任务、轨迹、结果缓存。保留额度计数和来源健康（删了会让每日上限失效）。"""
        n = self._db.execute("DELETE FROM entries").rowcount
        for t in ("jobs", "trace", "cache"):
            self._db.execute(f"DELETE FROM {t}")
        return n

    def stats(self) -> dict[str, int]:
        q = lambda sql: int(self._db.execute(sql).fetchone()[0])  # noqa: E731
        return {"entries": q("SELECT COUNT(*) FROM entries"), "sessions": q("SELECT COUNT(DISTINCT session_id) FROM entries"),
                "jobs": q("SELECT COUNT(*) FROM jobs"), "cache": q("SELECT COUNT(*) FROM cache")}

    # ---- 任务 ----
    def counter(self, name: str) -> int:
        row = self._db.execute("SELECT value FROM counters WHERE name=?", (name,)).fetchone()
        return int(row[0]) if row else 0

    def counter_add(self, name: str, n: int = 1) -> None:
        self._db.execute("INSERT INTO counters VALUES (?, ?) ON CONFLICT(name) DO UPDATE SET value=value+?", (name, n, n))

    def kv_get(self, name: str) -> str | None:
        row = self._db.execute("SELECT value FROM kv WHERE name=?", (name,)).fetchone()
        return row[0] if row else None

    def kv_set(self, name: str, value: str) -> None:
        self._db.execute("INSERT INTO kv VALUES (?, ?) ON CONFLICT(name) DO UPDATE SET value=excluded.value", (name, value))

    def next_job_id(self) -> str:
        self._db.execute("INSERT INTO counters VALUES ('job', 1) ON CONFLICT(name) DO UPDATE SET value=value+1")
        return f"J{self._db.execute('SELECT value FROM counters WHERE name=?', ('job',)).fetchone()[0]}"

    def save_job(self, job: dict[str, Any]) -> None:
        self._db.execute(
            "INSERT INTO jobs VALUES (:job_id,:session_id,:query,:depth,:intent,:caller,:status,:created,:finished,"
            ":delivered,:sns,:missing,:note) ON CONFLICT(job_id) DO UPDATE SET status=excluded.status,"
            " finished=excluded.finished, delivered=excluded.delivered, sns=excluded.sns, missing=excluded.missing,"
            " note=excluded.note",
            {**job, "sns": json.dumps(job.get("sns") or []), "missing": json.dumps(job.get("missing") or [])})

    def jobs(self, session_id: str, limit: int = 10) -> list[dict[str, Any]]:
        rows = self._db.execute("SELECT job_id, query, depth, status, created, finished, delivered, sns, missing, note"
                                " FROM jobs WHERE session_id=? ORDER BY created DESC LIMIT ?",
                                (session_id, limit)).fetchall()
        keys = ("job_id", "query", "depth", "status", "created", "finished", "delivered", "sns", "missing", "note")
        return [dict(zip(keys, r)) for r in rows]

    def mark_interrupted(self) -> int:
        """D18：加载时把仍在运行/排队的任务标为 interrupted，不续跑。"""
        return self._db.execute("UPDATE jobs SET status='interrupted', finished=? WHERE status IN"
                                " ('queued','running')", (time.time(),)).rowcount

    def interrupted_undelivered(self, session_id: str) -> list[str]:
        rows = self._db.execute("SELECT job_id FROM jobs WHERE session_id=? AND status='interrupted' AND delivered=0",
                                (session_id,)).fetchall()
        return [r[0] for r in rows]

    def mark_delivered(self, job_id: str, value: int = 1) -> None:
        self._db.execute("UPDATE jobs SET delivered=? WHERE job_id=?", (value, job_id))

    # ---- 统计 / 轨迹 ----
    def trace(self, rec: dict[str, Any]) -> None:
        self._db.execute("INSERT INTO trace VALUES (:job_id,:t,:session_id,:query,:depth,:intent,:caller,:outcome,"
                         ":sync,:seconds,:attempts,:missing,:used_sns)",
                         {**rec, "attempts": json.dumps(rec.get("attempts") or [], ensure_ascii=False),
                          "missing": json.dumps(rec.get("missing") or []),
                          "used_sns": json.dumps(rec.get("used_sns") or [])})

    def get_trace(self, session_id: str, job_id: str) -> dict[str, Any] | None:
        row = self._db.execute("SELECT job_id, t, query, depth, intent, caller, outcome, sync, seconds, attempts, missing"
                               " FROM trace WHERE session_id=? AND job_id=? ORDER BY t DESC LIMIT 1",
                               (session_id, job_id)).fetchone()
        if not row:
            return None
        keys = ("job_id", "t", "query", "depth", "intent", "caller", "outcome", "sync", "seconds", "attempts", "missing")
        d = dict(zip(keys, row))
        d["attempts"] = json.loads(d["attempts"] or "[]")
        d["missing"] = json.loads(d["missing"] or "[]")
        return d

    def prune(self, keep_days: int, trace_days: int) -> None:
        now = time.time()
        self._db.execute("DELETE FROM entries WHERE last_seen < ?", (now - keep_days * 86400,))
        self._db.execute("DELETE FROM jobs WHERE created < ?", (now - keep_days * 86400,))
        self._db.execute("DELETE FROM trace WHERE t < ?", (now - trace_days * 86400,))
        self._db.execute("DELETE FROM cache WHERE t < ?", (now - 86400,))
        # 每日计数只保留 7 天
        cutoff = time.strftime("%Y-%m-%d", time.localtime(now - 7 * 86400))
        self._db.execute("DELETE FROM counters WHERE name LIKE 'daily:%' AND substr(name, 7, 10) < ?", (cutoff,))

    # ---- 结果缓存（时效档，第 3 阶段） ----
    def cache_get(self, key: str, max_age: float) -> str | None:
        row = self._db.execute("SELECT t, payload FROM cache WHERE key=?", (key,)).fetchone()
        if not row or time.time() - row[0] > max_age:
            return None
        return row[1]

    def cache_put(self, key: str, payload: str) -> None:
        self._db.execute("INSERT INTO cache VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET t=excluded.t,"
                         " payload=excluded.payload", (key, time.time(), payload))
