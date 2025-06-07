#!/bin/sh
# Start Xvfb in background if headful mode might be used
if [ "${CHROME_HEADFUL}" = "true" ] || [ "${ENABLE_XVFB}" = "true" ]; then
    echo "Starting Xvfb on display :99"
    Xvfb :99 -screen 0 1920x1080x24 -ac +extension GLX > /dev/null 2>&1 &
    export XVFB_PID=$!
    export DISPLAY=:99
    sleep 1
fi

# Start the Python server
cd /usr/src/app
. ./bin/activate
exec python3 ./server.py "$@"