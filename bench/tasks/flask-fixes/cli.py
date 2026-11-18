#!/usr/bin/env python3
"""Run the notely service.

    python3 cli.py            # serves on 127.0.0.1:8000
"""
import argparse

from app.routes import app


def main(argv=None):
    parser = argparse.ArgumentParser(description="notely notes service")
    parser.add_argument("--host", default="127.0.0.1",
                        help="interface to bind (default: 127.0.0.1)")
    args = parser.parse_args(argv)
    app.serve(args.host, 8000)


if __name__ == "__main__":
    main()
