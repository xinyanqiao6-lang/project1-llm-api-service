"""项目1 主服务：AI 推理 API 网关（OpenAI 兼容）。

接口一览：
- POST /v1/chat/completions  对话接口，支持 stream=true（SSE 流式）
- GET  /health               健康检查
- GET  /stats                运行指标：缓存命中 / 限流拒绝 / 模式

链路：客户端 -> 限流 -> 缓存查询 -> (未命中) 调硅基流动 -> 写缓存 -> 返回
"""
import json
import time
import uuid

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import config, llm_client
from .cache import cache
from .ratelimit import limiter

app = FastAPI(
    title="LLM API Gateway",
    description="硅基流动 API 封装：OpenAI 兼容接口 + Redis 缓存 + 滑动窗口限流",
    version="1.0.0",
)


# ---------- 请求模型（OpenAI 兼容） ----------
class Message(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    model: str | None = None
    messages: list[Message]
    temperature: float = Field(default=0.7, ge=0, le=2)
    max_tokens: int = Field(default=1024, ge=1, le=8192)
    stream: bool = False


# ---------- 工具 ----------
def sse_chunk(req_id: str, model: str, content: str | None = None,
              finish_reason: str | None = None) -> str:
    """构造 OpenAI 风格的 SSE 数据帧。"""
    delta = {"content": content} if content is not None else {}
    frame = {
        "id": req_id,
        "object": "chat.completion.chunk",
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    return f"data: {json.dumps(frame, ensure_ascii=False)}\n\n"


def client_id_of(request: Request) -> str:
    """限流主体：优先取网关/代理传来的真实 IP，取不到就用客户端地址。"""
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# ---------- 接口 ----------
@app.get("/health")
async def health():
    return {
        "status": "ok",
        "model": config.MODEL,
        "mock_mode": config.MOCK_MODE,
        "cache_backend": cache.stats()["backend"],
        "circuit": llm_client.breaker.stats(),
        "timestamp": time.time(),
    }


@app.get("/stats")
async def stats():
    """压测后看这里：缓存命中率、限流拒绝数、熔断器状态，抄进《项目指标跟踪表》。"""
    return {
        "cache": cache.stats(),
        "rate_limit": limiter.stats(),
        "circuit_breaker": llm_client.breaker.stats(),
    }


@app.post("/v1/chat/completions")
async def chat_completions(body: ChatRequest, request: Request):
    # 1) 限流（滑动窗口）
    allowed, current = limiter.allow(client_id_of(request))
    if not allowed:
        return JSONResponse(
            status_code=429,
            content={
                "error": {
                    "message": f"Rate limit exceeded: {limiter.limit} requests "
                               f"per {limiter.window}s (current={current})",
                    "type": "rate_limit_error",
                }
            },
        )

    model = body.model or config.MODEL
    messages = [m.model_dump() for m in body.messages]
    req_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"

    # 2) 缓存查询（流式/非流式共用）
    cache_key = cache.make_key(model, messages, body.temperature)
    cached = cache.get(cache_key)
    if cached is not None:
        if body.stream:
            # 命中缓存也走流式返回：把整段文本切块吐出去，保持接口行为一致
            async def replay():
                for i in range(0, len(cached), 16):
                    yield sse_chunk(req_id, model, cached[i : i + 16])
                yield sse_chunk(req_id, model, finish_reason="stop")
                yield "data: [DONE]\n\n"

            return StreamingResponse(replay(), media_type="text/event-stream")
        return _full_response(req_id, model, cached)

    # 3) 未命中 -> 调上游
    if body.stream:
        async def stream_and_cache():
            parts: list[str] = []
            async for chunk in llm_client.chat_completion_stream(
                messages, body.temperature, body.max_tokens
            ):
                parts.append(chunk)
                yield sse_chunk(req_id, model, chunk)
            full = "".join(parts)
            if full:
                cache.set(cache_key, full)  # 流结束后写缓存
            yield sse_chunk(req_id, model, finish_reason="stop")
            yield "data: [DONE]\n\n"

        return StreamingResponse(stream_and_cache(), media_type="text/event-stream")

    # 3) 未命中 -> 调上游（带超时/重试/熔断，失败返回 503 而不是 500 崩溃）
    try:
        content = await llm_client.chat_completion(
            messages, body.temperature, body.max_tokens
        )
    except llm_client.UpstreamUnavailableError:
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "message": "上游服务暂时不可用（熔断中），请稍后重试",
                    "type": "upstream_unavailable",
                }
            },
        )
    except httpx.TimeoutException:
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "message": "上游服务超时，请稍后重试",
                    "type": "upstream_timeout",
                }
            },
        )
    cache.set(cache_key, content)
    return _full_response(req_id, model, content)


def _full_response(req_id: str, model: str, content: str) -> dict:
    return {
        "id": req_id,
        "object": "chat.completion",
        "model": model,
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": content},
             "finish_reason": "stop"}
        ],
        "usage": None,  # 上游 usage 透传可作扩展点
    }


@app.on_event("shutdown")
async def shutdown():
    await llm_client.close_client()
