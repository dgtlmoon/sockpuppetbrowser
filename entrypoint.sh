#!/bin/sh
# Disable core dumps to prevent large files
ulimit -c 0

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

# Under an init, because this process ends up as PID 1 and Chrome leaves orphans behind it.
# Killing a browser reparents its renderers, crashpad handlers, Xvfb and xvfb-run's own
# helpers onto PID 1, and the proxy cannot wait() on processes it never spawned - without a
# reaper they pile up as zombies at roughly seven per connection. `docker run --init` does the
# same job from outside; running under both is harmless (-s registers tini as a subreaper when
# it is not PID 1 itself).
if command -v tini >/dev/null 2>&1; then
    exec tini -s -- python3 ./server.py "$@"
fi

echo "WARNING: tini not found - orphaned Chrome processes will not be reaped. Run the container with --init."
exec python3 ./server.py "$@"