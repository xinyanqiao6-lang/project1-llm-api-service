"""指标口径单测 —— 防止 v1 的三个 bug 复发。

这三个 bug 的共同特征：代码"能跑"、结果"好看"，但数字是假的。
所以必须用单测把口径钉死。

运行：python -m pytest tests/test_metrics.py -v
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.metrics import (  # noqa: E402
    MIN_LATENCY_SAMPLES,
    cache_hit_rate,
    compute_qps,
    diff_counters,
    extract_counters,
    is_resume_grade,
    judge_run,
    looks_proxied,
    percentile,
    summarize_latencies,
)


# ---------- 硬伤 4：系统代理劫持本地压测请求 ----------
def test_looks_proxied_detects_encoded_absolute_uri():
    """被代理劫持时，服务端收到的请求行形如（实测抓包）：
    POST http%3A//127.0.0.1%3A8000/v1/chat/completions HTTP/1.1  -> 404
    整条 URL 被 percent-encode 后当成了 path。"""
    hijacked = "http://127.0.0.1:55012/http%3A//127.0.0.1%3A8000/v1/chat/completions"
    assert looks_proxied(hijacked, "127.0.0.1:8000") is True
    assert looks_proxied("http://h/http%3A//127.0.0.1%3A8000/v1/chat", "127.0.0.1:8000") is True
    assert looks_proxied("http://h/http%3a//127.0.0.1%3a8000/v1/chat", "127.0.0.1:8000") is True


def test_looks_proxied_passes_for_direct_requests():
    """正常直连时 path 是纯路径，不能误报。"""
    assert looks_proxied("http://127.0.0.1:8000/v1/chat/completions", "127.0.0.1:8000") is False
    assert looks_proxied("http://127.0.0.1:8000/stats", "127.0.0.1:8000") is False
    assert looks_proxied("", "127.0.0.1:8000") is False


# ---------- 硬伤 1b：/stats 是嵌套结构，差值读顶层恒为 0 ----------
def test_extract_counters_reads_nested_stats():
    """真实 /stats 结构是 {"cache":{"hits":..},"rate_limit":{"rejected_total":..}}。
    之前直接取顶层 key，导致 hits/misses 永远读到 0、命中率恒为 None。"""
    stats = {"cache": {"hits": 300, "misses": 700, "backend": "redis"},
             "rate_limit": {"rejected_total": 42, "limit": 10}}
    assert extract_counters(stats) == {"hits": 300, "misses": 700, "rejected_total": 42}


def test_extract_counters_tolerates_missing_sections():
    assert extract_counters(None) == {"hits": 0, "misses": 0, "rejected_total": 0}
    assert extract_counters({}) == {"hits": 0, "misses": 0, "rejected_total": 0}


def test_diff_counters_on_real_nested_payload():
    """用真实的嵌套结构做差：命中 300/700 → 命中率 0.3。"""
    before = {"cache": {"hits": 1000, "misses": 500},
              "rate_limit": {"rejected_total": 10}}
    after = {"cache": {"hits": 1300, "misses": 1200},
             "rate_limit": {"rejected_total": 60}}
    d = diff_counters(before, after)
    assert d == {"hits": 300, "misses": 700, "rejected_total": 50}
    assert cache_hit_rate(d) == 0.3


# ---------- 硬伤 1：累计值当本轮值 ----------
def test_diff_counters_on_flat_payload_still_works():
    """传入已抽平的计数器（向后兼容）。"""
    before = {"hits": 1000, "misses": 500, "rejected_total": 10}
    after = {"hits": 1500, "misses": 700, "rejected_total": 60}
    d = diff_counters(before, after)
    assert d == {"hits": 500, "misses": 200, "rejected_total": 50}


def test_diff_counters_handles_counter_reset():
    """服务重启会让累计值归零，差值不能变成负数。"""
    d = diff_counters({"hits": 100, "misses": 50, "rejected_total": 5},
                      {"hits": 10, "misses": 5, "rejected_total": 0})
    assert d == {"hits": 0, "misses": 0, "rejected_total": 0}


def test_diff_counters_missing_keys_defaults_to_zero():
    d = diff_counters({}, {})
    assert d == {"hits": 0, "misses": 0, "rejected_total": 0}


# ---------- 缓存命中率口径 ----------
def test_cache_hit_rate_uses_delta():
    """30% 重复率 → 命中率应接近 0.3，而不是被累计值刷成 0.985。"""
    d = {"hits": 300, "misses": 700, "rejected_total": 0}
    assert cache_hit_rate(d) == 0.3


def test_cache_hit_rate_none_when_no_data():
    """无数据时返回 None 而不是 0——0 会被误读成"命中率零"。"""
    assert cache_hit_rate({"hits": 0, "misses": 0}) is None


# ---------- 硬伤 2：429 计入 QPS 与延迟 ----------
def test_success_qps_excludes_rejected():
    """1000 次请求里 600 次被拒、400 次成功，耗时 10s：
    成功吞吐 40 QPS，而不是把 600 算进去得到的 100 QPS。"""
    assert compute_qps(400, 10) == 40.0


def test_latency_percentiles_exclude_429():
    """40 条真实推理 1000ms + 600 条 429 的 1ms 响应：
    只看成功请求 P50 应为 1000ms；若混入 429 会被拉到 1ms 的假象。"""
    lat_ok = [1000.0] * 40
    mixed = [1.0] * 600 + lat_ok
    assert summarize_latencies(lat_ok)["P50"] == 1000.0
    # 反证：混入 429 后 P50 确实被拉坏，说明为什么必须分桶
    assert summarize_latencies(mixed)["P50"] == 1.0


def test_latency_summary_handles_empty():
    assert summarize_latencies([]) == {"count": 0}


def test_percentile_empty_returns_zero():
    assert percentile([], 0.95) == 0.0


def test_qps_uses_actual_duration():
    """配置 30s 但实际只跑了 25s（worker 提前结束）：
    分母必须是实际耗时，否则 QPS 偏高 20%。"""
    assert compute_qps(250, 25) == 10.0
    assert compute_qps(250, 30) == 8.3


# ---------- 硬伤 3：无出处数字 / 内存缓存冒充 Redis ----------
def test_memory_fallback_backend_invalidates_cache_metric():
    j = judge_run(mock_mode=False, cache_backend="in-memory(fallback)",
                  latency_samples=100, rejected=0, redis_enabled=False)
    assert j["metric_validity"]["cache_hit_rate"] == "invalid"
    assert any("Redis" in r for rs in j["reasons"].values() for r in rs)


def test_real_redis_backend_validates_cache_metric():
    j = judge_run(mock_mode=False, cache_backend="redis",
                  latency_samples=100, rejected=0, redis_enabled=True)
    assert j["metric_validity"]["cache_hit_rate"] == "valid"
    assert is_resume_grade(j) is True


def test_mock_mode_invalidates_throughput_and_latency():
    j = judge_run(mock_mode=True, cache_backend="redis",
                  latency_samples=100, rejected=0, redis_enabled=True)
    assert j["metric_validity"]["throughput"] == "invalid"
    assert j["metric_validity"]["latency"] == "invalid"
    assert is_resume_grade(j) is False


def test_small_sample_invalidates_latency_only():
    """样本量不足只影响延迟，不影响吞吐判定（分指标判定，不一刀切）。"""
    j = judge_run(mock_mode=False, cache_backend="redis",
                  latency_samples=MIN_LATENCY_SAMPLES - 1, rejected=0, redis_enabled=True)
    assert j["metric_validity"]["latency"] == "invalid"
    assert j["metric_validity"]["throughput"] == "valid"
    assert j["metric_validity"]["cache_hit_rate"] == "valid"
    assert is_resume_grade(j) is False


def test_rejected_requests_flag_throughput():
    """有 429 拒绝时吞吐必须附说明，不能直接当处理能力上限报。"""
    j = judge_run(mock_mode=False, cache_backend="redis",
                  latency_samples=100, rejected=500, redis_enabled=True)
    assert j["metric_validity"]["throughput"] == "invalid"
    assert any("限流" in r for r in j["reasons"]["throughput"])


# ---------- 结论性保障 ----------
def test_resume_grade_requires_all_metrics_valid():
    """三项全 valid 才算可写进简历。"""
    ok = judge_run(mock_mode=False, cache_backend="redis",
                   latency_samples=100, rejected=0, redis_enabled=True)
    assert is_resume_grade(ok) is True

    for bad_kwargs in [
        dict(mock_mode=True, cache_backend="redis", latency_samples=100, rejected=0, redis_enabled=True),
        dict(mock_mode=False, cache_backend="in-memory(fallback)", latency_samples=100, rejected=0, redis_enabled=False),
        dict(mock_mode=False, cache_backend="redis", latency_samples=5, rejected=0, redis_enabled=True),
    ]:
        assert is_resume_grade(judge_run(**bad_kwargs)) is False


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
