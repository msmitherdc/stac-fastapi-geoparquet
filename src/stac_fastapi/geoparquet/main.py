import os

from fastapi.staticfiles import StaticFiles

import stac_fastapi.geoparquet.api

api = stac_fastapi.geoparquet.api.create()
app = api.app

# Static assets (Swagger UI) ship inside the package; resolve relative to this
# module so it works in dev, installed wheels, and the Lambda image alike.
# STATIC_DIR can override the location if needed.
default_static_dir = os.path.join(os.path.dirname(__file__), "static")
static_dir = os.getenv("STATIC_DIR", default_static_dir)
app.mount("/static", StaticFiles(directory=static_dir), name="static")
