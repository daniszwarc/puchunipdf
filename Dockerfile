FROM python:3.12-slim
# English is bundled in tesseract-ocr. OMP_THREAD_LIMIT=1 keeps each OCR process single-threaded (shared VPS).
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-spa tesseract-ocr-fra \
    && rm -rf /var/lib/apt/lists/*
ENV OMP_THREAD_LIMIT=1
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ .
RUN useradd -r app
USER app
EXPOSE 8000
# Trust X-Forwarded-* only from docker networks (Traefik); private ranges 172.16.0.0/12.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "172.16.0.0/12"]
