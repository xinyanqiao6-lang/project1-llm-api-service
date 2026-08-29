"""滑动窗口限流单测：窗口内放行、超限拒绝、客户端隔离。"""
from app.ratelimit import SlidingWindowLimiter


def _mem_limiter(limit: int, window: int) -> SlidingWindowLimiter:
    """强制走内存回退后端，保证单测不依赖 Redis。"""
    lim = SlidingWindowLimiter(limit, window)
    lim._redis = None
    return lim


def test_allow_within_limit():
    lim = _mem_limiter(3, 60)
    for _ in range(3):
        allowed, _ = lim.allow("c1")
        assert allowed is True


def test_reject_when_exceeded():
    lim = _mem_limiter(3, 60)
    for _ in range(3):
        lim.allow("c1")
    allowed, count = lim.allow("c1")
    assert allowed is False
    assert count == 3


def test_different_clients_isolated():
    lim = _mem_limiter(2, 60)
    lim.allow("A")
    lim.allow("A")          # A 用满
    allowed, _ = lim.allow("B")  # B 独立计数
    assert allowed is True


def test_rejected_counter_increments():
    lim = _mem_limiter(1, 60)
    lim.allow("c1")         # 用满
    lim.allow("c1")         # 拒绝
    assert lim.rejected == 1


def test_window_slides():
    """窗口外的旧记录应被清掉，不影响新窗口计数（滑动窗口核心特性）。"""
    lim = _mem_limiter(2, 60)
    lim._memory["c1"] = [0.0]  # 伪造一条 1970 年的过期记录
    allowed, count = lim.allow("c1")
    assert allowed is True
    assert count == 1
