#!/bin/bash
# P&I PO Tool — startup script
# Double-click this (or run: bash start.sh) to launch the tool

cd "$(dirname "$0")"

echo ""
echo "========================================="
echo "  P&I Purchase Order Tool"
echo "========================================="

# Kill any old instances
pkill -f "python3 app.py" 2>/dev/null
pkill -f "cloudflared tunnel" 2>/dev/null
sleep 1

# Start Flask
echo ""
echo "▶  Starting app..."
python3 app.py > /tmp/flask.log 2>&1 &
sleep 3

# Check Flask started
if curl -s -o /dev/null -w "%{http_code}" http://localhost:5050/login | grep -q "200"; then
  echo "✓  App running"
else
  echo "✗  App failed to start — check /tmp/flask.log"
  exit 1
fi

# Start Cloudflare tunnel
echo "▶  Opening public URL..."
/tmp/cloudflared tunnel --url http://localhost:5050 --no-autoupdate > /tmp/cf.log 2>&1 &
sleep 8

# Get URL
URL=$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' /tmp/cf.log | head -1)

echo ""
echo "========================================="
echo "  READY"
echo ""
echo "  URL:      $URL"
echo "  Password: PI2026"
echo "========================================="
echo ""
echo "  Share this URL with your team."
echo "  Keep this window open while using the tool."
echo ""

# Open in browser automatically
open "$URL" 2>/dev/null

# Wait so window stays open
read -p "  Press Enter to stop the tool..."
pkill -f "python3 app.py"
pkill -f "cloudflared tunnel"
echo "  Tool stopped."
