#!/usr/bin/env bash
# prometheus.sh — Prometheus management script
set -euo pipefail

PROJ="/home/tatel/Desktop/PROMETHEUS/Prometheus_Main"
MAIN_SVC="prometheus.service"
HUD_SVC="prometheus-hud.service"

_usage() {
    echo "Usage: prometheus.sh <command>"
    echo ""
    echo "Commands:"
    echo "  start         Start both Prometheus and HUD"
    echo "  stop          Stop both"
    echo "  restart       Restart both"
    echo "  restart-core  Restart only the core assistant"
    echo "  restart-hud   Restart only the HUD"
    echo "  status        Show status of both services"
    echo "  logs          Follow live logs (core + HUD)"
    echo "  logs-core     Follow only core logs"
    echo "  logs-hud      Follow only HUD logs"
    echo "  enable        Enable both services for autostart"
    echo "  disable       Disable autostart"
}

case "${1:-}" in
    start)
        systemctl --user start "$MAIN_SVC" "$HUD_SVC"
        echo "Prometheus started."
        ;;
    stop)
        systemctl --user stop "$HUD_SVC" "$MAIN_SVC" 2>/dev/null || true
        echo "Prometheus stopped."
        ;;
    restart)
        systemctl --user restart "$MAIN_SVC" "$HUD_SVC"
        echo "Prometheus restarted."
        ;;
    restart-core)
        systemctl --user restart "$MAIN_SVC"
        echo "Core restarted."
        ;;
    restart-hud)
        systemctl --user restart "$HUD_SVC"
        echo "HUD restarted."
        ;;
    status)
        echo "=== Core ==="
        systemctl --user status "$MAIN_SVC" --no-pager -l || true
        echo ""
        echo "=== HUD ==="
        systemctl --user status "$HUD_SVC" --no-pager -l || true
        ;;
    logs)
        journalctl --user -f -u "$MAIN_SVC" -u "$HUD_SVC"
        ;;
    logs-core)
        journalctl --user -f -u "$MAIN_SVC"
        ;;
    logs-hud)
        journalctl --user -f -u "$HUD_SVC"
        ;;
    enable)
        systemctl --user daemon-reload
        systemctl --user enable "$MAIN_SVC" "$HUD_SVC"
        echo "Autostart enabled."
        ;;
    disable)
        systemctl --user disable "$MAIN_SVC" "$HUD_SVC"
        echo "Autostart disabled."
        ;;
    *)
        _usage
        exit 1
        ;;
esac
