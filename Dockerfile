FROM python:3.11-slim

WORKDIR /app

# Dépendances système minimales
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Dépendances Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Code source
COPY . .

# Dossier persistant pour les données SQLite et médias
RUN mkdir -p /app/data/media

EXPOSE 8000

ENV PORT=8000
ENV PYTHONUNBUFFERED=1

CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT}"]
