from fastapi import FastAPI, Request, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import JSONResponse, FileResponse, Response
from app.database.connection import settings, files_col, create_indexes
from app.streamer.manager import session_manager
from app.streamer.engine import get_streaming_response, get_remux_response
from app.streamer.probe import probe_tracks
from app.streamer import subs as subs_service
from app.bot.main import register_handlers
from app.admin.routes import router as admin_router
from fastapi.middleware.gzip import GZipMiddleware
import uvicorn
import asyncio
import logging
import sys

# Configure Windows Event Loop Policy for subprocess support
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

# Configure Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup logic
    await create_indexes()
    await session_manager.start()
    register_handlers(session_manager.bot_client)
    logger.info("Application started")
    yield
    # Shutdown logic
    await session_manager.stop()
    logger.info("Application stopped")

app = FastAPI(title="Telegram Direct Media Link Generator", lifespan=lifespan)

# Include Routers
app.include_router(admin_router)

# Enable Compression
app.add_middleware(GZipMiddleware, minimum_size=1000)

# Mount Static Files & Templates
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/watch/{short_code}")
async def watch_page(request: Request, short_code: str):
    file_data = await files_col.find_one({"short_code": short_code})
    if not file_data:
        raise HTTPException(status_code=404, detail="Link not found or expired")
    
    return templates.TemplateResponse("watch.html", {
        "request": request,
        "file": file_data,
        "base_url": settings.BASE_URL
    })

@app.get("/dl/{short_code}")
@app.get("/stream/{short_code}")
async def stream_file(request: Request, short_code: str):
    file_data = await files_col.find_one({"short_code": short_code})
    if not file_data:
        raise HTTPException(status_code=404, detail="File not found")

    # Use all available clients for ultra-high-speed downloads
    clients = session_manager.get_all_clients()
    client = clients[0]  # Use first client for initial message fetch
    
    try:
        # Fetch the message that contains the media
        msg = await client.get_messages(file_data['chat_id'], ids=file_data['message_id'])
        if not msg or not msg.media:
            raise HTTPException(status_code=404, detail="Media no longer available on Telegram")
        
        file = msg.media
        # Some media types are nested
        if hasattr(file, 'document'):
            file = file.document
        elif hasattr(file, 'photo'):
            file = file.photo
            
    except Exception as e:
        logger.error(f"Error fetching file: {e}")
        raise HTTPException(status_code=500, detail="Error retrieving file from Telegram")

    return await get_streaming_response(
        clients,  # Pass ALL clients for parallel downloading
        file=file,
        file_size=file_data['file_size'],
        filename=file_data['filename'],
        mime_type=file_data['mime_type'],
        request=request
    )


# ─── Audio Track Discovery API ────────────────────────────────────────────────

@app.get("/api/tracks/{short_code}")
async def get_tracks(short_code: str):
    """Return available audio/video/subtitle tracks for a media file."""
    file_data = await files_col.find_one({"short_code": short_code})
    if not file_data:
        raise HTTPException(status_code=404, detail="File not found")
    
    # Check if tracks are already cached in the database (and not an error result)
    cached = file_data.get('tracks_info')
    if False and cached and not cached.get('error'):
        return JSONResponse(cached)
    
    # Need to probe the file
    client = session_manager.get_client()
    
    try:
        msg = await client.get_messages(file_data['chat_id'], ids=file_data['message_id'])
        if not msg or not msg.media:
            raise HTTPException(status_code=404, detail="Media no longer available")
        
        media = msg.media
        
        # Photos don't have audio tracks
        if hasattr(media, 'photo') and not hasattr(media, 'document'):
            return JSONResponse({
                "video_tracks": [],
                "audio_tracks": [],
                "subtitle_tracks": [],
                "has_multiple_audio": False
            })
        
        # Pass the message for download_media fallback, and media for iter_download
        tracks_info = await probe_tracks(client, msg, file_data['file_size'])
        
        # Only cache if no error
        if not tracks_info.get('error'):
            await files_col.update_one(
                {"short_code": short_code},
                {"$set": {"tracks_info": tracks_info}}
            )
        
        return JSONResponse(tracks_info)
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Track probing error: {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail="Failed to probe tracks")


# ─── Subtitles ────────────────────────────────────────────────────────────────

