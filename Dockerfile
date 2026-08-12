# pipeline-hygiene dashboard as a self-contained image for Azure App Service.
# Demo mode: the committed synthetic snapshots are ingested at BUILD time so the
# container serves realistic data on first request with no runtime ingest step
# and no external data source.
FROM python:3.12-slim

WORKDIR /app

# Runtime deps only need manylinux wheels (pandas/altair/streamlit/pyyaml) — no
# build toolchain required.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Bake the committed weekly snapshots into a SQLite store. Globbed (not a fixed
# filename list) so re-seeding with different dates can't silently break the
# build; sh expands the glob in sorted (chronological) order. A series — not a
# single snapshot — is what makes the Trajectory / Slippage / Flow tabs populate
# (tests/test_packaging.py asserts >=2 exist).
ENV PIPELINE_HYGIENE_DB=/app/data/pipeline.db
RUN set -e; for f in data/snapshots/opps_*.csv; do python -m src.ingest "$f"; done

# Quotas + owner team/region metadata drive coverage and the Teams tab; the
# series manifest carries them. Without this they render blank.
ENV PIPELINE_HYGIENE_QUOTAS=/app/data/delta_manifest.json

# App Service routes to the port named by the WEBSITES_PORT app setting; keep it
# in sync with the port Streamlit binds (see deploy/azure-deploy.ps1).
ENV PORT=8000
EXPOSE 8000

# --server.address overrides the committed .streamlit/config.toml loopback bind
# (CLI flags win over config.toml). Easy Auth, when enabled, fronts the app, so
# binding 0.0.0.0 inside the container does not expose it to the open internet.
CMD ["sh", "-c", "streamlit run app/dashboard.py --server.address=0.0.0.0 --server.port=${PORT} --server.headless=true --browser.gatherUsageStats=false"]
