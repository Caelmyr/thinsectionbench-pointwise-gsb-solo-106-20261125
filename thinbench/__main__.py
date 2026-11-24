"""Shell and container entry point."""
import os

from waitress import serve

from .server import create_app

PORT = int(os.environ.get("PORT", "8000"))
HOST = os.environ.get("HOST", "0.0.0.0")

if __name__ == "__main__":
    serve(create_app(), host=HOST, port=PORT, threads=4, channel_timeout=50)
