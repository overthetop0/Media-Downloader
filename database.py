from sqlmodel import SQLModel, Field, create_engine, Session, Relationship
from typing import Optional, List
from datetime import datetime
from enum import Enum
from contextlib import contextmanager

class ProviderType(str, Enum):
    VOD = "vod"
    SERIES = "series"

class Provider(SQLModel, table=True):
    __tablename__ = "providers"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    host: str
    username: str
    password: str
    provider_type: ProviderType = Field(default=ProviderType.VOD)
    user_agent: str = Field(default="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")

    max_size_gb: float = Field(default=0.0)
    max_concurrent: int = Field(default=2)
    max_speed_mbps: float = Field(default=0.0)
    enabled: bool = Field(default=True)

    created_at: datetime = Field(default_factory=datetime.now)
    items: List["MediaItem"] = Relationship(back_populates="provider")

class ItemType(str, Enum):
    MOVIE = "movie"
    EPISODE = "episode"

class MediaItem(SQLModel, table=True):
    __tablename__ = "media_items"

    id: Optional[int] = Field(default=None, primary_key=True)
    provider_id: int = Field(foreign_key="providers.id")
    provider: Optional[Provider] = Relationship(back_populates="items")

    title: str
    original_title: Optional[str] = None
    item_type: ItemType = Field(default=ItemType.MOVIE)

    series_name: Optional[str] = None
    season_num: Optional[int] = None
    episode_num: Optional[int] = None

    url: str
    icon_url: Optional[str] = None
    group_title: Optional[str] = None
    stream_id: Optional[str] = None

    status: str = Field(default="pending")
    priority: int = Field(default=0)

    size_total_mb: float = Field(default=0.0)
    size_downloaded_mb: float = Field(default=0.0)
    speed_mbps: float = Field(default=0.0)
    progress_percent: float = Field(default=0.0)

    error_message: Optional[str] = None
    skip_reason: Optional[str] = None

    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)

engine = create_engine("sqlite:///media_downloader.db", echo=False)
SQLModel.metadata.create_all(engine)

# Auto-migration for existing DBs
from sqlalchemy import text
with engine.connect() as conn:
    for col, definition in [
        ("max_speed_mbps", "FLOAT DEFAULT 0.0"),
        ("user_agent",     "TEXT DEFAULT 'Mozilla/5.0'"),
    ]:
        try:
            conn.execute(text(f"ALTER TABLE providers ADD COLUMN {col} {definition}"))
            conn.commit()
        except Exception:
            pass

@contextmanager
def get_session():
    session = Session(engine)
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
