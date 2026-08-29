"""LLM 客户端：封装硅基流动 API 的调用（非流式 + 流式 SSE）。

设计要点（面试会问）：
- 用 httpx.AsyncClient 异步调用，不阻塞事件循环，单进程也能撑住并发；
- 流式接口直接透传上游 SSE，逐块 yield 给上层；
- MOCK_MODE 下返回固定文本，用于无 Key 时先跑通链路；
- 上游韧性三层防线（针对免费模型偶发 60s 挂起把 P99 拖爆的问题）：
  1) 超时收紧：单次请求总超时从 60s 收到 LLM_TIMEOUT（默认 30s）；
  2) 有限重试：对超时 / 连接错误 / 5xx 重试，指数退避，4xx 不重试；
  3) 熔断器：连续失败达阈值后 open，冷却期内快速失败，half-open 放一个探测。
"""
import asyncio
import json
import time
from typing import AsyncGenerator

import httpx

from . import config

_client: httpx.AsyncClient | None = None


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(config.LLM_TIMEOUT, connect=config.LLM_CONNECT_TIMEOUT)
        )
    return _client


async def close_client():
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
        _client = None


class UpstreamUnavailableError(Exception):
    """熔断器打开 / 上游不可用时抛出，上层据此返回 503。"""


class CircuitBreaker:
    """三态熔断器：closed（正常）-> open（快速失败）-> half-open（放一个探测）。

    面试讲法：连续失败 N 次说明上游已经不可用，继续重试只会让失败请求
    排队占满连接、拖垮整个网关；熔断器在冷却期内直接快速失败，把故障
    隔离在单点，等冷却结束再放一个探测请求试探上游是否恢复。
    """

    def __init__(self, fail_threshold: int, cooldown: float):
        self.fail_threshold = fail_threshold
        self.cooldown = cooldown
        self._state = "closed"          # closed | open | half-open
        self._consecutive_failures = 0
        self._opened_at = 0.0
        self._probing = False           # half-open 时是否已有探测请求在途

    def allow(self) -> bool:
        now = time.monotonic()
        if self._state == "closed":
            return True
        if self._state == "open":
            if now - self._opened_at >= self.cooldown:
                self._state = "half-open"
                self._probing = False
            else:
                return False
        # half-open：只放一个探测请求，其余继续快速失败
        if self._probing:
            return False
        self._probing = True
        return True

    def record_success(self):
        self._consecutive_failures = 0
        self._state = "closed"
        self._probing = False

    def record_failure(self):
        self._consecutive_failures += 1
        self._probing = False
        if self._state == "half-open":
            # 探测失败 -> 立即重新 open
            self._state = "open"
            self._opened_at = time.monotonic()
        elif self._consecutive_failures >= self.fail_threshold:
            self._state = "open"
            self._opened_at = time.monotonic()

    @property
    def state(self) -> str:
        return self._state

    def stats(self) -> dict:
        return {
            "state": self._state,
            "consecutive_failures": self._consecutive_failures,
        }


breaker = CircuitBreaker(config.CIRCUIT_FAIL_THRESHOLD, config.CIRCUIT_COOLDOWN)


async def _sleep(sec: float):
    await asyncio.sleep(sec)


# ---------- Mock 模式（验证链路用，数字不进简历） ----------

async def mock_chat(messages, temperature, max_tokens) -> str:
    await _sleep(0.05)  # 模拟一点网络延迟
    return ("这是一个 MOCK 模式的回复（未调用真实模型）。"
            "配置 SILICONFLOW_API_KEY 并设置 MOCK_MODE=false 即可切换为真实调用。")


async def mock_chat_stream(text: str = None) -> AsyncGenerator[str, None]:
    text = text or "这是 MOCK 模式的流式回复，用于验证 SSE 链路是否通畅。"
    for i in range(0, len(text), 8):
        await _sleep(0.02)
        yield text[i : i + 8]


# ---------- 重试 / 熔断辅助 ----------

def _should_retry(status_code: int | None) -> bool:
    """只有 5xx（服务端问题）值得重试，4xx（客户端问题）重试也没用。"""
    return status_code is not None and status_code >= 500


async def _wait_before_retry(attempt: int):
    """指数退避：0.5s -> 1s -> 2s ..."""
    await _sleep(config.LLM_RETRY_BACKOFF * (2 ** attempt))


# ---------- 真实调用 ----------

async def chat_completion(messages: list, temperature: float = 0.7,
                           max_tokens: int = 1024) -> str:
    """非流式：一次请求返回完整回复文本（带超时 + 重试 + 熔断）。"""
    if config.MOCK_MODE or not config.API_KEY:
        return await mock_chat(messages, temperature, max_tokens)

    payload = {
        "model": config.MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    for attempt in range(config.LLM_MAX_RETRIES + 1):
        if not breaker.allow():
            raise UpstreamUnavailableError("circuit breaker open")

        try:
            resp = await get_client().post(
                f"{config.BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {config.API_KEY}"},
                json=payload,
            )
        except (httpx.TimeoutException, httpx.TransportError):
            # 超时 / 网络层错误：记录失败，重试
            breaker.record_failure()
            if attempt < config.LLM_MAX_RETRIES:
                await _wait_before_retry(attempt)
                continue
            raise

        if resp.status_code < 400:
            breaker.record_success()
            return resp.json()["choices"][0]["message"]["content"]

        if _should_retry(resp.status_code):
            breaker.record_failure()
            if attempt < config.LLM_MAX_RETRIES:
                await _wait_before_retry(attempt)
                continue
        resp.raise_for_status()  # 4xx 或重试耗尽后的 5xx


async def chat_completion_stream(messages: list, temperature: float = 0.7,
                                 max_tokens: int = 1024) -> AsyncGenerator[str, None]:
    """流式：逐块 yield 增量文本（SSE 透传），响应头阶段带重试 + 熔断。"""
    if config.MOCK_MODE or not config.API_KEY:
        async for chunk in mock_chat_stream():
            yield chunk
        return

    payload = {
        "model": config.MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": True,
    }

    for attempt in range(config.LLM_MAX_RETRIES + 1):
        if not breaker.allow():
            raise UpstreamUnavailableError("circuit breaker open")

        try:
            async with get_client().stream(
                "POST",
                f"{config.BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {config.API_KEY}"},
                json=payload,
            ) as resp:
                if resp.status_code >= 400:
                    # 还没开始读流体，可安全重试（仅 5xx）
                    if _should_retry(resp.status_code):
                        breaker.record_failure()
                        if attempt < config.LLM_MAX_RETRIES:
                            await _wait_before_retry(attempt)
                            continue
                    resp.raise_for_status()
                breaker.record_success()
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    payload_str = line[len("data: "):].strip()
                    if payload_str == "[DONE]":
                        break
                    try:
                        data = json.loads(payload_str)
                    except json.JSONDecodeError:
                        continue
                    choices = data.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    content = delta.get("content")
                    if content:
                        yield content
                return
        except (httpx.TimeoutException, httpx.TransportError):
            breaker.record_failure()
            if attempt < config.LLM_MAX_RETRIES:
                await _wait_before_retry(attempt)
                continue
            raise
