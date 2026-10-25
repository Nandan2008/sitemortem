# Playwright's image ships Chromium's system libraries. `playwright install` below fetches the browser build
# that matches whatever Playwright version requirements.txt resolves to (they must match, or Chromium won't start).
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && playwright install --with-deps chromium
COPY . .
ENV PORT=8000 \
    SITEMEDIC_DATA_DIR=/app/data \
    SITEMEDIC_LIVE_RUNS_PER_HOUR=5
RUN mkdir -p /app/data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/api/health')"
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips=*"]
