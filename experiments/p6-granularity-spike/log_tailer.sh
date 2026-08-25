#!/usr/bin/env bash
# p6c1 spike — preserve Arm A's runner/SUT container logs. THROWAWAY.
#
# WHY: `cv-infra run` tears both containers down in its finally (M3 §3.9), so the boot
# trace and the per-job GT/oracle lines the runner prints die with the container. This
# follower attaches `docker logs -f` to every cv-infra-labelled container as it appears
# and keeps the stream on disk. Read-only: it never stops, kills or inspects anything
# beyond `docker ps` / `docker logs`.
#
#   usage: log_tailer.sh <dir> [interval_s]      stop: kill the PID
set -euo pipefail

DIR="${1:?usage: log_tailer.sh <dir> [interval_s]}"
INTERVAL="${2:-0.5}"
mkdir -p "$DIR"

while :; do
  while IFS= read -r name; do
    [[ -z "$name" ]] && continue
    if [[ ! -e "$DIR/$name.log" ]]; then
      : > "$DIR/$name.log"
      docker logs -f "$name" >> "$DIR/$name.log" 2>&1 &
    fi
  done < <(docker ps --filter label=cv-infra.job_id --format '{{.Names}}' 2>/dev/null || true)
  sleep "$INTERVAL"
done
