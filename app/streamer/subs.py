"""
Subtitle Extraction Service
───────────────────────────
Extracts a single embedded subtitle track from a Telegram-hosted media file
and converts it to WebVTT so the browser player can render it natively.

Design goals (low server load):
  * Only ONE subtitle stream is decoded at a time (`-map 0:s:N`), video and
    audio are dropped (`-vn -an`) so FFmpeg does almost no work.
  * The resulting .vtt is tiny and cached on disk, so a track is extracted
    once per file — every later viewer gets a static file (CDN cacheable).
  * A global semaphore limits concurrent extractions, and identical in-flight
    requests share the same task instead of downloading the file twice.
"""
import asyncio
import logging
import os
import subprocess
import tempfile

logger = logging.getLogger(__name__)

CACHE_DIR = os.environ.get("SUBTITLE_CACHE_DIR", os.path.join("app", "cache", "subs"))
MAX_CONCURRENT_EXTRACTIONS = int(os.environ.get("SUBTITLE_MAX_CONCURRENT", "2"))

# Bitmap subtitles cannot be converted to WebVTT (they are images).
BITMAP_CODECS = {"hdmv_pgs_subtitle", "pgssub", "dvd_subtitle", "dvdsub", "dvb_subtitle", "xsub"}

_semaphore = asyncio.Semaphore(MAX_CONCURRENT_EXTRACTIONS)
_inflight: dict[str, asyncio.Task] = {}


def is_text_subtitle(codec: str) -> bool:
    return (codec or "").lower() not in BITMAP_CODECS


def cache_path(short_code: str, sub_index: int) -> str:
    return os.path.join(CACHE_DIR, f"{short_code}_{sub_index}.vtt")


def cached_subtitle(short_code: str, sub_index: int) -> str | None:
    """Return the cached .vtt path if it already exists."""
    path = cache_path(short_code, sub_index)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return path
    return None


async def get_subtitle_vtt(client, file, file_size: int, short_code: str, sub_index: int) -> str:
    """
    Return the path of a WebVTT file for subtitle stream `sub_index`.
    Extracts it (once) if not cached. Raises RuntimeError on failure.
    """
    cached = cached_subtitle(short_code, sub_index)
    if cached:
        return cached

    key = f"{short_code}:{sub_index}"
    task = _inflight.get(key)
    if task is None:
        task = asyncio.create_task(_extract(client, file, file_size, short_code, sub_index))
        _inflight[key] = task
        task.add_done_callback(lambda _t, k=key: _inflight.pop(k, None))
    return await asyncio.shield(task)


async def _extract(client, file, file_size: int, short_code: str, sub_index: int) -> str:
    async with _semaphore:
        cached = cached_subtitle(short_code, sub_index)
        if cached:
            return cached

        os.makedirs(CACHE_DIR, exist_ok=True)
        out_path = cache_path(short_code, sub_index)
        fd, tmp_path = tempfile.mkstemp(suffix=".vtt", dir=CACHE_DIR)
        os.close(fd)

        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", "pipe:0",
            "-map", f"0:s:{sub_index}",
            "-vn", "-an",                # never touch video/audio → minimal CPU
            "-c:s", "webvtt",
            "-f", "webvtt",
            tmp_path,
        ]

        logger.info(f"Extracting subtitle {sub_index} for {short_code} ({file_size/1024/1024:.1f}MB source)")
        process = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

        async def feed():
            try:
                async for chunk in client.iter_download(file, offset=0, limit=file_size, request_size=1024 * 1024):
                    if not chunk:
                        continue
                    if process.poll() is not None:
                        break
                    await asyncio.to_thread(process.stdin.write, bytes(chunk))
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:  # noqa: BLE001
                logger.warning(f"Subtitle feed error: {e}")
            finally:
                try:
                    process.stdin.close()
                except Exception:
                    pass

        try:
            await feed()
            await asyncio.to_thread(process.wait)

            if process.returncode != 0 or not os.path.exists(tmp_path) or os.path.getsize(tmp_path) == 0:
                err = process.stderr.read().decode(errors="replace")[:300]
                raise RuntimeError(f"ffmpeg subtitle extraction failed: {err or 'empty output'}")

            os.replace(tmp_path, out_path)
            logger.info(f"Subtitle cached: {out_path} ({os.path.getsize(out_path)} bytes)")
            return out_path
        finally:
            if process.poll() is None:
                try:
                    process.kill()
                except Exception:
                    pass
            if os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass
