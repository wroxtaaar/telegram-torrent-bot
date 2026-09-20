import json
import os
import shutil
from pathlib import Path

from aiohttp import web
from qbittorrentapi import Client

DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "/downloads")).resolve()
TOKEN_FILE = DOWNLOAD_DIR / ".file_tokens.json"
PORT = int(os.getenv("WEB_PORT", os.getenv("PORT", "8080")))
QBIT_HOST = os.getenv("QBIT_HOST", "127.0.0.1")
QBIT_PORT = int(os.getenv("QBIT_PORT", "8080"))
QBIT_USERNAME = os.getenv("QBITTORRENT_USERNAME", "")
QBIT_PASSWORD = os.getenv("QBITTORRENT_PASSWORD", "")
QBIT_AUTH_ENABLED = os.getenv("QBIT_AUTH_ENABLED", "true").lower() in ("1", "true", "yes")


def load_tokens():
    try:
        if TOKEN_FILE.exists():
            with TOKEN_FILE.open("r") as f:
                return json.load(f)
    except Exception as e:
        print(f"Token load error: {e}")
    return {}


def get_qbittorrent():
    client = Client(
        host=QBIT_HOST,
        port=QBIT_PORT,
        username=QBIT_USERNAME,
        password=QBIT_PASSWORD,
    )
    if QBIT_AUTH_ENABLED:
        client.auth_log_in()
    return client


def torrent_summary(torrent):
    return {
        "hash": torrent.hash,
        "name": torrent.name,
        "state": torrent.state,
        "progress": torrent.progress,
        "size": getattr(torrent, "size", 0),
        "downloaded": getattr(torrent, "downloaded", 0),
        "download_speed": getattr(torrent, "dlspeed", 0),
    }


async def health(request):
    return web.json_response({
        "status": "ok",
        "service": "telegram-torrent-bot",
    })


async def status(request):
    try:
        qb = get_qbittorrent()
        torrents = qb.torrents_info() or []
        total, used, free = shutil.disk_usage("/")
        return web.json_response({
            "status": "ok",
            "torrent_count": len(torrents),
            "downloading": sum(1 for t in torrents if getattr(t, "progress", 0) < 1 and getattr(t, "state", "") in ("downloading", "forcedDL", "queuedDL", "stalledDL", "checkingDL")),
            "completed": sum(1 for t in torrents if getattr(t, "progress", 0) >= 1),
            "disk": {
                "total": total,
                "used": used,
                "free": free,
                "used_percent": round((used / total * 100) if total else 0, 2),
            },
        })
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)}, status=503)


async def torrents(request):
    try:
        qb = get_qbittorrent()
        return web.json_response({
            "status": "ok",
            "torrents": [torrent_summary(t) for t in (qb.torrents_info() or [])],
        })
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)}, status=503)


async def serve_file(request):
    token = request.match_info["token"]
    file_path_str = load_tokens().get(token)
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
app.router.add_get("/health", health)
app.router.add_get("/status", status)
app.router.add_get("/torrents", torrents)
app.router.add_get("/files/{token}/{filename:.*}", serve_file)

web.run_app(app, host="0.0.0.0", port=PORT)
