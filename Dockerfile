FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    DATA_DIR=/app/data \
    TZ=Europe/Kyiv

# opencv (через rapidocr) потребує цих системних бібліотек,
# інакше розпізнавання курсу з картинок мовчки не працює.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN mkdir -p /app/data

CMD ["python", "telegram_bot.py", "--with-rate-updater"]
