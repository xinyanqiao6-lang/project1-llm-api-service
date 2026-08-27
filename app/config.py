"""统一配置：全部从环境变量 / .env 读取，不要把 API Key 写进代码。"""
import os
from dotenv import load_dotenv

load_dotenv()

# ---- 硅基流动（SiliconFlow）----
API_KEY = os.getenv("SILICONFLOW_API_KEY", "")
BASE_URL = os.getenv("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1")
MODEL = os.getenv("MODEL", "Qwen/Qwen2.5-7B-Instruct")

# ---- 缓存 ----
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
CACHE_TTL = int(os.getenv("CACHE_TTL", "3600"))  # 缓存有效期（秒）

# ---- 限流（滑动窗口）----
RATE_LIMIT = int(os.getenv("RATE_LIMIT", "10"))    # 窗口内最多 N 次
RATE_WINDOW = int(os.getenv("RATE_WINDOW", "60"))  # 窗口长度（秒）

# ---- 运行模式 ----
# MOCK_MODE=true 时不调真实 API，返回固定文本——用于没有 Key 时先跑通全链路。
# ⚠️ Mock 模式压测出的数字只能验证链路，不能写进简历！
MOCK_MODE = os.getenv("MOCK_MODE", "false").lower() in ("1", "true", "yes")
PORT = int(os.getenv("PORT", "8000"))
