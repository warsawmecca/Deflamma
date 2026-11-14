#!/usr/bin/env bash
# Install both the monitor and independent retention timer. Run after venv setup.
set -euo pipefail
root_dir="$(cd -- "$(dirname -- "$0")/.." && pwd)"
run_user="${SUDO_USER:-$(id -un)}"
[[ ${EUID} == 0 && "$run_user" != root ]] || { echo 'Use sudo from the normal server account.'; exit 1; }
python_bin="$root_dir/.venv/bin/python"
[[ -x "$python_bin" ]] || { echo 'Create .venv and install requirements first; see README.md'; exit 1; }
output_dir="$root_dir/monitor_integral_output"
mkdir -p "$output_dir"
chown "$run_user:$(id -gn "$run_user")" "$output_dir"
backup_dir="/etc/systemd/system/deflamma-backup-$(date +%Y%m%d%H%M%S)"
mkdir -p "$backup_dir"
systemctl stop himawari.service 2>/dev/null || true
[[ ! -e /etc/systemd/system/himawari.service ]] || cp -a /etc/systemd/system/himawari.service "$backup_dir/"
# Preserve all old overrides in a backup, avoiding conflicting ExecStart entries.
[[ ! -d /etc/systemd/system/himawari.service.d ]] || mv /etc/systemd/system/himawari.service.d "$backup_dir/"
cat > /etc/systemd/system/himawari.service <<EOF
[Unit]
Description=Deflamma national thermal monitor
Wants=network-online.target deflamma-cleanup.timer
After=network-online.target
[Service]
Type=simple
User=$run_user
WorkingDirectory=$root_dir
Environment=PYTHONUNBUFFERED=1
Environment=MPLCONFIGDIR=$output_dir/matplotlib_cache
Environment=HIMAWARI_PUBLIC_ORIGIN=https://deflamma.duckdns.org
Environment=DEFLAMMA_REVIEWERS_FILE=$root_dir/reviewers.json
Environment=DEFLAMMA_REVIEW_TTL_MINUTES=30
ExecStart="$python_bin" "$root_dir/monitor.py" --score-mode contrast --output "$output_dir" --latest-only --serve --port 8765
Restart=on-failure
RestartSec=20
TimeoutStopSec=30
UMask=0027
[Install]
WantedBy=multi-user.target
EOF
cat > /etc/systemd/system/deflamma-cleanup.service <<EOF
[Unit]
Description=Deflamma bounded data cleanup
[Service]
Type=oneshot
User=$run_user
WorkingDirectory=$root_dir
ExecStart="$python_bin" "$root_dir/retention.py" --output "$output_dir"
TimeoutStartSec=120
EOF
cat > /etc/systemd/system/deflamma-cleanup.timer <<'EOF'
[Unit]
Description=Clean Deflamma data even when acquisition is stopped
[Timer]
OnBootSec=2min
OnUnitActiveSec=1min
Persistent=true
[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable --now deflamma-cleanup.timer
systemctl restart deflamma-cleanup.timer
systemctl enable --now himawari.service
systemctl --no-pager status himawari.service deflamma-cleanup.timer || true
echo "Old configuration backed up at $backup_dir"
