"""Root entrypoint forwarding to app.main for backward compatibility."""
from app.main import app

__all__ = ["app"]
