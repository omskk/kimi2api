FROM python:3.12-slim


# 先复制 requirements.txt 以利用 Docker 层缓存
COPY requirements.txt .

# 安装依赖并清理缓存
RUN pip install --no-cache-dir -r requirements.txt

WORKDIR /app

# 复制应用代码
COPY openai.py .

COPY app/ ./app/

# 暴露端口
EXPOSE 8001

# 启动应用
CMD ["python", "openai.py"]
