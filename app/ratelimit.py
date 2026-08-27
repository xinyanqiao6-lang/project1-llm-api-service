"""限流：滑动窗口算法（Sliding Window）。

面试必考题——"限流算法区别？"参考答法：
- 固定窗口：按自然分钟计数，临界点会突发 2 倍流量（59s 和 61s 各打满 N 次）；
- 滑动窗口：只统计"最近 60 秒内"的请求，随时间平滑滑走，无临界问题；
- 令牌桶：允许一定突发（桶里攒的令牌），适合削峰填谷；
- 漏桶：恒定速率流出，绝对平滑但无法应对合理突发。

本服务用 Redis 有序集合（ZSET）实现分布式滑动窗口：
score 存时间戳，每来一个请求先删掉窗口外的旧记录，再数当前窗口内的数量。
"""
import time

import redis as redis_lib

from . import config


class SlidingWindowLimiter:
    def __init__(self, limit: int, window: int):
        self.limit = limit
        self.window = window  # 秒
        self.rejected = 0     # 被限流拒绝的总请求数（进 /stats）
        self._redis = None
        self._memory: dict[str, list[float]] = {}  # 回退方案
        try:
            r = redis_lib.Redis.from_url(
                config.REDIS_URL, socket_connect_timeout=1, decode_responses=True
            )
            r.ping()
            self._redis = r
        except Exception:
            self._redis = None

    def allow(self, client_id: str) -> tuple[bool, int]:
        """返回 (是否放行, 当前窗口内请求数)。"""
        now = time.time()
        window_start = now - self.window

        if self._redis is not None:
            key = f"rl:{client_id}"
            pipe = self._redis.pipeline()
            pipe.zremrangebyscore(key, "-inf", window_start)  # 清理窗口外旧记录
            pipe.zcard(key)                                    # 当前窗口内数量
            pipe.zadd(key, {str(now): now})                    # 记录本次请求
            pipe.expire(key, self.window + 1)                  # 防止 key 泄漏
            results = pipe.execute()
            count = results[1]
        else:
            ts = self._memory.setdefault(client_id, [])
            ts[:] = [t for t in ts if t > window_start]
            count = len(ts)
            ts.append(now)

        if count >= self.limit:
            # 注意：这里已把本次请求记进去了，超限即拒绝
            self.rejected += 1
            return False, count
        return True, count + 1

    def stats(self) -> dict:
        return {
            "algorithm": "sliding-window",
            "backend": "redis" if self._redis is not None else "in-memory(fallback)",
            "limit": self.limit,
            "window_seconds": self.window,
            "rejected_total": self.rejected,
        }


limiter = SlidingWindowLimiter(config.RATE_LIMIT, config.RATE_WINDOW)
