FROM python:3.12-slim

# no .pyc, unbuffered logs
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# install deps first for better layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# data (SQLite) and logs persist via a mounted volume
VOLUME ["/app/data"]

# default DB/log paths point at the volume; override in config.yaml if desired
CMD ["python", "main.py"]
