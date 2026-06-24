import os
from starlette.staticfiles import StaticFiles

# Example of how it might be dynamically resolving the path
default_static_dir = os.path.join(os.path.dirname(__file__), "static")
# Allow overriding via environment variable or default to /app/static if it exists
static_dir = os.getenv("STATIC_DIR", "/app/static" if os.path.exists("/app/static") else default_static_dir)

app.mount("/static", StaticFiles(directory=static_dir), name="static")