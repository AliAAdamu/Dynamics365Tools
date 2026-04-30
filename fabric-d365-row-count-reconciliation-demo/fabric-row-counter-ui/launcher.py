"""
PyInstaller entry point.

When frozen by PyInstaller, sys._MEIPASS points at the unpacked bundle that
contains app.py and count_rows.py. We launch Streamlit programmatically and
let it serve the bundled app on http://localhost:8501.
"""

from __future__ import annotations

import os
import sys
import threading
import time
import webbrowser


def resolve_bundle_dir() -> str:
    if getattr(sys, "frozen", False):
        return sys._MEIPASS  # type: ignore[attr-defined]
    return os.path.dirname(os.path.abspath(__file__))


def open_browser_when_ready(url: str) -> None:
    # Streamlit doesn't auto-open in headless mode; do it ourselves once it's up.
    import urllib.request
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=1).close()
            webbrowser.open(url)
            return
        except Exception:
            time.sleep(0.5)


def main() -> int:
    bundle = resolve_bundle_dir()
    app_path = os.path.join(bundle, "app.py")

    # Make sibling modules (count_rows.py) importable.
    if bundle not in sys.path:
        sys.path.insert(0, bundle)

    port = "8501"
    threading.Thread(
        target=open_browser_when_ready,
        args=(f"http://localhost:{port}",),
        daemon=True,
    ).start()

    from streamlit.web import cli as stcli  # noqa: WPS433

    sys.argv = [
        "streamlit", "run", app_path,
        "--server.port", port,
        "--server.headless", "true",
        "--browser.gatherUsageStats", "false",
        "--global.developmentMode", "false",
    ]
    return stcli.main()  # type: ignore[no-any-return]


if __name__ == "__main__":
    sys.exit(main())
