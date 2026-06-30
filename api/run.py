#!/usr/bin/env python3
"""Local entrypoint: run the Central Monitoring API with uvicorn.

Honours the same env vars as the systemd unit:
  MONITOR_HOST (default 0.0.0.0), MONITOR_PORT (default 9099)

Production uses systemd + uvicorn directly (see setup-server.sh); this script
is a convenience for local runs:

  python run.py
"""

import uvicorn

from app.config import LISTEN_HOST, LISTEN_PORT

if __name__ == "__main__":
    uvicorn.run("app.main:app", host=LISTEN_HOST, port=LISTEN_PORT, log_level="info")
