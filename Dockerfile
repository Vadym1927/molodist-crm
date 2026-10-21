FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=Europe/Kyiv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY tests ./tests
EXPOSE 8000
# Один процес: усередині працюють планувальник (синк, бекапи, нагадування) і Telegram-бот —
# кілька воркерів продублюють їх. --proxy-headers: реальні IP клієнтів з-за Caddy (для захисту входу).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", \
     "--proxy-headers", "--forwarded-allow-ips", "*", "--timeout-keep-alive", "30"]
