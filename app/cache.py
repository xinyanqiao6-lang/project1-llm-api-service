"""缓存层：优先 Redis，Redis 不可用时自动回退到进程内存缓存。

面试必考题——"Redis vs 本地内存？"参考答法：
1. 多实例共享：服务扩到多副本时，本地缓存各存一份、命中率被稀释，Redis 是集中式的；
2. 重启不丢：本地缓存随进程消亡，Redis 独立部署可持久化；
3. 统一淘汰：TTL / 内存上限策略集中在 Redis 管；
4. 代价是多一次网络往返（本机 ~1ms 级），远小于一次 LLM 推理（秒级），划算。

命中策略：对 (model + messages + temperature) 做 SHA256 当缓存 key，
同样的问题直接返回缓存，省一次推理。
"""
import hashlib
import json
import time

import redis as redis_lib

from . import config


class Cache:
    def __init__(self):
        self.hits = 0
        self.misses = 0
        self._redis = None
        self._memory: dict[str, str] = {}
        self._expiry: dict[str, float] = {}
        try:
            r = redis_lib.Redis.from_url(
                config.REDIS_URL, socket_connect_timeout=1, decode_responses=True
            )
            r.ping()
            self._redis = r
        except Exception:
            # Redis 没起也能跑——降级到内存缓存，/stats 里会显示 backend
            self._redis = None

    # ---------- key ----------
    @staticmethod
    def make_key(model: str, messages: list, temperature: float) -> str:
        raw = json.dumps(
            {"model": model, "messages": messages, "temperature": temperature},
            ensure_ascii=False, sort_keys=True,
        )
        return "chat:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()

    # ---------- get / set ----------
    def get(self, key: str) -> str | None:
        self._gc_memory()
        if self._redis is not None:
            val = self._redis.get(key)
        else:
            val = self._memory.get(key)
        if val is None:
            self.misses += 1
        else:
            self.hits += 1
        return val

    def set(self, key: str, value: str, ttl: int | None = None):
        ttl = ttl or config.CACHE_TTL
        if self._redis is not None:
            self._redis.setex(key, ttl, value)
        else:
            self._memory[key] = value
            self._expiry[key] = time.time() + ttl

    def _gc_memory(self):
        """内存回退模式下的简易过期清理。"""
        if self._redis is not None:
            return
        now = time.time()
        expired = [k for k, t in self._expiry.items() if t <= now]
        for k in expired:
            self._memory.pop(k, None)
            self._expiry.pop(k, None)

    # ---------- 统计 ----------
    def stats(self) -> dict:
        total = self.hits + self.misses
        return {
            "backend": "redis" if self._redis is not None else "in-memory(fallback)",
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": round(self.hits / total, 4) if total else None,
        }


cache = Cache()
