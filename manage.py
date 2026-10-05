#!/usr/bin/env python
"""Django's command-line utility for administrative tasks."""
import os
import socket
import sys
import threading
import time


def open_edge_when_server_is_ready(arguments):
    runserver_index = arguments.index('runserver')
    address = next(
        (argument for argument in arguments[runserver_index + 1:] if not argument.startswith('-')),
        None,
    )
    host = '::1' if '--ipv6' in arguments else '127.0.0.1'
    port = 8000

    if address:
        if address.startswith('[') and ']:' in address:
            host, port = address[1:].split(']:', 1)
        elif address.count(':') == 1:
            host, port = address.rsplit(':', 1)
            host = host or '127.0.0.1'
        elif ':' not in address:
            port = address
        else:
            host = address

    port = int(port)
    connection_host = '127.0.0.1' if host in ('', '0.0.0.0', '::') else host
    browser_host = f'[{connection_host}]' if ':' in connection_host else connection_host
    url = f'http://{browser_host}:{port}/'

    def wait_and_open():
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                with socket.create_connection((connection_host, port), timeout=0.5):
                    os.startfile(f'microsoft-edge:{url}')
                    return
            except OSError:
                time.sleep(0.2)

    threading.Thread(target=wait_and_open, daemon=True).start()


def main():
    """Run administrative tasks."""
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'django_ims.settings')
    if (
        sys.platform == 'win32'
        and 'runserver' in sys.argv
        and os.environ.get('RUN_MAIN') != 'true'
    ):
        open_edge_when_server_is_ready(sys.argv[1:])
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Are you sure it's installed and "
            "available on your PYTHONPATH environment variable? Did you "
            "forget to activate a virtual environment?"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == '__main__':
    main()
