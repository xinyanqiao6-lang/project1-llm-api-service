# Day 7-8 产出：Docker 化部署（FDE JD 高频要求）
FROM python:3.11-slim

WORKDIR /app

# 先拷依赖清单单独一层，利用 Docker 层缓存：改代码不用重装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# 生产建议多 worker；压测对比时可改 --workers 数
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
