"""压测脚本 v2 —— 指标口径可信化。

用法（在本目录下）：
    # 基线：正常负载、限流阈值放宽，测真实吞吐与延迟
    python scripts/load_test.py --url http://127.0.0.1:8000 \
        --concurrency 10 --duration 30 --mode baseline

    # 限流：故意打到阈值之上，测拒绝率与成功率（不用于报吞吐）
    python scripts/load_test.py --concurrency 20 --duration 30 --mode ratelimit

    # 缓存分布：指定重复比例，测缓存命中率（默认贴近真实的 30%）
    python scripts/load_test.py --concurrency 10 --duration 30 --mode cache --repeat 0.3

口径约定（v1 踩过的坑，v2 固化成代码）：
1. QPS 分子**只算成功请求**（HTTP 200）。被限流拒绝的 429 不是吞吐。
   另给 offered_QPS = 总发起 / 时长，两者分开报。
2. 延迟分位数**只统计成功请求**。429 响应只要几毫秒，混进去会把 P50 拉到 3ms 的假象。
3. 缓存命中率用 /stats **前后差值**（进程级累计值不能当本轮值）。
4. QPS 分母用**实际压测耗时**，不用配置的 --duration。
5. 每轮结果都带 metric_validity 判定，mock / 内存缓存 / 样本不足会被标 invalid。

输出：results/loadtest_<stamp>_<resume|partial|mock>.json
⚠️ 只有文件名带 resume 的结果，数字才能写进简历。
"""
import argparse
import asyncio
import json
import random
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.metrics import (  # noqa: E402
    cache_hit_rate,
    compute_qps,
    diff_counters,
    is_resume_grade,
    judge_run,
    looks_proxied,
    summarize_latencies,
)

# 语料池：贴近真实业务提问分布，而不是同一句话刷 1000 遍
PROMPTS = [
    "用一句话解释什么是 API 网关",
    "用一句话解释什么是 Redis",
    "用一句话解释什么是限流",
    "用一句话解释什么是 Docker",
    "用一句话解释什么是 SSE",
    "用一句话解释什么是负载均衡",
    "用一句话解释什么是缓存穿透",
    "用一句话解释什么是异步编程",
    "用一句话解释什么是哈希表",
    "用一句话解释什么是索引",
]


async def _fetch_stats(client: httpx.AsyncClient, url: str):
    try:
        r = await client.get(f"{url}/stats", timeout=10)
        return r.json(), str(r.request.url)
    except Exception:
        return None, ""


async def _fetch_health(client: httpx.AsyncClient, url: str):
    try:
        r = await client.get(f"{url}/health", timeout=10)
        return r.json(), str(r.request.url)
    except Exception:
        return None, ""


class Pacer:
    """开环发包调度器（避免压测器自己把被测系统打崩）。

    为什么需要它——这是实测踩到的第二个坑：
    第一版是"每个 worker 循环里能发多快发多快"的闭环压测。结果上游
    一旦开始报错（如免费模型限流），错误响应只要 5-10ms，worker 转眼
    就能打出 1400 req/s，把上游彻底压垮，20 秒内 28211 个请求只成功 10 个。
    这是**压测器的反馈崩塌**，测出来的"吞吐 946 QPS"毫无意义。

    改成开环：按目标 RPS 匀速发包（可线性爬坡），发多快由压测器说了算，
    而不是由被测系统的响应速度说了算。这样失败率反映的是系统真实表现。
    """

    def __init__(self, target_rps: float, ramp_s: float, start_at: float):
        self.target_rps = target_rps
        self.ramp_s = ramp_s
        self.start_at = start_at
        self.sent = 0

    async def wait_turn(self):
        if self.target_rps <= 0:          # 0 = 不限速（保留旧行为，但已不推荐）
            self.sent += 1
            return
        elapsed = time.time() - self.start_at
        # 爬坡：ramp 期间目标 RPS 从 20% 线性升到 100%，避免冷启动瞬间打满
        frac = min(1.0, elapsed / self.ramp_s) if self.ramp_s else 1.0
        rps_now = self.target_rps * (0.2 + 0.8 * frac)
        interval = 1.0 / max(rps_now, 0.001)
        target = self.start_at + self.sent * interval
        delay = target - time.time()
        if delay > 0:
            await asyncio.sleep(delay)
        self.sent += 1


