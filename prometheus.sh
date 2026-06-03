#!/usr/bin/env bash
# prometheus.sh — Prometheus core management script
# HUD is manually launched only — not managed by this script.
set -euo pipefail

PROJ="/home/tatel/Desktop/PROMETHEUS/Prometheus_Main"
MAIN_SVC="prometheus.service"

_usage() {
    echo "Usage: prometheus.sh <command>"
    echo ""
    echo "Commands:"
    echo "  start         Start Prometheus core"
    echo "  stop          Stop Prometheus core"
    echo "  restart       Restart Prometheus core"
    echo "  status        Show core service status"
    echo "  logs          Follow live logs"
    echo "  enable        Enable core for autostart"
    echo "  disable       Disable core autostart"
    echo ""
    echo "HUD is launched manually: ~/Desktop/PROMETHEUS/Frontend_Dashboard/launch_dashboard.sh"
}

case "${1:-}" in
    start)
        systemctl --user start "$MAIN_SVC"
        echo "Prometheus core started."
        ;;
    stop)
        systemctl --user stop "$MAIN_SVC" 2>/dev/null || true
        echo "Prometheus core stopped."
        ;;
    restart)
        systemctl --user restart "$MAIN_SVC"
        echo "Prometheus core restarted."
        ;;
    status)
        echo "=== Core ==="
        systemctl --user status "$MAIN_SVC" --no-pager -l || true
        ;;
    logs)
        journalctl --user -f -u "$MAIN_SVC"
        ;;
    enable)
        systemctl --user daemon-reload
        systemctl --user enable "$MAIN_SVC"
        echo "Autostart enabled."
        ;;
    disable)
        systemctl --user disable "$MAIN_SVC"
        echo "Autostart disabled."
        ;;
    *)
        _usage
        exit 1
        ;;
esac
