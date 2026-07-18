#!/usr/bin/env bash

wait_for_stage_marker() {
  local marker="$1" producer_unit="$2" producer_session="$3"
  local wait_seconds="$4" label="$5"
  local state load_state active_state normalized_unit

  [[ "$wait_seconds" =~ ^[1-9][0-9]*$ ]] || {
    echo "wait_for_stage_marker requires a positive wait interval" >&2
    return 2
  }
  while [[ ! -s "$marker" ]]; do
    if [[ -n "$producer_unit" ]]; then
      normalized_unit="${producer_unit%.service}.service"
      state=$(systemctl --user show "$normalized_unit" \
        -p LoadState -p ActiveState -p SubState -p ExecMainStatus -p Result \
        2>/dev/null || true)
      load_state=$(printf '%s\n' "$state" | awk -F= '$1 == "LoadState" {print $2}')
      active_state=$(printf '%s\n' "$state" | awk -F= '$1 == "ActiveState" {print $2}')
      if [[ "$load_state" == "loaded" &&
            ( "$active_state" == "active" ||
              "$active_state" == "activating" ||
              "$active_state" == "reloading" ) ]]; then
        sleep "$wait_seconds"
        continue
      fi
      printf '%s producer ended without a completion marker\n%s\n' \
        "$label" "$state" >&2
      return 2
    fi
    if [[ -n "$producer_session" ]] &&
        tmux has-session -t "$producer_session" 2>/dev/null; then
      sleep "$wait_seconds"
      continue
    fi
    echo "$label producer ended without a completion marker" >&2
    return 2
  done
}
