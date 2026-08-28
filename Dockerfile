# Day 7-8 产出：Docker 化部署（FDE JD 高频要求）
# 国内环境：镜像走 daocloud 完整路径绕开 auth.docker.io；pip 走清华源
# 踩坑：BuildKit 的 HEAD 请求对 daocloud 会 401，需先 docker pull 到本地再 build
FROM docker.m.daocloud.io/library/python:3.11-slim

WORKDIR /app

# 先拷依赖清单单独一层，利用 Docker 层缓存：改代码不用重装依赖
COPY requirements.txt .
# 清华源封 docker 容器 IP(403)，阿里云/中科大/豆瓣实测 200，用阿里云
RUN pip install --no-cache-dir -i https://mirrors.aliyun.com/pypi/simple/ -r requirements.txt

COPY . .

EXPOSE 8000

# 生产建议多 worker；压测对比时可改 --workers 数
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
