"""指标计算与可信度判定——「这个数字能不能写进简历」由这里说了算。

为什么单独抽一个模块（而不是把公式散在压测脚本里）：

1. 压测数字写进简历 = 对外承诺，面试官会追问"怎么测的"；
   口径必须集中定义、必须能被单测覆盖，不能靠人肉记忆。
2. 上一版踩过的三个坑（见下）全部源于"口径没定义清楚"：
   - 把被限流拒绝的 429 计入 QPS -> 吞吐虚高一个数量级；
   - 延迟分位数混入 429 的 1ms 响应 -> P50 被拉到 3ms 的假象；
   - 把服务端**进程级累计**的 /stats 当成本轮值 -> 命中率虚高到 98.5%。
   本模块把这三条固化成函数 + 单测，改不动。
"""
from __future__ import annotations

# 判定阈值：低于这个样本量的延迟分位数不写进简历（小样本噪声大）
MIN_LATENCY_SAMPLES = 30


# ---------- 基础统计 ----------
def percentile(sorted_values: list[float], p: float) -> float:
    """最近秩法分位数（p=0.95 取第 95 百分位）。空列表返回 0.0。"""
    if not sorted_values:
        return 0.0
    idx = min(int(len(sorted_values) * p), len(sorted_values) - 1)
    return float(sorted_values[idx])


def summarize_latencies(latencies_ms: list[float]) -> dict:
    """延迟汇总。**入参应当只包含成功（HTTP 200）请求的延迟**——
    把 429/5xx 混进来会让分位数失真，这是上一版的 bug 之一。"""
    if not latencies_ms:
        return {"count": 0}
    s = sorted(latencies_ms)
    return {
        "count": len(s),
        "avg": round(sum(s) / len(s), 1),
        "min": round(s[0], 1),
        "P50": round(percentile(s, 0.50), 1),
        "P95": round(percentile(s, 0.95), 1),
        "P99": round(percentile(s, 0.99), 1),
        "max": round(s[-1], 1),
    }


def compute_qps(count: int, duration_s: float) -> float:
    """QPS = 计数 / **实际**压测耗时。

    注意两个坑：
    - 分母用实际耗时，不要用配置里的 --duration（worker 提前退出会偏小，QPS 虚高）；
    - 分子默认只算「成功请求」。若把被限流拒绝的 429 也算进分子，
      得到的不是吞吐，而是"拒绝风暴的速率"。
    """
    if duration_s <= 0:
        return 0.0
    return round(count / duration_s, 1)


# ---------- 服务端计数器增量 ----------
_COUNTER_KEYS = ("hits", "misses", "rejected_total")


def extract_counters(stats: dict | None) -> dict:
    """从 /stats 响应里取出三个计数器。

    /stats 的结构是**嵌套**的：
        {"cache": {"hits":.., "misses":..}, "rate_limit": {"rejected_total":..}}
    之前直接对整个 /stats 响应取顶层 key，导致 hits/misses 永远读到 0
    （差值恒为 0，缓存命中率恒为 None）。这个函数把结构知识收在一处。

    同时兼容两种形态：
    - 嵌套形态（/stats 原始响应）
    - 抽平形态（{"hits":..,"misses":..,"rejected_total":..}）
      —— 顶层同时存在三键时按抽平处理，避免调用方各自再拆一次结构。
    """
    stats = stats or {}
    if all(k in stats for k in _COUNTER_KEYS):
        return {k: int(stats.get(k, 0) or 0) for k in _COUNTER_KEYS}
    cache = stats.get("cache") or {}
    rl = stats.get("rate_limit") or {}
    return {
        "hits": int(cache.get("hits", 0) or 0),
        "misses": int(cache.get("misses", 0) or 0),
        "rejected_total": int(rl.get("rejected_total", 0) or 0),
    }


def diff_counters(before: dict, after: dict) -> dict:
    """/stats 是**进程启动以来的累计值**，压测前后各拉一次做差才是本轮增量。

    上一版直接拿压测后的累计值当本轮值，导致多轮压测的数字互相污染、
    缓存命中率虚高到 98.5%（真实值取决于请求分布，repeat=0.9 时才接近 0.9）。

    入参建议直接传 /stats 响应，本函数内部会调 extract_counters 拆结构。
    """
    b, a = extract_counters(before), extract_counters(after)
    out = {}
    for k in _COUNTER_KEYS:
        out[k] = max(0, a[k] - b[k])  # max(0,·) 兜住服务重启导致的计数回退
    return out


