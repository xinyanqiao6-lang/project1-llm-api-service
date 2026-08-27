"""Day 9-10 产出：asyncio 压测脚本——真实指标就从这里出。

用法（在本目录下）：
    python scripts/load_test.py --url http://127.0.0.1:8000 --concurrency 10 --duration 30

参数说明：
    --url          服务地址
    --concurrency  并发协程数（同时挂着的请求数）
    --duration     压测时长（秒）
    --stream       加上则测流式接口
    --repeat       相同 prompt 重复发送的比例（默认 0.3 = 30% 重复，用来拉开缓存命中率）

输出：QPS / 成功失败数 / P50 / P95 / P99 延迟，并写入 results/ 目录 JSON。
⚠️ 数字只有用真实 API（MOCK_MODE=false）压出来的才能写进简历！
"""
import argparse
import asyncio
import json
import random
import statistics
import time
from datetime import datetime
from pathlib import Path

import httpx

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


def percentile(sorted_lat: list[float], p: float) -> float:
    if not sorted_lat:
        return 0.0
    idx = min(int(len(sorted_lat) * p), len(sorted_lat) - 1)
    return sorted_lat[idx]


async def worker(client: httpx.AsyncClient, url: str, args, latencies: list,
                 counters: dict, stop_at: float):
    i = 0
    while time.time() < stop_at:
        # 按 repeat 概率发"热点问题"（模拟重复提问，拉开缓存命中率），
        # 其余请求附加递增编号保证唯一——用于测"缓存全不命中"的冷路径吞吐。
        if random.random() < args.repeat:
            content = PROMPTS[0]
        else:
            content = f"{PROMPTS[counters['seq'] % len(PROMPTS)]}（变体 #{counters['seq']}）"
        counters["seq"] += 1

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
                    ok = resp.status_code == 200
            else:
                resp = await client.post(f"{url}/v1/chat/completions", json=payload)
                ok = resp.status_code == 200
            lat = (time.perf_counter() - t0) * 1000  # ms
            latencies.append(lat)
            counters["ok" if ok else "fail"] += 1
        except Exception:
            counters["fail"] += 1
        i += 1


async def run(args):
    latencies: list[float] = []
    counters = {"ok": 0, "fail": 0, "seq": 0}
    stop_at = time.time() + args.duration

    async with httpx.AsyncClient(timeout=120) as client:
        # 先确认服务活着
        try:
            h = await client.get(f"{args.url}/health")
            print(f"[*] 目标服务: {args.url} | health={h.status_code} | {h.json().get('mock_mode', '?')}")
            if h.json().get("mock_mode"):
                print("[!] ⚠️ 服务处于 MOCK_MODE，本次压测数字只能验证链路，不能写进简历！")
        except Exception as e:
            print(f"[!] 服务不可达: {e}")
            return

        tasks = [asyncio.create_task(worker(client, args.url, args, latencies, counters, stop_at))
                 for _ in range(args.concurrency)]
        await asyncio.gather(*tasks)

    lat_sorted = sorted(latencies)
    duration = args.duration
    total = counters["ok"] + counters["fail"]
    summary = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "target": args.url,
        "concurrency": args.concurrency,
        "duration_s": duration,
        "stream": args.stream,
        "total_requests": total,
        "success": counters["ok"],
        "failed": counters["fail"],
        "QPS": round(total / duration, 1),
        "latency_ms": {
            "avg": round(statistics.mean(lat_sorted), 1) if lat_sorted else None,
            "P50": round(percentile(lat_sorted, 0.50), 1),
            "P95": round(percentile(lat_sorted, 0.95), 1),
            "P99": round(percentile(lat_sorted, 0.99), 1),
        },
    }

    print("\n========== 压测结果 ==========")
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    # 顺手拉一下 /stats，看缓存命中与限流情况
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            s = await c.get(f"{args.url}/stats")
            print("========== 服务端统计 ==========")
            print(json.dumps(s.json(), ensure_ascii=False, indent=2))
            summary["server_stats"] = s.json()
    except Exception:
        pass

    out = Path("results")
    out.mkdir(exist_ok=True)
    fname = out / f"loadtest_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    fname.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[*] 结果已写入 {fname}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--concurrency", type=int, default=10)
    ap.add_argument("--duration", type=int, default=30)
    ap.add_argument("--stream", action="store_true")
    ap.add_argument("--repeat", type=float, default=0.3, help="重复 prompt 比例 0~1")
    args = ap.parse_args()
    asyncio.run(run(args))
