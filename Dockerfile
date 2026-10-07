FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY plex_epg_xmltv.py /app/plex_epg_xmltv.py
COPY server.py /app/server.py
COPY setup_lineup.py /app/setup_lineup.py
COPY static /app/static
COPY entrypoint.sh /app/entrypoint.sh

RUN chmod +x /app/entrypoint.sh \
    && mkdir -p /config /output

VOLUME ["/config", "/output"]

EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
  CMD python /app/server.py --healthcheck || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
