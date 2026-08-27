# taskdeck-sandbox-python
#
# Python 3.12 with FastAPI/Flask/uvicorn pre-installed (so agents'
# common stacks start fast). Sandbox-host invokes
# /usr/local/bin/entrypoint.sh which honors $TD_INSTALL_CMD then
# execs $TD_START_CMD.
#
# Common framework auto-detection from sandbox_host.detection:
#   - main.py with FastAPI → uvicorn main:app :8000
#   - app.py with Flask    → flask run :8000
#
# The user's pip install runs against /workspace requirements.
FROM python:3.12-slim

# Tools needed for installing wheels with C extensions, plus git for
# pip installing from VCS.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        git \
    && rm -rf /var/lib/apt/lists/*

# Pre-install web stacks the auto-detect path defaults to.
RUN pip install --no-cache-dir \
        fastapi==0.115.* \
        uvicorn[standard]==0.32.* \
        flask==3.0.* \
        gunicorn==23.0.*

WORKDIR /workspace

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
