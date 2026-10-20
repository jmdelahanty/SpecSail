"""
Serve the run monitor for one calibration run bundle.

Usage:
    pixi run monitor runs/<run_id> [--library LABEL=RUN_DIR ...] [--host 127.0.0.1] [--port 8765]

Then open http://127.0.0.1:8765/ in a browser. The page re-reads the bundle
every 15 s. --library adds runs to the "Compare shapes" card (default: this run).
"""

import argparse
import sys
from pathlib import Path

from run_monitor.server import make_server


def parse_library(items):
    runs = []
    for item in items:
        if "=" not in item:
            raise SystemExit(f"--library expects LABEL=RUN_DIR, got {item!r}")
        label, path = item.split("=", 1)
        runs.append((label, Path(path)))
    return runs


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--library", action="append", default=[], metavar="LABEL=RUN_DIR")
    parser.add_argument("--host", default="127.0.0.1",
                        help="address to bind (default 127.0.0.1; use SSH port forwarding to view remotely)")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    if not (args.run_dir / "log.jsonl").exists() and not (args.run_dir / "run_plan.json").exists():
        raise SystemExit(f"{args.run_dir} does not look like a calibration run bundle")
    library = parse_library(args.library)
    if library:
        library = [("this run", args.run_dir)] + library
    server = make_server(args.run_dir, library or None, args.host, args.port)
    host, port = server.server_address[:2]
    print(f"Run monitor for {args.run_dir.name}: http://{host}:{port}/  (Ctrl+C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
