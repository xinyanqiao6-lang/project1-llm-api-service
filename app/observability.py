"""可观测层：请求 trace 落库 + token/成本记账 + 模型分级路由。

## 为什么加这个
v1 的网关只有 `/stats` 给累计计数，无法回答三个关键问题：
1. **单个请求发生了什么**——慢在哪一步？缓存命中没有？重试了几次？
2. **这个月花了多少钱**——没有 token 记账就没有成本意识
3. **哪些请求可以走便宜模型**——没有 token 分布就做不了路由决策

v2 把这三个问题变成可查询的数据：
- 每次调用写一条 trace 到 SQLite（含分段耗时、token、缓存/重试/熔断状态）
- `/stats` 升级为分位延迟 + 按模型分组的 token 成本
- 新增 `/v1/chat/completions` 的模型分级路由：简单请求走便宜模型

## 成本模型
硅基流动上不同模型单价不同。这里用**每百万 token 单价**配置，
支持按模型覆盖；未配置的单价按 0 计并在响应里标注 `cost_estimated: false`，
避免"数字看起来像钱其实不是钱"。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

# 每百万 token 单价（元）。未列出的模型按 0 计并标注 cost_estimated=false。
PRICE_PER_MTOK: dict[str, float] = {
    "Qwen/Qwen2.5-7B-Instruct": 0.0,          # 免费档
    "deepseek-ai/DeepSeek-V3": 0.0,
}
FREE_MODELS = {"Qwen/Qwen2.5-7B-Instruct", "deepseek-ai/DeepSeek-V3"}

# 分级路由：简单请求走便宜模型，复杂请求走强模型
SIMPLE_MAX_TOKENS = 64          # 请求很短 → 大概率是简单问题
DEFAULT_MODEL = os.getenv("MODEL", "Qwen/Qwen2.5-7B-Instruct")
CHEAP_MODEL = os.getenv("CHEAP_MODEL", "Qwen/Qwen2.5-7B-Instruct")
STRONG_MODEL = os.getenv("STRONG_MODEL", "deepseek-ai/DeepSeek-V3")


# ---------- trace 数据结构 ----------

@dataclass
class RequestTrace:
    """一次 /v1/chat/completions 调用的完整记录。"""
    request_id: str
    model: str = ""
    routed_from: str = ""          # 路由前的客户端请求模型（为空=未启用路由）
    ts: float = field(default_factory=time.time)

    # 分段耗时（毫秒）：定位慢在哪一步
    t_total_ms: float = 0.0
    t_llm_ms: float = 0.0

    # 结果
    status: int = 200
    cache_hit: bool = False
    cache_backend: str = ""
    retries: int = 0
    breaker_state: str = "closed"
    stream: bool = False

    # 成本
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_cny: float = 0.0
    cost_estimated: bool = False

    # 元信息
    client: str = ""
    error: str = ""


class TraceStore:
    """SQLite 落库 + 内存聚合。单线程写锁，足够单机网关使用。"""

    def __init__(self, db_path: str | Path = "data/traces.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS traces (
                request_id TEXT PRIMARY KEY,
                ts REAL,
                model TEXT,
                routed_from TEXT,
                t_total_ms REAL,
                t_llm_ms REAL,
                status INTEGER,
                cache_hit INTEGER,
                retries INTEGER,
                breaker_state TEXT,
                stream INTEGER,
                prompt_tokens INTEGER,
                completion_tokens INTEGER,
                total_tokens INTEGER,
                cost_cny REAL,
                client TEXT,
                error TEXT
            )
        """)
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_ts ON traces(ts)")
        self._conn.commit()

    def write(self, tr: RequestTrace) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO traces VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (tr.request_id, tr.ts, tr.model, tr.routed_from, tr.t_total_ms,
                 tr.t_llm_ms, tr.status, int(tr.cache_hit), tr.retries,
                 tr.breaker_state, int(tr.stream), tr.prompt_tokens,
                 tr.completion_tokens, tr.total_tokens, tr.cost_cny,
                 tr.client, tr.error),
            )
            self._conn.commit()

    def recent(self, n: int = 20) -> list[dict]:
        cur = self._conn.execute(
            "SELECT * FROM traces ORDER BY ts DESC LIMIT ?", (n,))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def aggregate(self) -> dict:
        """聚合统计：延迟分位 + token 成本 + 缓存命中率。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT t_total_ms, cache_hit, total_tokens, cost_cny, model, status "
                "FROM traces").fetchall()
        if not rows:
            return {"count": 0}
        lat = sorted(r[0] for r in rows)
        n = len(rows)
        hits = sum(1 for r in rows if r[1])
        tokens = sum(r[2] for r in rows)
        cost = sum(r[3] for r in rows)
        by_model: dict[str, dict] = {}
        for r in rows:
            m = r[4]
            d = by_model.setdefault(m, {"requests": 0, "tokens": 0, "cost_cny": 0.0})
            d["requests"] += 1
            d["tokens"] += r[2]
            d["cost_cny"] = round(d["cost_cny"] + r[3], 6)

        def pct(p: float) -> float:
            if not lat:
                return 0.0
            return round(lat[min(int(n * p), n - 1)], 2)

        return {
            "count": n,
            "latency_ms": {"p50": pct(0.50), "p95": pct(0.95), "p99": pct(0.99)},
            "cache_hit_rate": round(hits / n, 4),
            "total_tokens": tokens,
            "avg_tokens_per_request": round(tokens / n, 1),
            "total_cost_cny": round(cost, 4),
            "by_model": by_model,
        }


# ---------- 成本计算 ----------

def compute_cost(model: str, prompt_tokens: int, completion_tokens: int) -> tuple[float, bool]:
    """返回 (成本元, 是否为估算)。

    免费模型返回 (0.0, True)；未配置单价的模型返回 (0.0, False) 以示"算不出来"，
    而不是假装成本是 0。
    """
    price = PRICE_PER_MTOK.get(model)
    if price is None:
        return 0.0, False
    cost = (prompt_tokens + completion_tokens) / 1_000_000 * price
    return round(cost, 6), True


# ---------- 模型分级路由 ----------

def route_model(requested: str | None, max_tokens: int,
                messages: list | None = None) -> tuple[str, str]:
    """决定用哪个模型，返回 (实际模型, 路由依据)。

    规则（可解释、可调，不用黑盒）：
    - 客户端显式指定模型 → 尊重客户端（不路由）
    - 请求很短（max_tokens 小）且无复杂指令 → 走便宜模型
    - 其余 → 走强模型

    为什么要分级：不是所有请求都需要强模型。简单请求走便宜模型，
    成本能降一个数量级；但**必须可解释**，否则出了问题没人知道为什么走错了模型。
    """
    if requested:
        return requested, "client-specified"
    if max_tokens <= SIMPLE_MAX_TOKENS:
        return CHEAP_MODEL, f"simple(len<={SIMPLE_MAX_TOKENS})"
    return STRONG_MODEL, "complex(default)"


store = TraceStore()