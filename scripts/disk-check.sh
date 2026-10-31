#!/usr/bin/env bash
# scripts/disk-check.sh
# Host disk capacity monitoring script for Incident Copilot
# Checks root filesystem usage, logs Docker storage utilization,
# and exits non-zero if utilization exceeds the 80% threshold.
#
# Usage (or run via hourly cron):
#   ./scripts/disk-check.sh

set -euo pipefail

THRESHOLD=80
ROOT_USAGE="$(df -P / | awk 'NR==2 {gsub("%","",$5); print $5}')"

echo "=========================================================="
echo " Incident Copilot Disk Utilization Check                  "
echo " Timestamp: $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
echo " Current / utilization: ${ROOT_USAGE}% (Threshold: ${THRESHOLD}%)"
echo "=========================================================="

echo "--> Docker Storage Breakdown (docker system df):"
docker system df || true
echo "----------------------------------------------------------"

if [ "${ROOT_USAGE}" -ge "${THRESHOLD}" ]; then
    echo "CRITICAL WARNING: Root disk usage is at ${ROOT_USAGE}%, exceeding the ${THRESHOLD}% safety threshold!" >&2
    echo "Action required: prune unused Docker layers ('docker system prune -af') or expand OCI boot volume." >&2
    exit 1
fi

echo "--> Disk health check PASSED (${ROOT_USAGE}% < ${THRESHOLD}%)."
exit 0
