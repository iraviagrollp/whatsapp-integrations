"""Run the WhatsApp inbox service.

    .venv\\Scripts\\python.exe scripts\\serve.py

Listens on 127.0.0.1:8790 (``host`` / ``port`` in config.json): the Cloudflare
tunnel and a browser on this machine are the only things that reach it.
Open http://127.0.0.1:8790 here, or https://whatsapp.ialreports.com elsewhere.
"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# Run by pythonw.exe - the scheduled task, with no window - there is no console,
# and uvicorn's own logging would have nowhere to write.  logs/wapp.log is the
# record either way.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

import uvicorn  # noqa: E402

from wapp.app import create_app  # noqa: E402
from wapp.config import ConfigError, load_config  # noqa: E402


def _logging() -> None:
    folder = ROOT / "logs"
    folder.mkdir(exist_ok=True)
    handler = TimedRotatingFileHandler(folder / "wapp.log", when="midnight", backupCount=60, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s"))
    console = logging.StreamHandler()
    console.setFormatter(handler.formatter)
    for name in ("wapp", "uvicorn.error"):
        log = logging.getLogger(name)
        log.setLevel(logging.INFO)
        log.addHandler(handler)
    logging.getLogger("wapp").addHandler(console)


def main() -> None:
    _logging()
    try:
        config = load_config()
    except ConfigError as exc:
        logging.getLogger("wapp").error("%s", exc)
        sys.exit(f"\n{exc}\n")
    logging.getLogger("wapp").info(
        "Starting the WhatsApp inbox on http://%s:%s for phone number ID %s",
        config.host, config.port, config.phone_number_id,
    )
    uvicorn.run(create_app(config), host=config.host, port=config.port,
                log_level="info", access_log=False)


if __name__ == "__main__":
    main()
