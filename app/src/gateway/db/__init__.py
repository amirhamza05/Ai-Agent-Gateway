"""Database layer — async SQLAlchemy 2.0.

Public surface:
    * :class:`gateway.db.models.Base` — declarative base for all tables.
    * :func:`gateway.db.session.create_engine` — async engine factory.
    * :func:`gateway.db.session.create_session_factory` — async sessionmaker.
"""
