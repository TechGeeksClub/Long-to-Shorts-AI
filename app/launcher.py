from __future__ import annotations

import argparse
import http.client
import os
import socket
import threading
import webbrowser

import uvicorn


HOST = "127.0.0.1"


def open_when_ready(port: int, stop: threading.Event) -> None:
    while not stop.wait(0.2):
        connection = http.client.HTTPConnection(HOST, port, timeout=0.5)
        try:
            connection.request("GET", "/api/health")
            if connection.getresponse().status != 200:
                continue
        except (OSError, http.client.HTTPException):
            continue
        finally:
            connection.close()
        if not stop.is_set():
            webbrowser.open(f"http://{HOST}:{port}")
        return


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Shorts Studio locally.")
    parser.add_argument("--port", type=int, default=8000, help="Local port (default: 8000).")
    parser.add_argument("--no-browser", action="store_true", help="Leave the browser closed.")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")

    with socket.socket() as listener:
        address_option = socket.SO_EXCLUSIVEADDRUSE if os.name == "nt" else socket.SO_REUSEADDR
        listener.setsockopt(socket.SOL_SOCKET, address_option, 1)
        try:
            listener.bind((HOST, args.port))
        except OSError as exc:
            parser.exit(1, f"Could not listen on {HOST}:{args.port} ({exc}).\n")

        url = f"http://{HOST}:{args.port}"
        print(f"Shorts Studio: {url}\nPress Ctrl+C to stop.", flush=True)
        server = uvicorn.Server(uvicorn.Config("app.main:app", host=HOST, port=args.port))
        stop = threading.Event()
        if not args.no_browser:
            threading.Thread(target=open_when_ready, args=(args.port, stop), daemon=True).start()
        try:
            server.run(sockets=[listener])
        except KeyboardInterrupt:
            return 0
        finally:
            stop.set()
        return 0 if server.started else 1


if __name__ == "__main__":
    raise SystemExit(main())
