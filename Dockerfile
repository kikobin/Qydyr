FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
RUN useradd --create-home qydyr && chown -R qydyr /app
USER qydyr

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8000/health'); sys.exit(0)"
# schema is idempotent: safe on every start
CMD ["sh", "-c", "python -m qydyr_data init-db && python -m qydyr_data serve --port ${PORT:-8000}"]
