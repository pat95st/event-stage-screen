"""Command line entry point.

    python -m spike serve  [--host 127.0.0.1] [--port 8090]
    python -m spike report [--metrics data/metrics.jsonl] [--out reports/<host>.md]
    python -m spike sources
"""

from __future__ import annotations

import argparse
import logging
import socket
import time
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(prog="spike")
    sub = parser.add_subparsers(dest="cmd", required=True)
    serve = sub.add_parser("serve", help="run the spike server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8090)
    report = sub.add_parser("report", help="write the Markdown report")
    report.add_argument("--metrics", type=Path, default=Path("data/metrics.jsonl"))
    report.add_argument("--out", type=Path)
    sub.add_parser("sources", help="list NDI sources on the network")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.cmd == "serve":
        import uvicorn

        from .server import create_app

        uvicorn.run(create_app(), host=args.host, port=args.port, log_level="warning")
    elif args.cmd == "report":
        from .report import write_report

        out = args.out or Path("reports") / f"{socket.gethostname()}-{time.strftime('%Y%m%d-%H%M')}.md"
        print(write_report(args.metrics, out))
    elif args.cmd == "sources":
        from .media import discover_ndi_sources, ndi_available

        if not ndi_available():
            print("ndisrc not available (NDI plugin or runtime missing)")
        for name in discover_ndi_sources(3.0):
            print(name)


if __name__ == "__main__":
    main()
