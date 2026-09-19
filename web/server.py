import json
import os
from pathlib import Path

from aiohttp import web


DOWNLOAD_DIR = Path("/downloads").resolve()
TOKEN_FILE = Path("/downloads/.file_tokens.json")
PORT = int(os.getenv("WEB_PORT", "8080"))


def load_tokens():
    try:
        if TOKEN_FILE.exists():
            with TOKEN_FILE.open("r") as f:
                return json.load(f)
    except Exception as e:
        print(f"Token load error: {e}")

    return {}


async def serve_file(request):
    token = request.match_info["token"]

    tokens = load_tokens()
    file_path_str = tokens.get(token)

    if not file_path_str:
        raise web.HTTPNotFound(text="Invalid or expired link")

    file_path = Path(file_path_str).resolve()

    try:
        file_path.relative_to(DOWNLOAD_DIR)
    except ValueError:
        raise web.HTTPForbidden()

    if not file_path.is_file():
        raise web.HTTPNotFound(text="File not found")

    return web.FileResponse(file_path)


app = web.Application()

app.router.add_get(
    "/files/{token}/{filename:.*}",
    serve_file,
)

web.run_app(app, host="0.0.0.0", port=PORT)
