import stac_fastapi.geoparquet.api
from fastapi.staticfiles import StaticFiles

api = stac_fastapi.geoparquet.api.create()
app = api.app

# Mount the static directory to serve local assets
app.mount("/static", StaticFiles(directory="static"), name="static")
