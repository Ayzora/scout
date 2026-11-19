from __future__ import annotations

import functools
import http.server
import threading
from pathlib import Path

import pytest

FIXTURE_SITE = Path(__file__).parent / "fixtures" / "site"


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):  # silence the test run
        pass


@pytest.fixture(scope="session")
def fixture_site() -> str:
    """Serve tests/fixtures/site on a loopback port; yields the base URL."""
    handler = functools.partial(_QuietHandler, directory=str(FIXTURE_SITE))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
