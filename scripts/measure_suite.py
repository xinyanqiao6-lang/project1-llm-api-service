"""测量套件：一条命令跑完所有场景，并生成可核对的指标台账。

为什么要有这个脚本：
v1 的压测是"手动起服务 + 手动改 RATE_LIMIT + 手动跑 + 手动抄数字"，
口径散落在人脑里，于是出现三个硬伤（QPS 含被拒请求、命中率用累计值、
README 数字与结果文件对不上）。本脚本把场景、口径、结果、台账固化，
任何人重跑都得到同样格式的结果，数字与文件永远对得上。

用法：
    python scripts/measure_suite.py                 # 跑全部场景
    python scripts/measure_suite.py --only baseline # 只跑基线
    python scripts/measure_suite.py --list          # 列出场景

⚠️ Redis 未启动时，缓存相关指标会被自动标记为 invalid（后端为 in-memory fallback）。
   要拿到可写进简历的缓存数字，先起 Redis：
       docker compose up -d redis
"""
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "results"
HOST = "127.0.0.1"
PORT_BASE = 8100          # 每个场景用独立端口，彻底避免"上一轮服务没死透"串味
BASE_URL = f"http://{HOST}:{PORT_BASE}"

# 场景定义：env 覆盖 + 压测参数
# RATE_LIMIT=100000 = 关闭限流干扰，专测吞吐/延迟；
# RATE_LIMIT=10      = 故意打到阈值之上，专测限流拒绝行为。
#
# ⚠️ 关于 --rps（开环发包）：硅基流动免费档 Qwen2.5-7B 实测只能扛约 5 QPS
#    的真实推理。之前不限速的闭环压测 20 秒打出 2.8 万请求、成功仅 10 个，
#    那是压测器把上游打崩后的反馈崩塌，数字无意义。
#
# ⚠️ expect：每个场景声明"服务端必须报告的配置"。启动后会核对 /stats，
#    对不上就直接判失败——防止测到"上一轮残留的旧进程"而毫无察觉。
SCENARIOS = {
    "baseline": {
        "desc": "基线：真实模型、关闭限流干扰、30% 热点、5 RPS 贴近上游容量",
        "env": {"RATE_LIMIT": "100000"},
        "expect": {"limit": 100000},
        "args": ["--concurrency", "3", "--duration", "45", "--repeat", "0.3",
                 "--rps", "5", "--ramp", "10", "--mode", "baseline"],
    },
    "cache-cold": {
        "desc": "冷路径：全部未命中（repeat=0），3 RPS 测纯推理延迟",
        "env": {"RATE_LIMIT": "100000"},
        "expect": {"limit": 100000},
        "args": ["--concurrency", "3", "--duration", "45", "--repeat", "0.0",
                 "--rps", "3", "--ramp", "10", "--mode", "cache"],
    },
    "cache-warm": {
        "desc": "热路径：90% 命中缓存，50 RPS 测缓存命中路径延迟",
        "env": {"RATE_LIMIT": "100000"},
        "expect": {"limit": 100000},
        "args": ["--concurrency", "10", "--duration", "30", "--repeat", "0.9",
                 "--rps", "50", "--ramp", "5", "--mode", "cache"],
    },
    "ratelimit": {
        "desc": "限流：RATE_LIMIT=10 + 30 RPS 打超阈值，测拒绝率（吞吐不作数）",
        "env": {"RATE_LIMIT": "10"},
        "expect": {"limit": 10},          # 关键校验：必须真的限流
        "args": ["--concurrency", "10", "--duration", "20", "--repeat", "0.3",
                 "--rps", "30", "--ramp", "3", "--mode", "ratelimit", "--expect-reject"],
    },
    "stream": {
        "desc": "流式：SSE 透传，5 RPS 测流式吞吐与延迟",
        "env": {"RATE_LIMIT": "100000"},
        "expect": {"limit": 100000},
        "args": ["--concurrency", "3", "--duration", "30", "--repeat", "0.3",
                 "--rps", "5", "--ramp", "8", "--mode", "stream", "--stream"],
    },
}


def _port_for(name: str) -> int:
    return PORT_BASE + list(SCENARIOS).index(name)


def start_server(env_overrides: dict, port: int) -> subprocess.Popen:
    env = os.environ.copy()
    env.update(env_overrides)
    env["MOCK_MODE"] = "false"          # 强制真实模型，杜绝 mock 数字混入
    env["PORT"] = str(port)
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", str(HOST), "--port", str(port), "--log-level", "warning"],
        cwd=ROOT, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    # 等健康检查通过
    import httpx
    base = f"http://{HOST}:{port}"
    for _ in range(40):
        time.sleep(0.5)
        if proc.poll() is not None:
            raise RuntimeError("服务进程意外退出")
        try:
            if httpx.get(f"{base}/health", timeout=2, trust_env=False).status_code == 200:
                return proc
        except Exception:
            continue
    proc.terminate()
    raise RuntimeError("服务 20s 内未就绪")


