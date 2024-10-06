#!/usr/bin/python3

import sys
import socket
import urllib.request
import argparse
from urllib.parse import urlparse

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

    try:
        # Strip 'http://' or 'https://' for socket connection
        # Check connection to port 3000
        s = socket.create_connection((hostname, 3000), timeout=5)
        s.close()

        # Check HTTP status code at /stats on port 8080
        stats_url = f'{base_url}:8080/stats'
        response = urllib.request.urlopen(stats_url, timeout=5)
        sys.exit(0 if response.status == 200 else 1)
    except Exception as e:
        # Optionally, print the exception for debugging
        print(f'Health check failed: {e}', file=sys.stderr)
        sys.exit(1)

if __name__ == '__main__':
    main()