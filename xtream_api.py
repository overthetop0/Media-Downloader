#!/usr/bin/env python3
"""
Xtream Codes API - Modulo unificato VOD + Serie TV
Basato sugli script api_m3u.py e api_series_m3u.py (TMDB rimosso)
"""

import asyncio
import aiohttp
import json
import logging
from typing import Dict, List, Optional, Set
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
#  Dataclasses VOD
# ─────────────────────────────────────────────

@dataclass
class VodStream:
    stream_id: int
    name: str
    icon: str
    category_id: str
    extension: str
    rating: Optional[str] = None

    @property
    def clean_icon(self) -> str:
        if not self.icon:
            return ""
        url = self.icon.replace("\\/", "/")
        if url.count("://") > 1:
            last_http = max(url.rfind("http://"), url.rfind("https://"))
            if last_http > 0:
                url = url[last_http:]
        return url


# ─────────────────────────────────────────────
#  Dataclasses Serie TV
# ─────────────────────────────────────────────

@dataclass
class Episode:
    id: int
    episode_num: int
    title: Optional[str]
    container_extension: str
    info: dict = field(default_factory=dict)

    @property
    def display_title(self) -> str:
        if self.title and self.title.strip():
            return f"E{self.episode_num:02d} - {self.title.strip()}"
        return f"E{self.episode_num:02d}"


@dataclass
class Season:
    season_num: int
    episodes: List[Episode] = field(default_factory=list)

    @property
    def name(self) -> str:
        return f"Stagione {self.season_num}"


@dataclass
class Series:
    series_id: int
    name: str
    category_id: str
    icon: str
    rating: Optional[str]
    seasons: List[Season] = field(default_factory=list)
    info: dict = field(default_factory=dict)

    @property
    def clean_icon(self) -> str:
        if not self.icon:
            return ""
        url = self.icon.replace("\\/", "/")
        if url.count("://") > 1:
            last_http = max(url.rfind("http://"), url.rfind("https://"))
            if last_http > 0:
                url = url[last_http:]
        return url


# ─────────────────────────────────────────────
#  Client API base condiviso
# ─────────────────────────────────────────────

