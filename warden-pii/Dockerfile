FROM python:3.13-slim
WORKDIR /srv
RUN useradd --create-home --uid 10001 app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
USER app
EXPOSE 8000
HEALTHCHECK --start-period=60s --interval=15s --timeout=5s --retries=5 CMD python -c "import urllib.request,json; assert json.load(urllib.request.urlopen('http://localhost:8000/health'))['status']=='ok'"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
