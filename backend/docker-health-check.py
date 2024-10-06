#!/usr/bin/python3

import sys
import socket
import urllib.request
import argparse
from urllib.parse import urlparse
import datetime

def main():
    parser = argparse.ArgumentParser(description='Health check script')
    parser.add_argument(
        '--host',
        default='http://localhost',
        help='Hostname or URL to check (default: http://localhost)'
    )
    args = parser.parse_args()

    # Extract hostname and scheme from the provided host argument
    host_input = args.host

    # Parse the URL to extract components
    parsed_url = urlparse(host_input)

    # If scheme is missing, assume 'http' and parse again
    if not parsed_url.scheme:
        host_input = f'http://{host_input}'
        parsed_url = urlparse(host_input)

    hostname = parsed_url.hostname or 'localhost'
    scheme = parsed_url.scheme or 'http'
    netloc = parsed_url.netloc or 'localhost'

    # Reconstruct the base URL without any path, params, query, or fragment
    base_url = f'{scheme}://{netloc}'

    # Prepare the log file path
    log_file_path = '/tmp/healthcheck.log'

    # Get the current timestamp
    timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    try:
        # Check connection to port 3000
        s = socket.create_connection((hostname, 3000), timeout=5)
        s.close()
    except Exception as e:
        # Log the exception with date/time
        with open(log_file_path, 'a') as log_file:
            log_file.write(f'[{timestamp}] Port 3000 check failed: {e}\n')
        sys.exit(1)

    try:
        # Check HTTP status code at /stats on port 8080
        stats_url = f'{base_url}:8080/stats'
        response = urllib.request.urlopen(stats_url, timeout=5)
        if response.status != 200:
            # Log the unexpected status code with date/time
            with open(log_file_path, 'a') as log_file:
                log_file.write(f'[{timestamp}] HTTP request to {stats_url} returned status code {response.status}\n')
            sys.exit(1)
    except Exception as e:
        # Log the exception with date/time
        with open(log_file_path, 'a') as log_file:
            log_file.write(f'[{timestamp}] HTTP request to {stats_url} failed: {e}\n')
        sys.exit(1)

    # Health check passed
    sys.exit(0)

if __name__ == '__main__':
    main()
