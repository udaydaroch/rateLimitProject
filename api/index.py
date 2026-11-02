"""Vercel entrypoint. The Python runtime serves the ASGI `app` exported here;
vercel.json rewrites every path to this function."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.main import app  # noqa: E402,F401
