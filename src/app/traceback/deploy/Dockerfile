# 溯源分析系统 Dockerfile (P4 生产镜像)
# 多阶段构建: 依赖层缓存 + 精简运行镜像
FROM python:3.12-slim AS base

# weasyprint PDF 需要的系统库 (Pango/Cairo/GDK); 不需 PDF 可去掉以缩小镜像
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpango-1.0-0 libpangoft2-1.0-0 libcairo2 libgdk-pixbuf2.0-0 \
    shared-mime-info \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 依赖 (利用层缓存)
COPY requirements-traceback.txt .
RUN pip install --no-cache-dir -r requirements-traceback.txt

# 应用代码
COPY src/app/traceback ./src/app/traceback

ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

EXPOSE 8000

# 健康检查
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://localhost:8000/health'); sys.exit(0)" || exit 1

# 默认启动 API (生产用 gunicorn + uvicorn worker; 单机 dev 可直接 uvicorn)
CMD ["python", "-m", "uvicorn", "src.app.traceback.main:app", \
     "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]
