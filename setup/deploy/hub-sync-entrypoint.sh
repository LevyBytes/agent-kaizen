#!/bin/sh
set -eu

# Migrate existing named-volume ownership once, then run the service without root privileges.
if [ "$(id -u)" -eq 0 ]; then
  if [ "$(stat -c '%u:%g' /data)" != "10001:10001" ]; then
    chown -R kaizen-hub:kaizen-hub /data
  fi
  exec gosu kaizen-hub "$@"
fi
exec "$@"
