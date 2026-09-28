FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY fashion_bot ./fashion_bot
COPY static ./static
COPY demo_plugin ./demo_plugin

EXPOSE 8000

CMD ["uvicorn", "fashion_bot.main:app", "--host", "0.0.0.0", "--port", "8000"]