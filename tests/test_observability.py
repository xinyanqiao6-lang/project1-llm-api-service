"""可观测层与模型路由的单测。

重点验证两件容易出错的事：
1. 成本计算不能"假装免费"——未配置单价的模型必须标 cost_estimated=False；
2. trace 聚合的延迟分位/命中率/按模型分组必须算对，否则运维看板会误导人。
"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.observability import (  # noqa: E402
    CHEAP_MODEL,
    SIMPLE_MAX_TOKENS,
    STRONG_MODEL,
    RequestTrace,
    TraceStore,
    compute_cost,
    route_model,
)


@pytest.fixture()
def store(tmp_path):
    return TraceStore(tmp_path / "t.db")


# ---------- 成本计算 ----------

def test_cost_of_free_model_is_zero_but_estimated():
    cost, estimated = compute_cost("Qwen/Qwen2.5-7B-Instruct", 1000, 500)
    assert cost == 0.0
    assert estimated is True


def test_cost_of_unknown_model_is_flagged_not_faked():
    """未配置单价的模型必须标 estimated=False——不能假装成本是 0。"""
    cost, estimated = compute_cost("some/unknown-model", 1000, 500)
    assert cost == 0.0
    assert estimated is False


def test_cost_of_paid_model():
    from app.observability import PRICE_PER_MTOK
    PRICE_PER_MTOK["test/paid"] = 10.0        # 10 元 / 百万 token
    try:
        cost, estimated = compute_cost("test/paid", 1_000_000, 0)
        assert cost == pytest.approx(10.0)
        assert estimated is True
    finally:
        PRICE_PER_MTOK.pop("test/paid", None)


# ---------- 模型路由 ----------

def test_client_specified_model_is_respected():
    """客户端显式指定模型时不做路由——尊重客户端，别自作聪明。"""
    m, why = route_model("my/custom-model", 1024)
    assert m == "my/custom-model"
    assert why == "client-specified"


def test_simple_request_routes_to_cheap_model():
    m, why = route_model(None, SIMPLE_MAX_TOKENS)
    assert m == CHEAP_MODEL
    assert "simple" in why


def test_complex_request_routes_to_strong_model():
    m, why = route_model(None, 1024)
    assert m == STRONG_MODEL
    assert "complex" in why


def test_routing_is_explainable():
    """路由必须给得出理由，否则线上出问题无从倒查。"""
    for kwargs in ({"requested": None, "max_tokens": 32},
                   {"requested": None, "max_tokens": 2048},
                   {"requested": "x", "max_tokens": 32}):
        _, why = route_model(kwargs["requested"], kwargs["max_tokens"])
        assert why and isinstance(why, str)


# ---------- trace 落库与聚合 ----------

def _mk(store, **kw):
    tr = RequestTrace(request_id=kw.pop("rid", "r0"), **kw)
    store.write(tr)
    return tr


def test_store_roundtrip(store):
    _mk(store, rid="a", model="m1", t_total_ms=10.0, total_tokens=100)
    _mk(store, rid="b", model="m1", t_total_ms=20.0, total_tokens=200)
    rows = store.recent(10)
    assert {r["request_id"] for r in rows} == {"a", "b"}


def test_aggregate_empty_is_safe(store):
    agg = store.aggregate()
    assert agg["count"] == 0          # 空库不能抛异常，否则看板直接挂


def test_aggregate_latency_percentiles(store):
    for i in range(1, 101):
        _mk(store, rid=f"r{i}", t_total_ms=float(i), total_tokens=10, cache_hit=False)
    agg = store.aggregate()
    assert agg["count"] == 100
    assert agg["latency_ms"]["p50"] == pytest.approx(50.0, abs=2)
    assert agg["latency_ms"]["p95"] == pytest.approx(95.0, abs=2)
    assert agg["latency_ms"]["p99"] == pytest.approx(99.0, abs=2)


def test_aggregate_cache_hit_rate(store):
    _mk(store, rid="h1", cache_hit=True, total_tokens=10)
    _mk(store, rid="h2", cache_hit=False, total_tokens=10)
    agg = store.aggregate()
    assert agg["cache_hit_rate"] == pytest.approx(0.5)


def test_aggregate_groups_by_model(store):
    _mk(store, rid="m1a", model="qwen", total_tokens=100)
    _mk(store, rid="m1b", model="qwen", total_tokens=100)
    _mk(store, rid="m2a", model="deepseek", total_tokens=500)
    agg = store.aggregate()
    assert agg["by_model"]["qwen"]["requests"] == 2
    assert agg["by_model"]["qwen"]["tokens"] == 200
    assert agg["by_model"]["deepseek"]["tokens"] == 500
    assert agg["total_tokens"] == 700
    assert agg["avg_tokens_per_request"] == pytest.approx(233.3, abs=1)


def test_trace_captures_cost(store):
    _mk(store, rid="c1", model="Qwen/Qwen2.5-7B-Instruct",
        total_tokens=1000, cost_cny=0.0)
    agg = store.aggregate()
    assert agg["total_cost_cny"] == 0.0
    # 免费模型成本为 0 是事实，但 total_tokens 必须仍然被记录
    assert agg["total_tokens"] == 1000


def test_same_request_id_overwrites(store):
    """trace id 主键冲突时应覆盖而不是累积——重试场景下会重复写同一 id。"""
    _mk(store, rid="dup", t_total_ms=1.0)
    _mk(store, rid="dup", t_total_ms=99.0)
    agg = store.aggregate()
    assert agg["count"] == 1
    assert agg["latency_ms"]["p50"] == pytest.approx(99.0)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))