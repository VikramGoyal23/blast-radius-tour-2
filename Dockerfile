# The scorer builds this on linux/amd64 and runs it with no network.
#
# map/      per-line test dependencies, traced once at the pinned commit
#           (make map) and committed.
# src/      sqlglot at the pinned commit, with the packages its tests import
#           at the versions the commits were labeled with. The selector uses
#           it to run a few cheap tests against a patch before it pays for a
#           slow one (see probe() in selector.py).
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
ADD https://github.com/tobymao/sqlglot/archive/eb11aec4be8cb2122afe713a4816662c3f0fb2fa.tar.gz /tmp/sqlglot.tar.gz
RUN mkdir -p /app/src && tar -xzf /tmp/sqlglot.tar.gz -C /app/src --strip-components=1 \
    && rm -rf /tmp/sqlglot.tar.gz /app/src/posts /app/src/benchmarks /app/src/pdoc
COPY requirements.lock /tmp/requirements.lock
RUN grep -E '^(duckdb|numpy|pandas|python-dateutil|pytz|tzdata|mypy_extensions|typing_extensions)==' /tmp/requirements.lock > /tmp/probe.txt \
    && pip install --no-cache-dir -r /tmp/probe.txt \
    && python -m compileall -q /app/src/sqlglot /app/src/tests
COPY map /app/map
COPY map_q /app/map_q
COPY probe /app/probe
COPY selector.py /app/selector.py
CMD ["python", "-S", "-B", "/app/selector.py"]
