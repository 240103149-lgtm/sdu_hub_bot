"""Vercel entrypoint.

Vercel's Python runtime looks for a top-level ASGI/WSGI callable named
`app` in the file a build points at (see vercel.json). The real
application lives in ../main.py so the website and this entrypoint always
share the same code — this file only makes it importable from api/.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from main import app  # noqa: E402  (import after sys.path fix, that's the point)

__all__ = ["app"]
