#!/bin/sh
set -eu

python - <<'PY'
import os
import socket
import sys
import time

targets = [
    ("db", int(os.getenv("POSTGRES_PORT", "5432"))),
    ("broker", int(os.getenv("RABBITMQ_PORT", "5672"))),
    ("redis", int(os.getenv("REDIS_PORT", "6379"))),
]
attempts = int(os.getenv("STARTUP_WAIT_ATTEMPTS", "30"))
delay = float(os.getenv("STARTUP_WAIT_SECONDS", "2"))

for host, port in targets:
    for attempt in range(1, attempts + 1):
        try:
            with socket.create_connection((host, port), timeout=2):
                print(f"Service ready: {host}:{port}")
            break
        except OSError as exc:
            if attempt >= attempts:
                print(f"Timed out waiting for {host}:{port}: {exc}", file=sys.stderr)
                raise
            print(
                f"Waiting for {host}:{port} ({attempt}/{attempts}): {exc}",
                file=sys.stderr,
            )
            time.sleep(delay)
PY