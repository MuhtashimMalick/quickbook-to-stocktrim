FROM python:3.12.16-slim-trixie AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.txt constraints.txt ./
RUN pip install -r requirements.txt -c constraints.txt

FROM base AS runtime
RUN useradd --system --uid 10001 --no-create-home app && mkdir /data && chown app /data
COPY alembic.ini ./
COPY alembic alembic
COPY app app
USER app
# TunnelGate reads the X-Forwarded-* headers itself; uvicorn only rewrites scope["client"] and keeps the headers.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips=*", "--timeout-keep-alive", "150", "--no-server-header"]

FROM base AS test
RUN useradd --system --uid 10001 --no-create-home app
COPY requirements-dev.txt ./
RUN pip install -r requirements-dev.txt -c constraints.txt
COPY . .
USER app
CMD ["pytest", "-q"]
