"""Day 1-2 产出：最基础的 API 调用脚本——先证明能调通硅基流动，再谈封装。

用法：
    python scripts/basic_call.py
前提：已把 .env.example 复制为 .env 并填入 SILICONFLOW_API_KEY
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("SILICONFLOW_API_KEY", "")
BASE_URL = os.getenv("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1")
MODEL = os.getenv("MODEL", "Qwen/Qwen2.5-7B-Instruct")


async def main():
    if not API_KEY:
        print("[!] 未配置 SILICONFLOW_API_KEY：请把 .env.example 复制为 .env 并填入 Key")
        print("    注册地址: https://cloud.siliconflow.cn")
        return

    print(f"[*] 模型: {MODEL}")
    print("[*] 正在请求:", f"{BASE_URL}/chat/completions")

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={
                "model": MODEL,
                "messages": [
                    {"role": "system", "content": "你是一个简洁的助手。"},
                    {"role": "user", "content": "用一句话解释什么是 API 网关。"},
                ],
                "temperature": 0.7,
                "max_tokens": 100,
            },
        )
        print(f"[*] HTTP 状态码: {resp.status_code}")
        if resp.status_code != 200:
            print(resp.text)
            return
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})
        print("[*] 模型回复:", content)
        print("[*] Token 用量:", usage)


if __name__ == "__main__":
    asyncio.run(main())
