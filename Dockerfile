# The scorer builds this on linux/amd64 and runs it with no network. Nothing
# is downloaded: the map was built once from sqlglot at the pinned commit
# (make map) and is committed under map/.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
COPY map /app/map
COPY selector.py /app/selector.py
CMD ["python", "-S", "-B", "/app/selector.py"]
