import asyncio
import logging
from database import Provider, MediaItem, ItemType, get_session, ProviderType
from sqlmodel import select
from xtream_api import XtreamAPI, XtreamSeriesAPI

logger = logging.getLogger(__name__)


class ProviderScanner:
    """Scanner che popola il DB dai provider Xtream"""

    @staticmethod
    async def scan_vod_provider(provider: Provider):
        """Scansiona film e popola DB"""
        async with XtreamAPI(provider.host, provider.username, provider.password) as api:
            try:
                categories = await api.get_categories()
                if not categories:
                    logger.warning(f"Nessuna categoria VOD per {provider.name}")
                    return

                streams = await api.get_vod_streams()

                with get_session() as db:
                    added = 0
                    for stream in streams:
                        exists = db.exec(
                            select(MediaItem).where(
                                MediaItem.provider_id == provider.id,
                                MediaItem.stream_id == str(stream.stream_id)
                            )
                        ).first()

                        if not exists:
                            item = MediaItem(
                                provider_id=provider.id,
                                title=stream.name,
                                item_type=ItemType.MOVIE,
                                url=f"{provider.host}/movie/{provider.username}/{provider.password}/{stream.stream_id}.{stream.extension}",
                                icon_url=stream.clean_icon,
                                group_title=api.get_category_name(stream.category_id),
                                stream_id=str(stream.stream_id)
                            )
                            db.add(item)
                            added += 1

                    db.commit()
                    logger.info(f"[VOD] {provider.name}: aggiunti {added} nuovi film")

            except Exception as e:
                logger.error(f"Errore scan VOD {provider.name}: {e}")

    @staticmethod
    async def scan_series_provider(provider: Provider):
        """Scansiona serie TV e popola DB"""
        async with XtreamSeriesAPI(provider.host, provider.username, provider.password) as api:
            try:
                categories = await api.get_categories()
                if not categories:
                    logger.warning(f"Nessuna categoria serie per {provider.name}")
                    return

                series_list = await api.get_series_list()
                logger.info(f"[Serie] {provider.name}: trovate {len(series_list)} serie, recupero episodi...")

                added = 0
                for i, series in enumerate(series_list):
                    logger.info(f"[{i+1}/{len(series_list)}] {series.name}")
                    detailed = await api.get_series_info(series.series_id)
                    if not detailed or not detailed.seasons:
                        continue

                    with get_session() as db:
                        for season in detailed.seasons:
                            for ep in season.episodes:
                                exists = db.exec(
                                    select(MediaItem).where(
                                        MediaItem.provider_id == provider.id,
                                        MediaItem.stream_id == str(ep.id)
                                    )
                                ).first()

                                if not exists:
                                    item = MediaItem(
                                        provider_id=provider.id,
                                        title=f"{detailed.name} - S{season.season_num:02d}E{ep.episode_num:02d}",
                                        original_title=ep.title,
                                        item_type=ItemType.EPISODE,
                                        series_name=detailed.name,
                                        season_num=season.season_num,
                                        episode_num=ep.episode_num,
                                        url=f"{provider.host}/series/{provider.username}/{provider.password}/{ep.id}.{ep.container_extension}",
                                        icon_url=detailed.clean_icon,
                                        group_title=api.get_category_name(detailed.category_id),
                                        stream_id=str(ep.id)
                                    )
                                    db.add(item)
                                    added += 1
                        db.commit()

                    await asyncio.sleep(0.3)

                logger.info(f"[Serie] {provider.name}: aggiunti {added} nuovi episodi")

            except Exception as e:
                logger.error(f"Errore scan serie {provider.name}: {e}")