async def worker(client: httpx.AsyncClient, url: str, args,
                 acc: dict, stop_at: float, pacer: Pacer):
    """单个压测协程。

    关键：延迟分别落在三个桶里，绝不混算。
      lat_ok   -> 成功（200），唯一参与延迟分位数统计的桶
      lat_429  -> 被本网关限流拒绝
      lat_err  -> 上游错误 / 熔断 503 / 网络异常
    """
    while time.time() < stop_at:
        await pacer.wait_turn()
        if time.time() >= stop_at:
            break

        if random.random() < args.repeat:
            content = PROMPTS[0]                      # 热点问题：命中缓存
        else:
            n = acc["seq"]
            content = f"{PROMPTS[n % len(PROMPTS)]}（变体 #{n}）"  # 冷路径
        acc["seq"] += 1

        payload = {
            "messages": [{"role": "user", "content": content}],
            "max_tokens": 64,
            "stream": args.stream,
        }
        t0 = time.perf_counter()
        try:
            if args.stream:
                async with client.stream("POST", f"{url}/v1/chat/completions",
                                         json=payload) as resp:
                    async for _ in resp.aiter_lines():
                        pass
                    code = resp.status_code
            else:
                resp = await client.post(f"{url}/v1/chat/completions", json=payload)
                code = resp.status_code
            lat = (time.perf_counter() - t0) * 1000
            if code == 200:
                acc["ok"] += 1
                acc["lat_ok"].append(lat)
            elif code == 429:
                acc["rejected"] += 1
                acc["lat_429"].append(lat)
            else:
                acc["err"] += 1
                acc["lat_err"].append(lat)
                acc["status_codes"][str(code)] = acc["status_codes"].get(str(code), 0) + 1
        except Exception as e:
            acc["err"] += 1
            acc["lat_err"].append((time.perf_counter() - t0) * 1000)
            acc["status_codes"]["exception"] = acc["status_codes"].get("exception", 0) + 1
            acc.setdefault("errors", []).append(type(e).__name__)
        acc["offered"] += 1


