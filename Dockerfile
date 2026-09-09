FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py README.md ./
COPY data/config.example.json data/config.example.json

ENV HOST=0.0.0.0 PORT=7861
EXPOSE 7861
VOLUME ["/app/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
    CMD python -c "import urllib.request,os;urllib.request.urlopen(f\"http://127.0.0.1:{os.environ.get('PORT','7861')}/healthz\",timeout=4)"

CMD ["python", "app.py"]
