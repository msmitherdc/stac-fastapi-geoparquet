import os
import stac_fastapi.geoparquet.api
from fastapi.staticfiles import StaticFiles

api = stac_fastapi.geoparquet.api.create()
app = api.app

# Resolve the absolute path to the static directory
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
static_dir = os.path.join(BASE_DIR, "static")

# Mount the static directory to serve local assets
app.mount("/static", StaticFiles(directory=static_dir), name="static")