def stop_server(proc: subprocess.Popen, port: int):
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    # 等端口真正释放：Windows 上 uvicorn 优雅退出可能滞后，
    # 端口没释放就起下一个服务会绑不上，或者更糟——静默测到旧进程。
    import socket
    for _ in range(20):
        with socket.socket() as s:
            if s.connect_ex((HOST, port)) != 0:
                return
        time.sleep(0.5)


def verify_server(port: int, expect: dict) -> None:
    """核对服务端自报配置与场景期望是否一致。

    这是实测踩到的最隐蔽的一类错误：上一轮的服务进程没死透，新服务绑不上
    端口直接退出，而健康检查恰好打到了旧进程——于是"限流场景"其实跑在
    RATE_LIMIT=100000 的旧服务上，拒绝率 0%，看起来一切正常。
    """
    import httpx
    base = f"http://{HOST}:{port}"
    stats = httpx.get(f"{base}/stats", timeout=5, trust_env=False).json()
    rl = stats.get("rate_limit", {})
    if "limit" in expect and rl.get("limit") != expect["limit"]:
        raise RuntimeError(
            f"服务端 RATE_LIMIT={rl.get('limit')}，但本场景期望 {expect['limit']}；"
            f"很可能测到了残留的旧进程，本轮结果作废"
        )
    print(f"[*] 服务端自报：limit={rl.get('limit')} window={rl.get('window_seconds')}s "
          f"backend={rl.get('backend')} 缓存={(stats.get('cache') or {}).get('backend')}")


def run_scenario(name: str) -> str | None:
    sc = SCENARIOS[name]
    port = _port_for(name)
    base_url = f"http://{HOST}:{port}"
    print(f"\n{'='*70}\n>>> 场景：{name} —— {sc['desc']}\n{'='*70}")
    proc = start_server(sc["env"], port)
    try:
        verify_server(port, sc.get("expect", {}))
        before = set(RESULTS.glob("loadtest_*.json"))
        subprocess.run(
            [sys.executable, "scripts/load_test.py", "--url", base_url] + sc["args"],
            cwd=ROOT, check=True,
        )
        new = sorted(set(RESULTS.glob("loadtest_*.json")) - before)
        return new[-1].name if new else None
    finally:
        stop_server(proc, port)
        time.sleep(1)


