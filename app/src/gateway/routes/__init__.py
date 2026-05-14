"""HTTP routers grouped by feature area.

Phase 1 ships :mod:`gateway.routes.health`. Phase 2 adds :mod:`gateway.routes.usage`
(placeholder). Phase 3 adds :mod:`gateway.routes.messages` (streaming
passthrough) and replaces the usage placeholder with a real aggregate query.
``embeddings`` and ``vectors`` land in P5 per §5 of the plan.
"""
