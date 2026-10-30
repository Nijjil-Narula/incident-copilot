"""
api/app/routers/__init__.py

Package marker for the routers module.

Each router module in this package exposes an APIRouter instance that is
registered in app/main.py:

  - health.py   : GET /health, GET /config
  - incidents.py: CRUD for incident tickets and chat history
  - knowledge.py: Document upload, indexing, and vector similarity search
"""

from app.routers import health, incidents, knowledge

__all__ = ["health", "incidents", "knowledge"]
