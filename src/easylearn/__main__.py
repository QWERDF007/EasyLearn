from __future__ import annotations

import argparse
import threading
import webbrowser

import uvicorn

from easylearn.config import Settings
from easylearn.main import create_app


def main() -> None:
    parser = argparse.ArgumentParser(description="Start the EasyLearn local document workspace")
    parser.add_argument("--config", default=None, help="TOML configuration file")
    args = parser.parse_args()
    settings = Settings.load(args.config)
    if settings.app.open_browser:
        threading.Timer(
            1.0, webbrowser.open, args=(f"http://{settings.app.host}:{settings.app.port}",)
        ).start()
    uvicorn.run(
        create_app(settings),
        host=settings.app.host,
        port=settings.app.port,
        workers=1,
        reload=False,
        access_log=False,
    )


if __name__ == "__main__":
    main()
