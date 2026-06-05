import asyncio
import aiohttp
import aiofiles
import logging
import re
import time
from pathlib import Path
from typing import Dict, Optional
from datetime import datetime
from database import get_session, MediaItem, Provider, ItemType
from sqlmodel import select

logger = logging.getLogger(__name__)

DEFAULT_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

def _safe(text: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', '_', text or "Unknown").strip() or "Unknown"


class DownloadWorker:
    def __init__(self, item_id: int, max_speed_mbps: float = 0.0, user_agent: str = DEFAULT_UA):
        self.item_id = item_id
        self.max_speed_mbps = max_speed_mbps
        self.user_agent = user_agent
        self.cancelled = False

    async def run(self):
        with get_session() as db:
            item = db.get(MediaItem, self.item_id)
            if not item:
                return
            provider = db.get(Provider, item.provider_id)
            if not provider:
                return

            item.status = "downloading"
            item.updated_at = datetime.now()
            db.commit()

            if provider.max_size_gb > 0:
                size_gb = await self._check_size(item.url)
                if size_gb and size_gb > provider.max_size_gb:
                    item.status = "skipped_size"
                    item.skip_reason = f"Troppo grande: {size_gb:.2f}GB > {provider.max_size_gb}GB"
                    db.commit()
                    logger.info(f"SKIPPED: {item.title}")
                    return

            try:
                await self._download(item, provider, db)
            except asyncio.CancelledError:
                item.status = "pending"
                item.error_message = "Cancellato"
                db.commit()
                raise
            except Exception as e:
                item.status = "error"
                item.error_message = str(e)
                db.commit()
                logger.error(f"Error {item.title}: {e}")

    async def _check_size(self, url: str) -> Optional[float]:
        try:
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.head(url, ssl=False, allow_redirects=True) as r:
                    if r.status == 200:
                        size = int(r.headers.get('content-length', 0))
                        return size / (1024 ** 3) if size > 0 else None
        except Exception:
            pass
        return None

    async def _download(self, item: MediaItem, provider: Provider, db):
        base = Path("./downloads") / _safe(provider.name)

        if item.item_type == ItemType.EPISODE and item.series_name:
            cat    = _safe(item.group_title or "No Category")
            series = _safe(item.series_name)
            season = f"S{item.season_num:02d}" if item.season_num else "S01"
            folder = base / "Series" / cat / series / season
        else:
            cat    = _safe(item.group_title or "No Category")
            folder = base / "Movies" / cat

        folder.mkdir(parents=True, exist_ok=True)

        ext      = Path(item.url).suffix or '.mp4'
        filepath = folder / f"{_safe(item.title)}{ext}"

        max_bps = self.max_speed_mbps * 1024 * 1024 if self.max_speed_mbps > 0 else 0
        timeout = aiohttp.ClientTimeout(total=None, connect=30, sock_read=60)
        headers = {'User-Agent': self.user_agent}

        # Resume support: check if partial file exists
        resume_pos = 0
        if filepath.exists():
            resume_pos = filepath.stat().st_size
            if resume_pos > 0:
                headers['Range'] = f'bytes={resume_pos}-'
                logger.info(f"Resume {item.title} from {resume_pos/1024/1024:.1f} MB")

        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(item.url, ssl=False) as resp:
                # 200 = full file (server ignored Range), 206 = partial OK
                if resp.status == 200:
                    resume_pos = 0  # server did not honour Range, restart
                elif resp.status == 206:
                    pass  # resume confirmed
                else:
                    raise Exception(f"HTTP {resp.status}")

                total_remaining = int(resp.headers.get('content-length', 0))
                total = resume_pos + total_remaining if total_remaining > 0 else 0
                if total > 0:
                    item.size_total_mb = total / (1024 ** 2)

                downloaded  = resume_pos  # already on disk
                start       = time.monotonic()
                last_commit = start

                open_mode = 'ab' if resume_pos > 0 else 'wb'
                async with aiofiles.open(filepath, open_mode) as f:
                    async for chunk in resp.content.iter_chunked(65536):
                        if self.cancelled:
                            raise asyncio.CancelledError()

                        await f.write(chunk)
                        downloaded += len(chunk)
                        now = time.monotonic()

                        if max_bps > 0:
                            elapsed_bps = now - start
                            written_since_start = downloaded - resume_pos
                            expected = max_bps * elapsed_bps
                            if written_since_start > expected:
                                await asyncio.sleep((written_since_start - expected) / max_bps)
                                now = time.monotonic()

                        if now - last_commit >= 1.0:
                            item.size_downloaded_mb = downloaded / (1024 ** 2)
                            item.progress_percent   = (downloaded / total * 100) if total > 0 else 0
                            elapsed = now - start
                            written = downloaded - resume_pos
                            item.speed_mbps = (written / elapsed) / (1024 ** 2) if elapsed > 0 else 0
                            db.commit()
                            last_commit = now

                item.status             = "completed"
                item.progress_percent   = 100.0
                item.size_downloaded_mb = downloaded / (1024 ** 2)
                item.speed_mbps         = 0.0
                item.updated_at         = datetime.now()
                db.commit()
                logger.info(f"Done: {item.title} -> {filepath}")


class ProviderManager:
    def __init__(self, provider_id: int):
        self.provider_id = provider_id
        self.queue: asyncio.Queue = asyncio.Queue()
        self.active: Dict[int, asyncio.Task] = {}
        self.running = False

    async def start(self):
        self.running = True
        with get_session() as db:
            provider = db.get(Provider, self.provider_id)
            if not provider or not provider.enabled:
                self.running = False
                return
            max_concurrent = provider.max_concurrent
            max_speed      = provider.max_speed_mbps
            user_agent     = provider.user_agent or DEFAULT_UA

        await self._load_pending()

        workers = [asyncio.create_task(self._loop(max_speed, user_agent))
                   for _ in range(max_concurrent)]
        await asyncio.gather(*workers, return_exceptions=True)

    async def _load_pending(self):
        with get_session() as db:
            # Items stuck as "downloading" (app crash) -> reset to pending so they resume
            stuck = db.exec(
                select(MediaItem).where(
                    MediaItem.provider_id == self.provider_id,
                    MediaItem.status == "downloading"
                )
            ).all()
            for item in stuck:
                item.status      = "pending"
                item.speed_mbps  = 0.0
            if stuck:
                db.commit()
                logger.info(f"Provider {self.provider_id}: reset {len(stuck)} stuck downloads -> pending")

            items = db.exec(
                select(MediaItem).where(
                    MediaItem.provider_id == self.provider_id,
                    MediaItem.status == "pending"
                ).order_by(MediaItem.priority.desc(), MediaItem.id)
            ).all()
            for item in items:
                await self.queue.put(item.id)
        logger.info(f"Provider {self.provider_id}: {self.queue.qsize()} items queued")

    async def _loop(self, max_speed: float, user_agent: str):
        while self.running:
            try:
                item_id = await asyncio.wait_for(self.queue.get(), timeout=2.0)
                worker  = DownloadWorker(item_id, max_speed, user_agent)
                task    = asyncio.create_task(worker.run())
                self.active[item_id] = task
                try:
                    await task
                finally:
                    self.active.pop(item_id, None)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Worker error: {e}")

    def stop(self):
        self.running = False
        for t in self.active.values():
            t.cancel()


class GlobalDownloadManager:
    def __init__(self):
        self.providers: Dict[int, ProviderManager] = {}

    async def add_provider(self, provider_id: int):
        if provider_id not in self.providers:
            mgr = ProviderManager(provider_id)
            self.providers[provider_id] = mgr
            asyncio.create_task(mgr.start())

    def stop_provider(self, provider_id: int):
        if provider_id in self.providers:
            self.providers[provider_id].stop()
            del self.providers[provider_id]

    async def queue_item(self, item_id: int, provider_id: int):
        if provider_id in self.providers:
            await self.providers[provider_id].queue.put(item_id)

    def stop_all(self):
        for mgr in self.providers.values():
            mgr.stop()
        self.providers.clear()


download_manager = GlobalDownloadManager()