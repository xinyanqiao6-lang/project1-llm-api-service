"""三态熔断器单测：closed -> open -> half-open -> closed 的完整状态机。"""
import time

from app.llm_client import CircuitBreaker


def test_closed_to_open_after_threshold():
    cb = CircuitBreaker(fail_threshold=3, cooldown=30)
    for _ in range(3):
        assert cb.allow() is True
        cb.record_failure()
    assert cb.state == "open"


def test_open_blocks_requests():
    cb = CircuitBreaker(fail_threshold=3, cooldown=30)
    for _ in range(3):
        cb.allow()
        cb.record_failure()
    assert cb.allow() is False  # open 状态下快速失败


def test_half_open_after_cooldown():
    cb = CircuitBreaker(fail_threshold=3, cooldown=0.1)
    for _ in range(3):
        cb.allow()
        cb.record_failure()
    assert cb.state == "open"
    time.sleep(0.15)  # 等冷却期结束
    assert cb.allow() is True   # 放一个探测请求
    assert cb.state == "half-open"


def test_half_open_success_recovers():
    cb = CircuitBreaker(fail_threshold=3, cooldown=0.1)
    for _ in range(3):
        cb.allow()
        cb.record_failure()
    time.sleep(0.15)
    assert cb.allow() is True
    cb.record_success()          # 探测成功
    assert cb.state == "closed"


def test_half_open_failure_reopens():
    cb = CircuitBreaker(fail_threshold=3, cooldown=0.1)
    for _ in range(3):
        cb.allow()
        cb.record_failure()
    time.sleep(0.15)
    assert cb.allow() is True
    cb.record_failure()          # 探测失败
    assert cb.state == "open"


def test_success_resets_consecutive_failures():
    cb = CircuitBreaker(fail_threshold=3, cooldown=30)
    cb.record_failure()
    cb.record_failure()
    cb.record_success()          # 成功清零计数
    cb.record_failure()
    cb.record_failure()
    assert cb.state == "closed"   # 累计 2 次失败，未到阈值 3
