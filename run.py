"""Single entry point:  python run.py   then open http://127.0.0.1:8000

The backend starts the MCP server itself (child process, stdio) and serves the built React UI.
Deliberately no --reload: on Windows, uvicorn's reload mode switches to an event loop that cannot
start subprocesses, and the MCP connection would fail at startup (LLD §8.3).
"""
from __future__ import annotations

import logging

import uvicorn

from backend.config import get_settings


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    settings = get_settings()
    if not settings.erp_db_path.is_file():
        logging.warning("No data at %s. Run first:  python seed/generate.py", settings.erp_db_path)
    print(f"\n  Spend & Contract Leakage Analyzer   http://{settings.host}:{settings.port}"
          f"   (LLM_MODE={settings.llm_mode})\n")
    from backend.app import app
    uvicorn.run(app, host=settings.host, port=settings.port, reload=False, log_level="info")


if __name__ == "__main__":
    main()
