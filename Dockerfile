FROM mcr.microsoft.com/playwright/python:v1.59.0-noble

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    EFFECT_BROWSER_DATABASE_URL=sqlite:////data/effect-browser.db \
    EFFECT_BROWSER_BROWSER_HEADLESS=true \
    EFFECT_BROWSER_BROWSER_SANDBOX=false

WORKDIR /app
RUN groupadd --system app && useradd --system --gid app app \
    && mkdir /data /app/artifacts && chown -R app:app /data /app/artifacts

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir ".[postgres,mcp]"

USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')"

CMD ["uvicorn", "effect_browser.api:app", "--host", "0.0.0.0", "--port", "8000"]
