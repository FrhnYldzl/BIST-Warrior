FROM python:3.12-slim

WORKDIR /app

# Sistem bagimliliklari
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Python bagimliliklari
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Uygulama dosyalari — tum proje
COPY server/ ./server/

# PYTHONPATH ayarla — server/ icindeki moduller birbirini bulsun
ENV PYTHONPATH=/app/server

# Railway PORT env var inject eder, varsayilan 8000
ENV PORT=8000
EXPOSE 8000

# Calisma dizini server/ olsun
WORKDIR /app/server

# Baslat
CMD sh -c "exec uvicorn main:app --host 0.0.0.0 --port $PORT"
