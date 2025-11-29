# Dockerfile for Koyeb
FROM python:3.11-slim

WORKDIR /app

# install deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# copy app
COPY . .

ENV PORT=8080

# uvicorn as entry
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
