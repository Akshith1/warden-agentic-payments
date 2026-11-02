# The service that reads untrusted invoice images. Compose runs it with no route
# to the internet, a read-only root filesystem, no capabilities and a non-root
# user; app/jail.py then puts each Tesseract run under a Landlock ruleset.
FROM python:3.13-slim
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /srv
RUN useradd --create-home --uid 10001 app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
USER app
EXPOSE 8000
HEALTHCHECK --start-period=20s --interval=15s --timeout=5s --retries=5 CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