@app.get("/api/qualities/{short_code}")
async def get_qualities(short_code: str):
    """
    Return the selectable quality options for a file.

    Embedded video streams are offered as-is (stream copy = no server load).
    Downscaled options are only advertised when the operator enables
    ENABLE_TRANSCODE_QUALITY, because those require real transcoding.
    """
    file_data = await files_col.find_one({"short_code": short_code})
    if not file_data:
        raise HTTPException(status_code=404, detail="File not found")

    tracks = file_data.get("tracks_info") or {}
    video_tracks = tracks.get("video_tracks") or []

    options = [{
        "id": "source",
        "label": "Original (Direct, full speed)",
        "url": f"/stream/{short_code}",
        "transcoded": False,
    }]

    for v in video_tracks[1:]:
        options.append({
            "id": f"v{v.get('video_index', 0)}",
            "label": v.get("quality_label") or "Alternate video",
            "url": f"/remux/{short_code}?video={v.get('video_index', 0)}",
            "transcoded": False,
        })

    if settings.ENABLE_TRANSCODE_QUALITY:
        source_height = (video_tracks[0].get("height") if video_tracks else 0) or 0
        for h in settings.transcode_heights:
            if source_height and h >= source_height:
                continue
            options.append({
                "id": f"h{h}",
                "label": f"{h}p (data saver)",
                "url": f"/remux/{short_code}?height={h}",
                "transcoded": True,
            })

    return JSONResponse({"options": options})


@app.get("/subtitle/{short_code}/{sub_index}.vtt")
async def get_subtitle(short_code: str, sub_index: int):
    """
    Return an embedded subtitle track converted to WebVTT.

    The track is extracted once and cached on disk, so repeat viewers get a
    tiny static file with long-lived cache headers (CDN friendly).
    """
    file_data = await files_col.find_one({"short_code": short_code})
    if not file_data:
        raise HTTPException(status_code=404, detail="File not found")

    headers = {
        "Content-Type": "text/vtt; charset=utf-8",
        "Cache-Control": "public, max-age=604800",
        "Access-Control-Allow-Origin": "*",
    }

    cached = subs_service.cached_subtitle(short_code, sub_index)
    if cached:
        return FileResponse(cached, media_type="text/vtt", headers=headers)

    # Validate the requested track against the probed info when available
    tracks = file_data.get("tracks_info") or {}
    sub_tracks = tracks.get("subtitle_tracks") or []
    if sub_tracks:
        match = next((t for t in sub_tracks if t.get("subtitle_index") == sub_index), None)
        if match is None:
            raise HTTPException(status_code=404, detail="Subtitle track not found")
        if not match.get("text_based", True):
            raise HTTPException(status_code=415, detail="Image-based subtitles cannot be shown in the browser")

    client = session_manager.get_client()
    try:
        msg = await client.get_messages(file_data['chat_id'], ids=file_data['message_id'])
        if not msg or not msg.media:
            raise HTTPException(status_code=404, detail="Media no longer available")

        media = msg.media
        if hasattr(media, 'document'):
            media = media.document

        path = await subs_service.get_subtitle_vtt(
            client, media, file_data['file_size'], short_code, sub_index
        )
        return FileResponse(path, media_type="text/vtt", headers=headers)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Subtitle extraction error: {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail="Failed to extract subtitles")


# ─── Remux Streaming (Audio Track Selection) ──────────────────────────────────

@app.get("/remux/{short_code}")
async def remux_file(request: Request, short_code: str, audio: int = 0, video: int = 0, height: int = 0):
    """
    Stream media remuxed with a selected audio and/or video track.
    Uses FFmpeg stream-copy (no transcoding) into fragmented MP4.
    
    Query params:
        audio:  Audio track index (0-based, default 0)
        video:  Video track index (0-based, default 0) - used for quality
                switching when the file carries multiple video streams.
        height: Optional downscale target (e.g. 480/720). Requires
                ENABLE_TRANSCODE_QUALITY=true, otherwise ignored so the
                server never spends CPU on transcoding.
    """
    if height and not settings.ENABLE_TRANSCODE_QUALITY:
        height = 0
    if height and height not in settings.transcode_heights:
        raise HTTPException(status_code=400, detail="Unsupported quality")
    file_data = await files_col.find_one({"short_code": short_code})
    if not file_data:
        raise HTTPException(status_code=404, detail="File not found")
    
    # Use a single client for sequential download (FFmpeg needs sequential input)
    client = session_manager.get_client()
    
    try:
        msg = await client.get_messages(file_data['chat_id'], ids=file_data['message_id'])
        if not msg or not msg.media:
            raise HTTPException(status_code=404, detail="Media no longer available")
        
        file = msg.media
        if hasattr(file, 'document'):
            file = file.document
        elif hasattr(file, 'photo'):
            file = file.photo
        
        return await get_remux_response(
            client=client,
            file=file,
            file_size=file_data['file_size'],
            filename=file_data['filename'],
            audio_track=audio,
            video_track=video,
            max_height=height
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Remux error: {e}")
        raise HTTPException(status_code=500, detail="Error starting remux stream")


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 8000))
    # use loop="asyncio" to prevent Uvicorn from forcing SelectorEventLoop on Windows,
    # which causes NotImplementedError with asyncio.create_subprocess_exec
    uvicorn.run("app.main:app", host="0.0.0.0", port=port, reload=True, loop="asyncio")
