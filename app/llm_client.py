"""LLM 客户端：封装硅基流动 API 的调用（非流式 + 流式 SSE）。

设计要点（面试会问）：
- 用 httpx.AsyncClient 异步调用，不阻塞事件循环，单进程也能撑住并发；
- 流式接口直接透传上游 SSE，逐块 yield 给上层；
- MOCK_MODE 下返回固定文本，用于无 Key 时先跑通链路。
"""
import json
import time
from typing import AsyncGenerator

import httpx

from . import config

_client: httpx.AsyncClient | None = None


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0))
    return _client


async def close_client():
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
        _client = None


# ---------- Mock 模式（验证链路用，数字不进简历） ----------

async def mock_chat(messages, temperature, max_tokens) -> str:
    await asyncio_sleep(0.05)  # 模拟一点网络延迟
    return ("这是一个 MOCK 模式的回复（未调用真实模型）。"
            "配置 SILICONFLOW_API_KEY 并设置 MOCK_MODE=false 即可切换为真实调用。")


async def mock_chat_stream(text: str = None) -> AsyncGenerator[str, None]:
    text = text or "这是 MOCK 模式的流式回复，用于验证 SSE 链路是否通畅。"
    for i in range(0, len(text), 8):
        await asyncio_sleep(0.02)
        yield text[i : i + 8]


async def asyncio_sleep(sec: float):
    import asyncio
    await asyncio.sleep(sec)


# ---------- 真实调用 ----------

async def chat_completion(messages: list, temperature: float = 0.7,
                           max_tokens: int = 1024) -> str:
    """非流式：一次请求返回完整回复文本。"""
    if config.MOCK_MODE or not config.API_KEY:
        return await mock_chat(messages, temperature, max_tokens)

    resp = await get_client().post(
        f"{config.BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {config.API_KEY}"},
        json={
            "model": config.MODEL,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        },
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


async def chat_completion_stream(messages: list, temperature: float = 0.7,
                                 max_tokens: int = 1024) -> AsyncGenerator[str, None]:
    """流式：逐块 yield 增量文本（SSE 透传）。"""
    if config.MOCK_MODE or not config.API_KEY:
        async for chunk in mock_chat_stream():
            yield chunk
        return

    async with get_client().stream(
        "POST",
        f"{config.BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {config.API_KEY}"},
        json={
            "model": config.MODEL,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        },
    ) as resp:
        resp.raise_for_status()
        async for line in resp.aiter_lines():
            if not line.startswith("data: "):
                continue
            payload = line[len("data: "):].strip()
            if payload == "[DONE]":
                break
            try:
                data = json.loads(payload)
            except json.JSONDecodeError:
                continue
            choices = data.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            content = delta.get("content")
            if content:
                yield content
