"""Capture the official client's browser launch URL without opening a container browser."""
import json
import os
import sys
from pathlib import Path


def main():
    destination = Path(os.environ["PREDICTION_CLIENT_BROWSER_REQUEST"])
    urls = [arg for arg in sys.argv[1:] if arg.startswith("https://")]
    if len(urls) != 1:
        raise SystemExit(1)
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump({"url": urls[0]}, stream)


if __name__ == "__main__":
    main()