class _BaseXtreamAPI:
    def __init__(self, host: str, username: str, password: str,
                 delay: float = 0.5, max_items: int = 50000):
        self.host = host.rstrip('/')
        self.username = username
        self.password = password
        self.delay = delay
        self.max_items = max_items
        self.session: Optional[aiohttp.ClientSession] = None
        self._categories_map: Dict[str, str] = {}

        if not self.host.startswith(('http://', 'https://')):
            self.host = 'http://' + self.host

    async def __aenter__(self):
        timeout = aiohttp.ClientTimeout(total=30, connect=10)
        connector = aiohttp.TCPConnector(limit=5, limit_per_host=3)
        self.session = aiohttp.ClientSession(
            connector=connector,
            timeout=timeout,
            headers={
                'User-Agent': 'Mozilla/5.0 (Qt; Android) AppleWebKit/537.36',
                'Accept': 'application/json'
            }
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()

    def _build_url(self, action: str, **params) -> str:
        base = f"{self.host}/player_api.php"
        query = {
            'username': self.username,
            'password': self.password,
            'action': action,
            **params
        }
        query_str = '&'.join(f"{k}={v}" for k, v in query.items())
        return f"{base}?{query_str}"

    async def _api_call(self, url: str):
        for attempt in range(3):
            try:
                await asyncio.sleep(self.delay)
                async with self.session.get(url, ssl=False) as response:
                    if response.status == 401:
                        logger.error("Autenticazione fallita")
                        return None
                    if response.status != 200:
                        logger.warning(f"HTTP {response.status}")
                        continue
                    text = await response.text()
                    return json.loads(text)
            except Exception as e:
                logger.warning(f"Tentativo {attempt + 1} fallito: {e}")
                await asyncio.sleep(2 ** attempt)
        return None

    def get_category_name(self, cat_id: str) -> str:
        return self._categories_map.get(str(cat_id), "Unknown")


# ─────────────────────────────────────────────
#  API VOD (Film)
# ─────────────────────────────────────────────

class XtreamAPI(_BaseXtreamAPI):
    """Client API per VOD (Film)"""

    def __init__(self, host: str, username: str, password: str,
                 delay: float = 0.5, max_vods: int = 50000):
        super().__init__(host, username, password, delay, max_vods)

    async def get_categories(self) -> Dict[str, str]:
        logger.info("Recupero categorie VOD...")
        url = self._build_url('get_vod_categories')
        data = await self._api_call(url)

        if data is None:
            raise Exception("Autenticazione fallita")

        cats = data if isinstance(data, list) else []
        self._categories_map = {
            str(cat['category_id']): cat['category_name']
            for cat in cats
            if 'category_id' in cat and 'category_name' in cat
        }
        logger.info(f"Trovate {len(self._categories_map)} categorie VOD")
        return self._categories_map

    async def get_vod_streams(self, category_id: Optional[str] = None) -> List[VodStream]:
        all_streams: List[VodStream] = []
        seen_ids: Set[int] = set()
        limit = 1000
        start = 0
        empty_count = 0

        logger.info(f"Recupero VOD (max: {self.max_items})...")

        while len(all_streams) < self.max_items:
            url = self._build_url('get_vod_streams', limit=limit, start=start)
            if category_id:
                url += f"&category_id={category_id}"

            data = await self._api_call(url)
            batch = data if isinstance(data, list) else []

            if not batch:
                empty_count += 1
                if empty_count >= 3:
                    break
                start += limit
                continue
            else:
                empty_count = 0

            new_count = 0
            duplicates = 0

            for item in batch:
                try:
                    sid = int(item.get('stream_id', 0))
                    if sid in seen_ids:
                        duplicates += 1
                        continue
                    seen_ids.add(sid)
                    all_streams.append(VodStream(
                        stream_id=sid,
                        name=item.get('name', 'Unknown').strip(),
                        icon=item.get('stream_icon', ''),
                        category_id=str(item.get('category_id', '0')),
                        extension=item.get('container_extension', 'mp4'),
                        rating=str(item.get('rating_5based', ''))
                    ))
                    new_count += 1
                except Exception:
                    continue

            logger.info(f"start={start}: {new_count} nuovi (totale: {len(all_streams)})")

            if len(batch) > 0 and duplicates / len(batch) > 0.5:
                logger.warning("Troppi duplicati, fine lista")
                break
            if len(batch) < limit:
                break

            start += limit

        logger.info(f"Totale VOD unici: {len(all_streams)}")
        return all_streams


# ─────────────────────────────────────────────
#  API Serie TV
# ─────────────────────────────────────────────

class XtreamSeriesAPI(_BaseXtreamAPI):
    """Client API per Serie TV"""

    def __init__(self, host: str, username: str, password: str,
                 delay: float = 0.5, max_series: int = 50000):
        super().__init__(host, username, password, delay, max_series)

    async def get_categories(self) -> Dict[str, str]:
        logger.info("Recupero categorie serie TV...")
        url = self._build_url('get_series_categories')
        data = await self._api_call(url)

        if data is None:
            raise Exception("Autenticazione fallita")

        cats = data if isinstance(data, list) else data.get('categories', [])
        self._categories_map = {
            str(cat['category_id']): cat['category_name']
            for cat in cats
            if 'category_id' in cat and 'category_name' in cat
        }
        logger.info(f"Trovate {len(self._categories_map)} categorie serie")
        return self._categories_map

    async def get_series_list(self, category_id: Optional[str] = None) -> List[Series]:
        all_series: List[Series] = []
        seen_ids: Set[int] = set()
        limit = 1000
        start = 0
        empty_count = 0

        logger.info(f"Recupero elenco serie (max: {self.max_items})...")

        while len(all_series) < self.max_items:
            url = self._build_url('get_series', limit=limit, start=start)
            if category_id:
                url += f"&category_id={category_id}"

            data = await self._api_call(url)
            batch = data if isinstance(data, list) else []

            if not batch:
                empty_count += 1
                if empty_count >= 3:
                    break
                start += limit
                continue
            else:
                empty_count = 0

            new_count = 0
            duplicates = 0

            for item in batch:
                try:
                    sid = int(item.get('series_id', 0))
                    if sid in seen_ids:
                        duplicates += 1
                        continue
                    seen_ids.add(sid)
                    all_series.append(Series(
                        series_id=sid,
                        name=item.get('name', 'Unknown').strip(),
                        category_id=str(item.get('category_id', '0')),
                        icon=item.get('cover', item.get('stream_icon', '')),
                        rating=str(item.get('rating_5based', '')),
                        info=item.get('info', {})
                    ))
                    new_count += 1
                except Exception:
                    continue

            logger.info(f"start={start}: {new_count} nuove serie (totale: {len(all_series)})")

            if len(batch) > 0 and duplicates / len(batch) > 0.5:
                logger.warning("Troppi duplicati, fine lista")
                break
            if len(batch) < limit:
                break

            start += limit

        logger.info(f"Totale serie trovate: {len(all_series)}")
        return all_series

    async def get_series_info(self, series_id: int) -> Optional[Series]:
        """Recupera stagioni ed episodi di una serie"""
        url = self._build_url('get_series_info', series_id=series_id)
        data = await self._api_call(url)

        if not data or not isinstance(data, dict):
            return None

        info = data.get('info', {})
        episodes_data = data.get('episodes', {})

        series = Series(
            series_id=series_id,
            name=info.get('name', 'Unknown'),
            category_id=str(info.get('category_id', '0')),
            icon=info.get('cover', ''),
            rating=str(info.get('rating_5based', '')),
            info=info
        )

        seasons_map: Dict[int, Season] = {}
        for season_num_str, episodes_list in episodes_data.items():
            try:
                season_num = int(season_num_str)
            except Exception:
                continue

            season = Season(season_num=season_num)
            for ep_data in episodes_list:
                try:
                    episode = Episode(
                        id=int(ep_data.get('id', 0)),
                        episode_num=int(ep_data.get('episode_num', 0)),
                        title=ep_data.get('title', ''),
                        container_extension=ep_data.get('container_extension', 'mp4'),
                        info=ep_data.get('info', {})
                    )
                    season.episodes.append(episode)
                except Exception:
                    continue

            if season.episodes:
                season.episodes.sort(key=lambda e: e.episode_num)
                seasons_map[season_num] = season

        series.seasons = [seasons_map[k] for k in sorted(seasons_map.keys())]
        return series
