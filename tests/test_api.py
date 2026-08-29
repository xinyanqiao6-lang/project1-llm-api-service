"""API 冒烟测试：健康检查与运行指标端点。"""
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "cache_backend" in body
    assert "circuit" in body  # 熔断器状态已暴露


def test_stats():
    r = client.get("/stats")
    assert r.status_code == 200
    body = r.json()
    assert "cache" in body
    assert "rate_limit" in body
    assert "circuit_breaker" in body
    assert body["circuit_breaker"]["state"] in ("closed", "open", "half-open")
