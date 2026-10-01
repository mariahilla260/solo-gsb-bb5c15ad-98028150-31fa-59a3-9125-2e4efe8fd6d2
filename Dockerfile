FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8000

# 应用启动时自动幂等初始化数据库（扩展/表/约束/索引）
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