def cache_hit_rate(delta: dict) -> float | None:
    """命中率 = hits / (hits + misses)，用本轮增量算。无数据返回 None（不要写 0）。"""
    denom = delta.get("hits", 0) + delta.get("misses", 0)
    if denom <= 0:
        return None
    return round(delta["hits"] / denom, 4)


# ---------- 可信度判定 ----------
def judge_run(
    *,
    mock_mode: bool,
    cache_backend: str,
    latency_samples: int,
    rejected: int,
    redis_enabled: bool,
) -> dict:
    """判定本轮各项指标能否写进简历。**分指标判定，不搞整轮一刀切**——
    因为缓存后端退化只影响缓存指标，不影响真实模型下的吞吐与延迟。

    返回形如：
        {"metric_validity": {"throughput": "valid", "latency": "valid",
                             "cache_hit_rate": "invalid"},
         "reasons": {...}}
    """
    reasons: dict[str, list[str]] = {
        "throughput": [], "latency": [], "cache_hit_rate": [],
    }

    # 吞吐 / 延迟：必须打到真实模型
    if mock_mode:
        reasons["throughput"].append("服务处于 MOCK_MODE，数字只验证了链路，未真实调用模型")
        reasons["latency"].append("MOCK_MODE 下延迟不代表真实推理耗时")

    # 延迟分位数：样本量门槛
    if latency_samples < MIN_LATENCY_SAMPLES:
        reasons["latency"].append(
            f"成功样本仅 {latency_samples} 条，低于门槛 {MIN_LATENCY_SAMPLES}，分位数噪声过大"
        )

    # 缓存命中率：必须是真 Redis，否则只能叫"内存回退缓存"
    if not redis_enabled or cache_backend != "redis":
        reasons["cache_hit_rate"].append(
            f"缓存后端为 {cache_backend}，不是 Redis；该数字只能表述为"
            f"「自研进程内缓存」，不可写「Redis 缓存命中率」"
        )

    if rejected > 0:
        reasons["throughput"].append(
            f"本轮有 {rejected} 次请求被限流拒绝；吞吐需同时说明拒绝率，"
            f"否则会被误读为网关处理能力上限"
        )

    def verdict(key: str) -> str:
        return "invalid" if reasons[key] else "valid"

    return {
        "metric_validity": {k: verdict(k) for k in reasons},
        "reasons": {k: v for k, v in reasons.items() if v},
        "min_latency_samples": MIN_LATENCY_SAMPLES,
    }


def is_resume_grade(judgement: dict) -> bool:
    """三项指标全部 valid 才算「可写进简历」。"""
    return all(v == "valid" for v in judgement["metric_validity"].values())


# ---------- 代理劫持检测 ----------
def looks_proxied(request_url: str, expected_host: str) -> bool:
    """判断一次请求是否被系统代理劫持（httpx trust_env=True 的陷阱）。

    实测踩坑：这台机器设了 `HTTP_PROXY=127.0.0.1:55012`，而 `NO_PROXY` 不含
    127.0.0.1。httpx 默认 `trust_env=True`，于是所有打向本地网关的压测请求
    全被发去了代理，代理以 absolute-URI 形式收到后一律回 404：
        实际收到的请求行： POST http%3A//127.0.0.1%3A8000/v1/chat/completions 404
    表现是"压测跑完几乎没有成功请求"，但代码看起来完全正常。

    判据：URL 里出现编码后的 "://"（%3A//），说明整条 URL 被当成 path 塞给了代理。
    正常直连时 path 应该是 /v1/chat/completions 这种纯路径。
    """
    if not request_url:
        return False
    return "%3A//" in request_url or "%3a//" in request_url


# ---------- 结果文件命名 ----------
def new_result_path(results_dir, stamp: str) -> "object":
    """结果文件名带上可信度标记，肉眼即可分辨哪轮能引用。

    命名规则： loadtest_<stamp>_<resume|partial|mock>.json
    """
    from pathlib import Path

    return Path(results_dir) / f"loadtest_{stamp}.json"