def build_ledger(files: list[str]) -> str:
    rows = []
    for fn in files:
        p = RESULTS / fn
        if not p.exists():
            continue
        d = json.loads(p.read_text(encoding="utf-8"))

        # v1 = 旧口径文件：QPS 含被拒请求、延迟混入 429、命中率为进程累计值。
        # 这类文件只作历史留档，**一律不可引用**，此处重算一遍真实口径供对照。
        if d.get("schema") != "v2":
            total = d.get("total_requests") or 0
            ok = d.get("success") or 0
            dur = d.get("duration_s") or 0
            ss = (d.get("server_stats") or {})
            cache = ss.get("cache") or {}
            rl = ss.get("rate_limit") or {}
            rows.append({
                "file": fn, "legacy": True, "mode": "v1-legacy",
                "conc": d.get("concurrency"), "dur": dur,
                "backend": cache.get("backend"), "mock": None,
                "offered": total, "ok": ok, "rej": rl.get("rejected_total"),
                "rej_rate": (rl.get("rejected_total", 0) / total) if total else None,
                "sqps": round(ok / dur, 1) if dur else None,
                "oqps": round(total / dur, 1) if dur else None,
                "p50": (d.get("latency_ms") or {}).get("P50"),
                "p95": (d.get("latency_ms") or {}).get("P95"),
                "p99": (d.get("latency_ms") or {}).get("P99"),
                "n": ok, "hit": None, "hits": None, "misses": None,
                "valid": {"throughput": "invalid", "latency": "invalid",
                          "cache_hit_rate": "invalid"},
                "grade": "legacy",
                "reasons": {"legacy": [
                    "v1 旧口径文件：QPS 把被限流拒绝的请求算进了分子；"
                    "延迟分位数混入了 429 的毫秒级响应；缓存命中率是进程累计值而非本轮增量。"
                    "本次已重算真实成功吞吐供对照，但延迟与命中率不可再拆分为成功/拒绝两类，故整体不可引用。",
                ]},
            })
            continue

        c = d.get("counts", {})
        t = d.get("throughput", {})
        lat = d.get("latency_ms", {}).get("ok_only", {})
        ca = d.get("cache", {})
        env = d.get("env", {})
        rows.append({
            "file": fn,
            "legacy": False,
            "mode": d.get("mode"),
            "conc": d.get("config", {}).get("concurrency"),
            "dur": t.get("duration_actual_s"),
            "backend": env.get("cache_backend"),
            "mock": env.get("mock_mode"),
            "offered": c.get("offered"),
            "ok": c.get("ok"),
            "rej": c.get("rejected_429"),
            "rej_rate": c.get("rejected_rate"),
            "sqps": t.get("success_QPS"),
            "oqps": t.get("offered_QPS"),
            "p50": lat.get("P50"), "p95": lat.get("P95"), "p99": lat.get("P99"),
            "n": lat.get("count"),
            "hit": ca.get("hit_rate"),
            "hits": ca.get("hits_delta"), "misses": ca.get("misses_delta"),
            "valid": d.get("metric_validity"),
            "grade": "resume" if d.get("resume_grade") else "partial",
            "reasons": d.get("invalid_reasons", {}),
        })

    def mark(v):
        return "✅" if v == "valid" else "❌"

    out = [
        "# 项目1 压测指标台账（自动生成，勿手改）",
        "",
        f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "> 口径说明：",
        "> - **成功吞吐** = HTTP 200 数量 / 实际压测耗时（被 429 拒绝的不算吞吐）",
        "> - **发起吞吐** = 总请求数 / 实际耗时（含被拒请求，仅作参考）",
        "> - **延迟分位数只统计 200 响应**（429 仅几毫秒，混入会把 P50 拉到假象）",
        "> - **缓存命中率用压测前后 /stats 差值**（增量），不是进程累计值",
        "> - ✅/❌ 为可信度判定；只有全部 ✅ 的行才能写进简历",
        "",
        "## 总览",
        "",
        "| 场景 | 后端 | 并发 | 成功/发起 | 拒绝率 | 成功吞吐 | 发起吞吐 | P50 | P95 | P99 | 样本 | 命中率 | 吞吐 | 延迟 | 缓存 | 可写简历 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        v = r["valid"] or {}
        out.append(
            f"| {r['mode']} | {r['backend']} | {r['conc']} | {r['ok']}/{r['offered']} | "
            f"{_pct(r['rej_rate'])} | {_n(r['sqps'])} | {_n(r['oqps'])} | "
            f"{_n(r['p50'])} | {_n(r['p95'])} | {_n(r['p99'])} | {r['n']} | "
            f"{_pct(r['hit'])} | {mark(v.get('throughput'))} | {mark(v.get('latency'))} | "
            f"{mark(v.get('cache_hit_rate'))} | {'✅' if r['grade']=='resume' else '❌'} |"
        )

    out += ["", "## 不可写进简历的行及原因", ""]
    any_bad = False
    for r in rows:
        if r["grade"] == "resume":
            continue
        any_bad = True
        out.append(f"### `{r['file']}`（场景 {r['mode']}）")
        for k, rs in r["reasons"].items():
            for reason in rs:
                out.append(f"- **{k}**：{reason}")
        out.append("")
    if not any_bad:
        out.append("（无）全部行通过可信度判定。")

    out += [
        "",
        "## 引用规则",
        "",
        "1. 简历/面试只引用本台账中「可写简历 = ✅」的行；",
        "2. 引用时必须同时说明后端（redis / in-memory）与是否含被限流请求；",
        "3. 任何与本台账不一致的数字，一律以 `results/*.json` 原始文件为准；",
        "4. 重跑本脚本会覆盖台账，README 中的数字需同步更新。",
    ]
    text = "\n".join(out)
    (RESULTS / "METRICS.md").write_text(text, encoding="utf-8")
    return text


def _n(v):
    return "—" if v is None else str(v)


def _pct(v):
    return "—" if v is None else f"{v*100:.2f}%"


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=list(SCENARIOS), help="只跑单个场景")
    ap.add_argument("--list", action="store_true", help="列出场景")
    ap.add_argument("--ledger-only", action="store_true", help="不压测，只用现有结果重建台账")
    a = ap.parse_args()

    if a.list:
        for k, v in SCENARIOS.items():
            print(f"{k:12s} {v['desc']}")
        sys.exit(0)

    if a.ledger_only:
        files = sorted(p.name for p in RESULTS.glob("loadtest_*.json"))
        print(build_ledger(files))
        sys.exit(0)

    names = [a.only] if a.only else list(SCENARIOS)
    produced = []
    for n in names:
        try:
            fn = run_scenario(n)
            if fn:
                produced.append(fn)
        except Exception as e:
            print(f"[!] 场景 {n} 失败：{e}")

    # 把历史结果也纳入台账，保证口径一致
    all_files = sorted(p.name for p in RESULTS.glob("loadtest_*.json"))
    print("\n" + "=" * 70)
    print(build_ledger(all_files))
    print(f"\n[*] 台账已写入 {RESULTS / 'METRICS.md'}")
