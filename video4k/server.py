"""A probe endpoint so the Load node can show a file's details before it runs."""
import os

from aiohttp import web
import server

from . import media
from .nodes import resolve_video


@server.PromptServer.instance.routes.get("/video4k/probe")
async def probe_video(request):
    query = request.rel_url.query
    try:
        path, count = resolve_video(query.get("source", "upload"), query.get("video", ""),
                                    query.get("path", ""), int(query.get("video_index", 0) or 0))
        if not os.path.isfile(path):
            return web.json_response({"error": "not found"}, status=404)
        info = media.probe(path)
    except Exception as error:
        return web.json_response({"error": str(error)}, status=400)
    return web.json_response({
        "file": os.path.basename(path),
        "width": info.width,
        "height": info.height,
        "fps": round(info.fps, 3),
        "duration": round(info.duration, 3),
        "frames": info.frame_count,
        "has_audio": info.has_audio,
        "videos_in_folder": count,
    })
