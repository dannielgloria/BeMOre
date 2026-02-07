FROM python:3.12-slim

WORKDIR /app

# System deps (wget para healthcheck interno)
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
  && rm -rf /var/lib/apt/lists/*

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

ENV PYTHONUNBUFFERED=1

CMD ["python", "-m", "app.main"]
