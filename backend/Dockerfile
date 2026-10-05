# syntax=docker/dockerfile:1.7
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

FROM base AS build
COPY pyproject.toml ./
COPY config ./config
COPY apps ./apps
RUN pip install --prefix=/install .

FROM base AS runtime
RUN groupadd --system --gid 10001 reviewbot \
 && useradd --system --uid 10001 --gid reviewbot --home-dir /app --shell /usr/sbin/nologin reviewbot
COPY --from=build /install /usr/local
COPY manage.py ./
COPY config ./config
COPY apps ./apps
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh
USER reviewbot
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)" || exit 1
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["api"]
