FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
RUN useradd --create-home qydyr && chown -R qydyr /app
USER qydyr

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.getenv('PORT', '8000'))"
# schema is idempotent: safe on every start. seed fills a new database (places, 2ГИС from seed/, events,
# the city audit) in the background and does nothing on one that has it all; the server starts right away.
CMD ["sh", "-c", "python -m qydyr_data init-db && { python -m qydyr_data seed & } && exec python -m qydyr_data serve --port ${PORT:-8000}"]