async def run(args):
    acc = {"ok": 0, "rejected": 0, "err": 0, "offered": 0, "seq": 0,
           "lat_ok": [], "lat_429": [], "lat_err": [],
           "status_codes": {}, "errors": []}

    # trust_env=False：本机压测绝不走系统代理。
    # 实测踩坑：这台机器设了 HTTP_PROXY=127.0.0.1:55012 但 NO_PROXY 不含 127.0.0.1，
    # httpx 默认 trust_env=True 会把打向本地网关的请求全发给代理，代理一律回 404，
    # 表现为"压测跑完几乎没有成功请求"，而代码看起来完全正常。
    async with httpx.AsyncClient(timeout=args.timeout, trust_env=False) as client:
        health, health_url = await _fetch_health(client, args.url)
        if health is None:
            print(f"[!] 服务不可达或 /health 无响应: {args.url}")
            print("[!] 先启动服务：python -m uvicorn app.main:app --host 127.0.0.1 --port 8000")
            return

        # 预检：确认请求没有被代理劫持，否则后面的数字全部作废
        if looks_proxied(health_url, args.url):
            print(f"[!] ✗ 请求被系统代理劫持（实际请求目标：{health_url}）")
            print("[!] 本压测脚本已设 trust_env=False；若仍被劫持，请检查 NO_PROXY 是否包含 127.0.0.1")
            return
        if health.get("mock_mode"):
            print("[!] ⚠️ MOCK_MODE=true：数字只能验证链路，禁止写进简历。")
        cache_backend = health.get("cache_backend", "unknown")
        redis_on = cache_backend == "redis"
        if not redis_on:
            print(f"[!] ⚠️ 缓存后端为 {cache_backend}（非 Redis）：命中率只能表述为"
                  f"「自研进程内缓存」，禁止写「Redis 缓存命中率」。")

        # 关键：压测前抓一次 /stats 基线，用于算本轮增量
        stats_before, _ = await _fetch_stats(client, args.url)
        t_start = time.perf_counter()

        # 时钟必须分清：stop_at 用墙钟（与 worker 里的 time.time() 同源），
        # 耗时统计用单调钟 perf_counter（不受系统时间调整影响）。
        # 两者混用会导致 while 循环一次都不执行。
        wall_start = time.time()
        stop_at = wall_start + args.duration
        pacer = Pacer(args.rps, args.ramp, wall_start)
        tasks = [asyncio.create_task(
            worker(client, args.url, args, acc, stop_at, pacer))
            for _ in range(args.concurrency)]
        await asyncio.gather(*tasks)
        actual_duration = time.perf_counter() - t_start  # 实际耗时，不写死 args.duration

        stats_after, _ = await _fetch_stats(client, args.url)

    # ---------- 指标计算 ----------
    mock_mode = bool(health.get("mock_mode"))
    lat_ok = summarize_latencies(acc["lat_ok"])
    delta = diff_counters(stats_before or {}, stats_after or {})
    hit_rate = cache_hit_rate(delta)

    judgement = judge_run(
        mock_mode=mock_mode,
        cache_backend=cache_backend,
        latency_samples=lat_ok["count"],
        rejected=acc["rejected"],
        redis_enabled=redis_on,
    )
    grade = ("mock" if mock_mode else ("resume" if is_resume_grade(judgement) else "partial"))

    summary = {
        # ---- 溯源信息：没有这些，数字就无法自证 ----
        "schema": "v2",
        "run_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "mode": args.mode,
        "config": {
            "url": args.url, "concurrency": args.concurrency,
            "duration_cfg_s": args.duration, "stream": args.stream,
            "repeat": args.repeat, "timeout_s": args.timeout,
            "target_rps": args.rps, "ramp_s": args.ramp,
        },
        "env": {
            "mock_mode": mock_mode, "cache_backend": cache_backend,
            "model": health.get("model"),
            "rate_limit": (stats_after or {}).get("rate_limit", {}),
        },
        # ---- 请求计数 ----
        "counts": {
            "offered": acc["offered"], "ok": acc["ok"],
            "rejected_429": acc["rejected"], "error": acc["err"],
            "rejected_rate": round(acc["rejected"] / acc["offered"], 4) if acc["offered"] else None,
            "error_rate": round(acc["err"] / acc["offered"], 4) if acc["offered"] else None,
            "status_codes": acc["status_codes"],
        },
        # ---- 吞吐：两个口径分开报 ----
        "throughput": {
            "duration_actual_s": round(actual_duration, 2),
            "success_QPS": compute_qps(acc["ok"], actual_duration),      # 只算成功
            "offered_QPS": compute_qps(acc["offered"], actual_duration),  # 总发起
            "note": "success_QPS 才是网关有效吞吐；offered_QPS 含被限流拒绝的请求",
        },
        # ---- 延迟：分桶，200 才进分位数 ----
        "latency_ms": {
            "ok_only": lat_ok,
            "rejected_429_only": summarize_latencies(acc["lat_429"]),
            "error_only": summarize_latencies(acc["lat_err"]),
            "note": "分位数只统计 HTTP 200；429 响应仅几毫秒，混入会严重拉低 P50",
        },
        # ---- 缓存：用增量，标口径 ----
        "cache": {
            "backend": cache_backend,
            "hits_delta": delta["hits"], "misses_delta": delta["misses"],
            "hit_rate": hit_rate,
            "note": "本轮增量（压测前后 /stats 差值），非进程累计值",
        },
        "ratelimit": {"rejected_delta": delta["rejected_total"]},
        "circuit_breaker": (stats_after or {}).get("circuit_breaker", {}),
        "errors": acc.get("errors", [])[:20],
        # ---- 判定 ----
        "metric_validity": judgement["metric_validity"],
        "invalid_reasons": judgement["reasons"],
        "resume_grade": grade == "resume",
    }

    print("\n========== 压测结果 ==========")
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    out = Path("results")
    out.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = out / f"loadtest_{stamp}_{grade}.json"
    fname.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[*] 结果已写入 {fname}")
    print(f"[*] 可信度标记：{grade}"
          + ("" if grade == "resume" else f"  ← 不可写进简历：{judgement['reasons']}"))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="LLM 网关压测（口径可信版）")
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--concurrency", type=int, default=10)
    ap.add_argument("--duration", type=int, default=30)
    ap.add_argument("--stream", action="store_true")
    ap.add_argument("--repeat", type=float, default=0.3,
                    help="热点问题比例 0~1，用于测缓存命中率（贴近真实取 0.3）")
    ap.add_argument("--mode", default="baseline",
                    choices=["baseline", "cache", "ratelimit", "stream"],
                    help="baseline=正常负载；cache=缓存分布；ratelimit=限流压测；stream=流式")
    ap.add_argument("--timeout", type=float, default=120)
    ap.add_argument("--rps", type=float, default=0.0,
                    help="目标发包速率（开环）。0=不限速，但不推荐：会触发压测器反馈崩塌")
    ap.add_argument("--ramp", type=float, default=5.0,
                    help="线性爬坡秒数：目标 RPS 从 20% 升到 100%，避免冷启动打满")
    ap.add_argument("--expect-reject", action="store_true",
                    help="ratelimit 模式下声明预期会有拒绝")
    args = ap.parse_args()
    asyncio.run(run(args))
