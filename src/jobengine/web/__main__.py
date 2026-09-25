"""python -m jobengine.web: the Cloud Run entry point (uvicorn on $PORT, default 8080)."""

from __future__ import annotations

import logging
import os

import uvicorn

from jobengine.main import banner
from jobengine.settings import get_settings
from jobengine.web.app import build_app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    s = get_settings()
    print(banner(s), flush=True)
    app = build_app(s)  # exits on an unsafe configuration
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")),
                log_level="info", proxy_headers=True)


if __name__ == "__main__":
    main()
