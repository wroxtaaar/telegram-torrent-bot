FROM python:3.13-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY bot ./bot
COPY web ./web

EXPOSE 8080

CMD ["sh", "-c", "python -m web.server & python -m bot.bot"]
