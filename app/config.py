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

# ---- 上游调用韧性（超时 / 重试 / 熔断）----
# 免费模型偶发 60s 挂起会把 P99 拖爆，这里用三层防线：
# 1) 超时：单次请求超时从 60s 收紧到 LLM_TIMEOUT；
# 2) 重试：对超时/连接错误/5xx 做有限次重试 + 指数退避；
# 3) 熔断：连续失败达阈值后打开熔断器，冷却期内快速失败，避免雪崩。
LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "30"))          # 单次请求总超时（秒）
LLM_CONNECT_TIMEOUT = float(os.getenv("LLM_CONNECT_TIMEOUT", "5"))  # 连接超时（秒）
LLM_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "2"))     # 最多重试次数
LLM_RETRY_BACKOFF = float(os.getenv("LLM_RETRY_BACKOFF", "0.5"))  # 退避基数（秒）
CIRCUIT_FAIL_THRESHOLD = int(os.getenv("CIRCUIT_FAIL_THRESHOLD", "5"))  # 连续失败 N 次触发熔断
CIRCUIT_COOLDOWN = float(os.getenv("CIRCUIT_COOLDOWN", "30"))  # 熔断冷却期（秒）

# ---- 运行模式 ----
# MOCK_MODE=true 时不调真实 API，返回固定文本——用于没有 Key 时先跑通全链路。
# ⚠️ Mock 模式压测出的数字只能验证链路，不能写进简历！
MOCK_MODE = os.getenv("MOCK_MODE", "false").lower() in ("1", "true", "yes")
PORT = int(os.getenv("PORT", "8000"))
