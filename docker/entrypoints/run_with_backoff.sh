#!/bin/sh
set -eu

if [ "$#" -eq 0 ]; then
  echo "Usage: run_with_backoff.sh <command> [args...]" >&2
  exit 64
fi

/app/docker/entrypoints/wait_for_services.sh

attempt=1
max_attempts=${STARTUP_RUN_ATTEMPTS:-30}
delay=${STARTUP_RETRY_SECONDS:-2}
max_delay=${STARTUP_RETRY_MAX_SECONDS:-15}

while :; do
  "$@"
  status=$?
  if [ "$status" -eq 0 ]; then
    exit 0
  fi

  if [ "$attempt" -ge "$max_attempts" ]; then
    echo "Command failed after $attempt attempts: $*" >&2
    exit "$status"
  fi

  echo "Command failed with status $status. Retrying in ${delay}s: $*" >&2
  sleep "$delay"
  attempt=$((attempt + 1))

  next_delay=$((delay * 2))
  if [ "$next_delay" -gt "$max_delay" ]; then
    delay=$max_delay
  else
    delay=$next_delay
  fi
done