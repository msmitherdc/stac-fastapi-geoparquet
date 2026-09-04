import os

from fastapi.staticfiles import StaticFiles

import stac_fastapi.geoparquet.api
from stac_fastapi.geoparquet import access_tags

# GRiD access-tag filtering is optional (see `access_tags`), but it *is* how
# this deployment enforces access control, so it's on unless explicitly
# disabled — a silent default-off would serve restricted collections publicly.
ACCESS_TAGS_ENABLED = os.getenv("STAC_FASTAPI_ACCESS_TAGS", "true").lower() not in (
    "0",
    "false",
    "no",
)

create = (
    access_tags.create if ACCESS_TAGS_ENABLED else stac_fastapi.geoparquet.api.create
)

api = create()
app = api.app

# Static assets (Swagger UI) ship inside the package; resolve relative to this
# module so it works in dev, installed wheels, and the Lambda image alike.
# STATIC_DIR can override the location if needed.
default_static_dir = os.path.join(os.path.dirname(__file__), "static")
static_dir = os.getenv("STATIC_DIR", default_static_dir)
app.mount("/static", StaticFiles(directory=static_dir), name="static")
