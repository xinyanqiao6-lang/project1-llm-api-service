"""项目1 主服务：AI 推理 API 网关（OpenAI 兼容）。

接口一览：
- POST /v1/chat/completions  对话接口，支持 stream=true（SSE 流式）
- GET  /health               健康检查
- GET  /stats                运行指标：缓存命中 / 限流拒绝 / 模式
- GET  /v1/models            模型列表（OpenAI 兼容）

链路：客户端 -> 限流 -> 缓存查询 -> (未命中) 调硅基流动 -> 写缓存 -> 返回
"""
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import config, llm_client
from .cache import cache
from .observability import RequestTrace, compute_cost, route_model, store
from .ratelimit import limiter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("gateway")


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await llm_client.close_client()  # 优雅关闭：释放 httpx 连接池


app = FastAPI(
    title="LLM API Gateway",
    description="硅基流动 API 封装：OpenAI 兼容接口 + Redis 缓存 + 滑动窗口限流",
    version="1.0.0",
    lifespan=lifespan,
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
    """运行指标总览。

    v2 把 trace 聚合并进来——v1 只有累计计数，无法回答两个关键问题：
    「单个请求到底发生了什么」和「这个月花了多少钱」。
    """
    return {
        "cache": cache.stats(),
        "rate_limit": limiter.stats(),
        "circuit_breaker": llm_client.breaker.stats(),
        "trace": store.aggregate(),
    }


@app.get("/traces/recent")
async def traces_recent(n: int = 20):
    """最近 n 条请求 trace——排查慢请求、定位问题用。"""
    return {"traces": store.recent(n)}


@app.get("/v1/models")
async def list_models():
    """OpenAI 兼容的模型列表接口，方便标准客户端 / 前端下拉直接接入。"""
    return {
        "object": "list",
        "data": [
            {
                "id": config.MODEL,
                "object": "model",
                "created": int(time.time()),
                "owned_by": "siliconflow",
            }
        ],
    }


@app.post("/v1/chat/completions")
async def chat_completions(body: ChatRequest, request: Request):
    t_start = time.perf_counter()
    client = client_id_of(request)

    # 0) trace 骨架：无论成功、缓存命中还是失败都会落一条，便于事后定位
    tr = RequestTrace(request_id=f"chatcmpl-{uuid.uuid4().hex[:12]}", client=client,
                      stream=body.stream)

    # 1) 限流（滑动窗口）
    allowed, current = limiter.allow(client)
    if not allowed:
        logger.warning("限流拒绝：client=%s current=%d", client, current)
        tr.status = 429
        tr.breaker_state = llm_client.breaker.state
        tr.t_total_ms = (time.perf_counter() - t_start) * 1000
        store.write(tr)
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

    # 1.5) 模型分级路由：客户端没指定模型时，按请求复杂度自动选模型。
    # 可解释优先——路由依据会写进 trace，出问题能倒查"为什么走了这个模型"。
    if config.ENABLE_MODEL_ROUTING:
        model, route_reason = route_model(body.model, body.max_tokens, body.messages)
        tr.routed_from = body.model or ""
    else:
        model, route_reason = (body.model or config.MODEL), "routing-disabled"
    tr.model = model

    messages = [m.model_dump() for m in body.messages]
    req_id = tr.request_id

    # 2) 缓存查询（流式/非流式共用）
    cache_key = cache.make_key(model, messages, body.temperature)
    cached = cache.get(cache_key)
    tr.cache_hit = cached is not None
    tr.cache_backend = cache.stats()["backend"]
    if cached is not None:
        if body.stream:
            # 命中缓存也走流式返回：把整段文本切块吐出去，保持接口行为一致
            async def replay():
                for i in range(0, len(cached), 16):
                    yield sse_chunk(req_id, model, cached[i: i + 16])
                yield sse_chunk(req_id, model, finish_reason="stop")
                yield "data: [DONE]\n\n"

            tr.status = 200
            tr.t_total_ms = (time.perf_counter() - t_start) * 1000
            store.write(tr)
            return StreamingResponse(replay(), media_type="text/event-stream")

        tr.status = 200
        tr.t_total_ms = (time.perf_counter() - t_start) * 1000
        tr.breaker_state = llm_client.breaker.state
        store.write(tr)
        return _full_response(req_id, model, cached)

    # 3) 未命中 -> 调上游（流式）
    if body.stream:
        t_llm = time.perf_counter()

        async def stream_and_cache():
            parts: list[str] = []
            try:
                async for chunk in llm_client.chat_completion_stream(
                    messages, body.temperature, body.max_tokens
                ):
                    parts.append(chunk)
                    yield sse_chunk(req_id, model, chunk)
            except Exception as e:              # noqa: BLE001
                # 流已开始无法改状态码，但必须把失败落 trace，否则这次调用就消失了
                tr.status = 502
                tr.error = f"{type(e).__name__}: {e}"
                tr.t_llm_ms = (time.perf_counter() - t_llm) * 1000
                tr.t_total_ms = (time.perf_counter() - t_start) * 1000
                tr.breaker_state = llm_client.breaker.state
                store.write(tr)
                raise
            full = "".join(parts)
            if full:
                cache.set(cache_key, full)  # 流结束后写缓存
            yield sse_chunk(req_id, model, finish_reason="stop")
            yield "data: [DONE]\n\n"
            tr.status = 200
            tr.t_llm_ms = (time.perf_counter() - t_llm) * 1000
            tr.t_total_ms = (time.perf_counter() - t_start) * 1000
            tr.breaker_state = llm_client.breaker.state
            tr.completion_tokens = len(full) // 3   # 粗估：流式拿不到 usage
            tr.total_tokens = tr.completion_tokens
            tr.cost_cny, tr.cost_estimated = compute_cost(
                model, tr.prompt_tokens, tr.completion_tokens)
            store.write(tr)

        return StreamingResponse(stream_and_cache(), media_type="text/event-stream")

    # 3) 未命中 -> 调上游（带超时/重试/熔断，失败返回 503 而不是 500 崩溃）
    t_llm = time.perf_counter()
    try:
        content, usage, retries = await llm_client.chat_completion_with_usage(
            messages, body.temperature, body.max_tokens
        )
    except llm_client.UpstreamUnavailableError:
        logger.error("上游熔断中，返回 503")
        tr.status = 503
        tr.error = "circuit breaker open"
        tr.t_llm_ms = (time.perf_counter() - t_llm) * 1000
        tr.t_total_ms = (time.perf_counter() - t_start) * 1000
        tr.breaker_state = llm_client.breaker.state
        store.write(tr)
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
        logger.error("上游超时（重试耗尽），返回 503")
        tr.status = 503
        tr.error = "upstream timeout after retries"
        tr.t_llm_ms = (time.perf_counter() - t_llm) * 1000
        tr.t_total_ms = (time.perf_counter() - t_start) * 1000
        tr.breaker_state = llm_client.breaker.state
        store.write(tr)
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "message": "上游服务超时，请稍后重试",
                    "type": "upstream_timeout",
                }
            },
        )

    # 4) 成功：记账（token / 成本 / 分段耗时 / 重试次数）后落 trace
    cache.set(cache_key, content)
    tr.status = 200
    tr.retries = retries
    tr.prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
    tr.completion_tokens = int(usage.get("completion_tokens", 0) or 0)
    tr.total_tokens = int(usage.get("total_tokens", 0) or 0)
    tr.cost_cny, tr.cost_estimated = compute_cost(
        model, tr.prompt_tokens, tr.completion_tokens)
    tr.t_llm_ms = (time.perf_counter() - t_llm) * 1000
    tr.t_total_ms = (time.perf_counter() - t_start) * 1000
    tr.breaker_state = llm_client.breaker.state
    store.write(tr)

    resp = _full_response(req_id, model, content)
    # 把 usage 与路由信息带回给客户端（OpenAI 兼容字段），方便客户端侧核算
    resp["usage"] = {
        "prompt_tokens": tr.prompt_tokens,
        "completion_tokens": tr.completion_tokens,
        "total_tokens": tr.total_tokens,
    }
    resp["x_gateway"] = {
        "routed_by": route_reason,
        "cost_cny": tr.cost_cny,
        "cost_estimated": tr.cost_estimated,
        "retries": retries,
        "cache_hit": False,
        "trace_id": req_id,
    }
    return resp


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
