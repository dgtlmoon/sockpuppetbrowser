#!/bin/sh
# Start the Python server
cd /usr/src/app
. ./bin/activate
exec python3 ./server.py "$@"