#!/bin/sh
# Named volume(/app/data,/app/spool)은 root 소유로 마운트되므로 appuser(10001) 쓰기 전에 맞춤.
set -eu
mkdir -p /app/data /app/spool
chown -R appuser:appuser /app/data /app/spool
exec runuser -u appuser -- "$@"
