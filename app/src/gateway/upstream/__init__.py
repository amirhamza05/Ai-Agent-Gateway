"""Async HTTP clients for upstream providers — populated in Phase 3+.

P3 adds ``openrouter.py``; P5 adds ``qdrant.py``. Both wrap a shared
:class:`httpx.AsyncClient` and inject the relevant API key from settings.
"""
