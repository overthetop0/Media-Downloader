import asyncio
import logging
import time
import shutil

import psutil
from nicegui import ui
from database import get_session, Provider, MediaItem, ProviderType, ItemType
from sqlmodel import select
from download_manager import download_manager
from providers import ProviderScanner
from xtream_api import XtreamAPI, XtreamSeriesAPI

logger = logging.getLogger(__name__)

CARD  = "background:#1e1e2e;border:1px solid #313244;border-radius:12px;"
HDR   = "background:#181825;border-bottom:1px solid #313244;"
INPUT = "color:#cdd6f4 !important;"

_net_prev  = None
_net_time  = None

# Global store: survives NiceGUI page refreshes (process-level singleton)
# { provider_id: {"path": str, "titles": M3UIndex} }
_M3U_STORE: dict = {}

USER_AGENTS = {
    "Windows Chrome": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Android Mobile": "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 Chrome/120.0.0.0 Mobile Safari/537.36",
    "Qt Player":      "Mozilla/5.0 (Qt; Android) AppleWebKit/537.36",
    "VLC":            "VLC/3.0.18 LibVLC/3.0.18",
    "Custom":         "",
}


def get_disk_info(path="./downloads"):
    try:
        import os; os.makedirs(path, exist_ok=True)
        u = shutil.disk_usage(path)
        return u.used/1024**3, u.total/1024**3, u.free/1024**3, u.used/u.total*100
    except Exception:
        return 0.0, 0.0, 0.0, 0.0


def get_net_speed():
    global _net_prev, _net_time
    try:
        now = time.monotonic()
        cur = psutil.net_io_counters()
        if _net_prev is None:
            _net_prev, _net_time = cur, now
            return 0.0, 0.0
        dt = now - _net_time
        if dt <= 0:
            return 0.0, 0.0
        down = (cur.bytes_recv - _net_prev.bytes_recv) / dt / 1024**2
        up   = (cur.bytes_sent - _net_prev.bytes_sent) / dt / 1024**2
        _net_prev, _net_time = cur, now
        return max(0.0, down), max(0.0, up)
    except Exception:
        return 0.0, 0.0


def _notify(msg: str, kind: str = "info"):
    """Safe notify that works even from background tasks."""
    try:
        ui.notify(msg, type=kind)
    except Exception:
        logger.info(f"[notify] {msg}")




# ---------------------------------------------------------------------------
# M3U comparison helper  --  fast token-index approach
# ---------------------------------------------------------------------------
# Strategy:
#   1. Normalise titles: lowercase, strip years/tags/punctuation, split into words
#   2. Build an inverted index: word -> set of title indices
#   3. For a new candidate title, collect only the titles that share >= 1 word
#      (the "candidate set"), then do a lightweight word-overlap score on those.
#   This reduces 15k comparisons to a small candidate set per query.
#
# Score formula: Jaccard on word sets  (intersection / union)
#   >= M3U_THRESHOLD  ->  skip the item
# ---------------------------------------------------------------------------
import re as _re

M3U_THRESHOLD = 0.55   # Jaccard 55% -- episode-code guard handles same-series/diff-ep

_STOP = {
    'the','a','an','of','in','on','at','to','and','or','is','it',
    'le','la','les','de','du','un','une','des','il','lo','el','los',
    's','e','i','o'
}

def _normalise_ep(t: str) -> str:
    """Convert 'Season 2 Episode 5', 's02e05', '2x05' etc. to canonical 's02e05'."""
    t2 = t.lower()
    # already canonical: s02e05
    t2 = _re.sub(r's(\d{1,2})\s*e(\d{1,2})', lambda m: f"s{int(m.group(1)):02d}e{int(m.group(2)):02d}", t2)
    # Season 2 Episode 5  /  stagione 2 episodio 5
    t2 = _re.sub(r'(season|stagione)\s*(\d{1,2})\s*(episode|episodio)\s*(\d{1,2})',
                 lambda m: f"s{int(m.group(2)):02d}e{int(m.group(4)):02d}", t2)
    # 2x05
    t2 = _re.sub(r'(\d{1,2})x(\d{1,2})',
                 lambda m: f"s{int(m.group(1)):02d}e{int(m.group(2)):02d}", t2)
    return t2


def _tokenise(t: str) -> list:
    """Lowercase, remove quality tags/years, normalise episode codes, tokenise."""
    t = t.lower()
    # normalise episode codes first (before stripping punctuation)
    t = _normalise_ep(t)
    # remove resolution/quality tags
    t = _re.sub(r'\b(\d{3,4}p|hdr|sdr|hevc|avc|web.?dl|bluray|remux|x26[45])\b', '', t)
    # remove 4-digit years
    t = _re.sub(r'\b(19|20)\d{2}\b', '', t)
    # remove non-alphanumeric except spaces (keep sXXeYY intact as one token)
    t = _re.sub(r'[^a-z0-9\s]', ' ', t)
    words = [w for w in t.split() if w not in _STOP and len(w) > 1]
    return words


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


class M3UIndex:
    """
    Inverted-index structure for fast fuzzy title matching.
    Built once when the M3U is loaded; lookups are O(candidates) not O(all).
    """
    __slots__ = ("titles", "word_sets", "index")

    def __init__(self, raw_titles: list):
        self.titles    = raw_titles          # normalised strings
        self.word_sets = []                  # list[set] parallel to titles
        self.index     = {}                  # word -> set of title indices

        for i, t in enumerate(raw_titles):
            ws = set(_tokenise(t))
            self.word_sets.append(ws)
            for w in ws:
                self.index.setdefault(w, set()).add(i)

    _EP_RE = _re.compile(r's\d{2}e\d{2}')

    def match(self, needle: str) -> bool:
        """Return True if needle matches any stored title at >= M3U_THRESHOLD.
        Special rule: if both titles contain an episode code and they differ,
        treat as non-match regardless of score (different episodes should not
        be skipped just because the series name matches).
        """
        nwords = set(_tokenise(needle))
        if not nwords:
            return False

        # extract episode code from needle (e.g. 's01e02')
        needle_norm = _normalise_ep(needle.lower())
        needle_ep   = self._EP_RE.search(needle_norm)

        candidates = set()
        for w in nwords:
            candidates.update(self.index.get(w, set()))

        for i in candidates:
            # episode-code guard: skip if both have codes but they differ
            if needle_ep:
                stored_ep = self._EP_RE.search(self.titles[i])
                if stored_ep and stored_ep.group() != needle_ep.group():
                    continue   # different episode -> never a match

            score = _jaccard(nwords, self.word_sets[i])
            if score >= M3U_THRESHOLD:
                logger.debug(
                    f"M3U match {score:.2f}: '{needle}' ~ '{self.titles[i]}'"
                )
                return True
        return False

    def __len__(self):
        return len(self.titles)


def _parse_m3u_index(m3u_path: str) -> "M3UIndex":
    """Parse M3U and return a ready-to-query M3UIndex."""
    titles = []
    try:
        with open(m3u_path, encoding='utf-8', errors='replace') as f:
            for line in f:
                line = line.strip()
                if line.startswith('#EXTINF') and ',' in line:
                    titles.append(line.split(',', 1)[-1].strip())
    except Exception as e:
        logger.warning(f"M3U parse error: {e}")
    idx = M3UIndex(titles)
    logger.info(f"M3U index built: {len(idx)} titles, {len(idx.index)} unique tokens")
    return idx


# keep old name for compatibility
def _parse_m3u_titles(m3u_path: str):
    return _parse_m3u_index(m3u_path)

# ---------------------------------------------------------------------------
# Main interface
# ---------------------------------------------------------------------------

class MainInterface:
    def __init__(self):
        self.tables              = {}
        self.providers_container = None
        self.stat_labels         = {}
        self.disk_label          = None
        self.disk_bar            = None
        self.net_down_label      = None
        self.net_up_label        = None
        self.dl_table            = None
        self.dl_filter_provider  = None
        self.dl_filter_status    = None
        # per-tab filter state
        self.film_filter_prov    = None
        self.film_page           = 0
        self.film_page_lbl       = None
        self.series_filter_prov  = None
        self.series_page         = 0
        self.series_page_lbl     = None
        self._client             = None   # NiceGUI client reference for bg tasks

    def build(self):
        # save client reference so background coroutines can use it
        try:
            from nicegui import context
            self._client = context.client
        except Exception:
            pass

        with ui.row().classes("w-full items-center q-px-md q-pt-sm q-pb-xs").style(HDR):
            ui.label("Media Downloader").style("color:#cdd6f4;font-size:1.2rem;font-weight:700;")
            ui.space()
            self.net_down_label = ui.label("DN: --").style("color:#a6e3a1;font-size:0.8rem;")
            self.net_up_label   = ui.label("UP: --").style("color:#f38ba8;font-size:0.8rem;margin-left:8px;")

        with ui.tabs().classes("w-full").style("background:#181825;") as tabs:
            ui.tab("Dashboard", icon="dashboard")
            ui.tab("Providers", icon="dns")
            ui.tab("Downloads", icon="downloading")
            ui.tab("Film",      icon="movie")
            ui.tab("Serie TV",  icon="tv")

        with ui.tab_panels(tabs, value="Dashboard").classes("w-full").style("background:#11111b;min-height:90vh;"):
            with ui.tab_panel("Dashboard"):
                self._build_dashboard()
            with ui.tab_panel("Providers"):
                self._build_providers()
            with ui.tab_panel("Downloads"):
                self._build_downloads_tab()
            with ui.tab_panel("Film"):
                self._build_media_list(ItemType.MOVIE)
            with ui.tab_panel("Serie TV"):
                self._build_media_list(ItemType.EPISODE)

        ui.timer(3.0, self._tick)

    # -----------------------------------------------------------------------
    # Dashboard
    # -----------------------------------------------------------------------

    def _build_dashboard(self):
        with ui.column().classes("w-full q-pa-md").style("gap:12px;"):
            stat_defs = [
                ("Totali",      "storage",      "#7287fd"),
                ("In attesa",   "schedule",     "#89b4fa"),
                ("Downloading", "downloading",  "#1e66f5"),
                ("Completati",  "check_circle", "#40a02b"),
                ("Saltati",     "block",        "#df8e1d"),
                ("Errori",      "error",        "#d20f39"),
            ]
            with ui.row().style("flex-wrap:wrap;gap:10px;"):
                for key, icon, color in stat_defs:
                    with ui.card().style(f"{CARD}flex:1;min-width:110px;padding:10px;"):
                        with ui.row().classes("items-center").style("gap:6px;"):
                            ui.icon(icon).style(f"color:{color};font-size:1.2rem;")
                            ui.label(key).style("color:#a6adc8;font-size:0.75rem;")
                        self.stat_labels[key] = ui.label("...").style(
                            f"color:{color};font-size:1.5rem;font-weight:700;"
                        )

            with ui.row().style("flex-wrap:wrap;gap:12px;"):
                with ui.card().style(f"{CARD}flex:1;min-width:220px;padding:14px;"):
                    ui.label("Spazio disco  ./downloads").style("color:#a6adc8;font-size:0.8rem;margin-bottom:6px;")
                    self.disk_label = ui.label("--").style("color:#cdd6f4;font-size:0.85rem;")
                    self.disk_bar   = ui.linear_progress(value=0).style("margin-top:6px;")

                with ui.card().style(f"{CARD}flex:1;min-width:220px;padding:14px;"):
                    ui.label("Controlli").style("color:#a6adc8;font-size:0.8rem;margin-bottom:6px;")
                    with ui.row().style("flex-wrap:wrap;gap:8px;"):
                        ui.button("Avvia Tutti",        color="positive", on_click=self._start_all).props("rounded")
                        ui.button("Stop Tutti",         color="negative", on_click=self._stop_all).props("rounded")
                        ui.button("Pulizia Completati", on_click=self._clean_completed).props("rounded flat")

            self._refresh_stats()
            self._refresh_disk()

    def _refresh_stats(self):
        if not self.stat_labels:
            return
        from sqlalchemy import func, text as sa_text
        from database import engine
        with engine.connect() as conn:
            rows = conn.execute(sa_text(
                "SELECT status, COUNT(*) as cnt FROM media_items GROUP BY status"
            )).fetchall()
        counts = {r[0]: r[1] for r in rows}
        total = sum(counts.values())
        data = {
            "Totali":      total,
            "In attesa":   counts.get("pending", 0),
            "Downloading": counts.get("downloading", 0),
            "Completati":  counts.get("completed", 0),
            "Saltati":     counts.get("skipped_size", 0),
            "Errori":      counts.get("error", 0),
        }
        for k, v in data.items():
            if k in self.stat_labels:
                self.stat_labels[k].set_text(str(v))

    def _refresh_disk(self):
        if not self.disk_label:
            return
        used, total, free, pct = get_disk_info()
        self.disk_label.set_text(f"Usato: {used:.1f} GB / {total:.1f} GB   Libero: {free:.1f} GB")
        if self.disk_bar:
            self.disk_bar.value = pct / 100

    # -----------------------------------------------------------------------
    # Providers
    # -----------------------------------------------------------------------

    def _build_providers(self):
        with ui.column().classes("w-full q-pa-md").style("gap:12px;"):
            with ui.card().style(f"{CARD}padding:16px;"):
                ui.label("Aggiungi Provider").style("color:#cdd6f4;font-weight:600;margin-bottom:10px;font-size:1rem;")
                with ui.column().classes("w-full").style("gap:8px;"):
                    with ui.row().style("flex-wrap:wrap;gap:8px;"):
                        name_inp = ui.input("Nome").props("dark").style("flex:1;min-width:120px;")
                        host_inp = ui.input("Host").props("dark").style("flex:2;min-width:180px;")
                    with ui.row().style("flex-wrap:wrap;gap:8px;"):
                        user_inp = ui.input("Username").props("dark").style("flex:1;min-width:120px;")
                        pass_inp = ui.input("Password", password=True).props("dark").style("flex:1;min-width:120px;")
                    with ui.row().style("flex-wrap:wrap;gap:8px;align-items:flex-end;"):
                        type_sel  = ui.select(["vod", "series"], label="Tipo", value="vod").props("dark").style("min-width:90px;")
                        size_inp  = ui.number("Max GB (0=inf)",   value=0, min=0, format="%.1f").props("dark").style("min-width:130px;flex:1;")
                        speed_inp = ui.number("Max MB/s (0=inf)", value=0, min=0, format="%.1f").props("dark").style("min-width:140px;flex:1;")
                        conc_inp  = ui.number("Paralleli",        value=2, min=1, max=10).props("dark").style("min-width:100px;flex:1;")
                    with ui.row().style("flex-wrap:wrap;gap:8px;align-items:flex-end;"):
                        ua_sel = ui.select(list(USER_AGENTS.keys()), label="User-Agent preset", value="Windows Chrome").props("dark").style("min-width:160px;flex:1;")
                        ua_inp = ui.input("User-Agent").props("dark").style("flex:2;min-width:220px;")
                        ua_inp.set_value(USER_AGENTS["Windows Chrome"])

                        def _on_ua():
                            preset = USER_AGENTS.get(ua_sel.value, "")
                            if preset:
                                ua_inp.set_value(preset)
                        ua_sel.on("update:model-value", lambda _: _on_ua())

                    ui.button("Salva", color="primary", on_click=lambda: _add()).props("rounded")

                def _add():
                    if not name_inp.value or not host_inp.value:
                        ui.notify("Nome e Host obbligatori", type="warning")
                        return
                    ua = ua_inp.value.strip() or USER_AGENTS["Windows Chrome"]
                    with get_session() as db:
                        db.add(Provider(
                            name=name_inp.value, host=host_inp.value.rstrip("/"),
                            username=user_inp.value, password=pass_inp.value,
                            user_agent=ua,
                            provider_type=ProviderType.VOD if type_sel.value == "vod" else ProviderType.SERIES,
                            max_size_gb=float(size_inp.value or 0),
                            max_speed_mbps=float(speed_inp.value or 0),
                            max_concurrent=int(conc_inp.value or 2),
                        ))
                        db.commit()
                    ui.notify("Provider aggiunto!", type="positive")
                    self._refresh_providers()

            self.providers_container = ui.column().classes("w-full").style("gap:10px;")
            self._refresh_providers()

    def _refresh_providers(self):
        if not self.providers_container:
            return
        self.providers_container.clear()
        with get_session() as db:
            rows = [{
                "id": p.id, "name": p.name,
                "provider_type": p.provider_type.value,
                "host": p.host, "username": p.username, "password": p.password,
                "max_size_gb": p.max_size_gb, "max_speed_mbps": p.max_speed_mbps,
                "max_concurrent": p.max_concurrent,
                "user_agent": p.user_agent or USER_AGENTS["Windows Chrome"],
            } for p in db.exec(select(Provider)).all()]

        with self.providers_container:
            if not rows:
                ui.label("Nessun provider configurato.").style("color:#6c7086;")
                return
            for p in rows:
                with ui.card().style(f"{CARD}padding:12px;"):
                    with ui.row().classes("items-center justify-between w-full").style("flex-wrap:wrap;gap:8px;"):
                        with ui.column().style("gap:2px;"):
                            ui.label(p["name"]).style("color:#cdd6f4;font-weight:700;font-size:1rem;")
                            spd = f"{p['max_speed_mbps']} MB/s" if p["max_speed_mbps"] > 0 else "illimitata"
                            sz  = f"{p['max_size_gb']} GB"      if p["max_size_gb"]    > 0 else "illimitata"
                            m3u_info = _M3U_STORE.get(p["id"])
                            m3u_lbl  = f"  |  M3U: {len(m3u_info['titles'])} titoli" if m3u_info else ""
                            ui.label(
                                f"{p['provider_type'].upper()}  |  Speed: {spd}  |  Max: {sz}  |  x{p['max_concurrent']}{m3u_lbl}"
                            ).style("color:#a6adc8;font-size:0.78rem;")
                            ui.label(p["host"]).style("color:#6c7086;font-size:0.72rem;")
                        with ui.row().style("flex-wrap:wrap;gap:4px;"):
                            ui.button("Cerca",     on_click=lambda pd=p: self._show_search(pd)).props("flat dense rounded").style("color:#cba6f7;")
                            ui.button("Anteprima", on_click=lambda pd=p: self._show_preview(pd)).props("flat dense rounded").style("color:#89b4fa;")
                            ui.button("Scansiona", on_click=lambda pid=p["id"]: self._scan_provider(pid)).props("flat dense rounded").style("color:#a6e3a1;")
                            ui.button("Avvia",     on_click=lambda pid=p["id"]: self._start_provider(pid)).props("flat dense rounded").style("color:#40a02b;")
                            ui.button("Modifica",   on_click=lambda pd=p: self._show_edit(pd)).props("flat dense rounded").style("color:#f9e2af;")
                            ui.button("M3U",       on_click=lambda pd=p: self._show_m3u_provider(pd)).props("flat dense rounded").style("color:#89dceb;")
                            ui.button("Stop",      on_click=lambda pid=p["id"]: self._stop_provider(pid)).props("flat dense rounded").style("color:#df8e1d;")
                            ui.button("Elimina",   on_click=lambda pid=p["id"]: self._confirm_delete_provider(pid)).props("flat dense rounded").style("color:#d20f39;")

    # -----------------------------------------------------------------------
    # Delete provider with cascade (FIX: delete items first)
    # -----------------------------------------------------------------------

    def _confirm_delete_provider(self, provider_id: int):
        with ui.dialog() as dlg, ui.card().style(f"{CARD}padding:20px;min-width:300px;"):
            ui.label("Conferma eliminazione").style("color:#cdd6f4;font-weight:700;margin-bottom:8px;")
            ui.label("Eliminare il provider e tutti i suoi contenuti dal DB?").style("color:#a6adc8;font-size:0.9rem;")
            ui.label("(I file gia scaricati non vengono toccati)").style("color:#6c7086;font-size:0.8rem;")
            with ui.row().style("gap:8px;margin-top:12px;"):
                def _do():
                    dlg.close()
                    self._delete_provider(provider_id)
                ui.button("Elimina", color="negative", on_click=_do).props("rounded")
                ui.button("Annulla", on_click=dlg.close).props("flat rounded")
        dlg.open()

    def _delete_provider(self, provider_id: int):
        download_manager.stop_provider(provider_id)
        with get_session() as db:
            # Delete all media items first to avoid FK / NOT NULL constraint
            items = db.exec(select(MediaItem).where(MediaItem.provider_id == provider_id)).all()
            for item in items:
                db.delete(item)
            db.flush()
            p = db.get(Provider, provider_id)
            if p:
                db.delete(p)
            db.commit()
        ui.notify("Provider eliminato", type="positive")
        self._refresh_providers()
        self._refresh_stats()

    # -----------------------------------------------------------------------
    # Clear pending items for a provider
    # -----------------------------------------------------------------------

    def _clear_pending(self, provider_id: int, provider_name: str):
        with ui.dialog() as dlg, ui.card().style(f"{CARD}padding:20px;min-width:300px;"):
            ui.label("Svuota coda pending").style("color:#cdd6f4;font-weight:700;margin-bottom:8px;")
            ui.label(f"Eliminare tutti gli item 'pending' di {provider_name} dal DB?").style("color:#a6adc8;font-size:0.9rem;")
            ui.label("I download in corso e completati non vengono toccati.").style("color:#6c7086;font-size:0.8rem;")
            with ui.row().style("gap:8px;margin-top:12px;"):
                def _do():
                    dlg.close()
                    with get_session() as db:
                        items = db.exec(select(MediaItem).where(
                            MediaItem.provider_id == provider_id,
                            MediaItem.status == "pending"
                        )).all()
                        count = len(items)
                        for i in items:
                            db.delete(i)
                        db.commit()
                    ui.notify(f"Rimossi {count} item pending", type="positive")
                    self._refresh_stats()
                    self._refresh_media_table(ItemType.MOVIE)
                    self._refresh_media_table(ItemType.EPISODE)
                ui.button("Svuota", color="warning", on_click=_do).props("rounded")
                ui.button("Annulla", on_click=dlg.close).props("flat rounded")
        dlg.open()

    # -----------------------------------------------------------------------
    # Preview dialog (category selection)
    # -----------------------------------------------------------------------

    def _show_preview(self, pd: dict):
        async def _load():
            dialog.open()
            status_lbl.set_text("Connessione al provider...")
            cat_col.clear()
            try:
                ptype = pd["provider_type"]
                if ptype == "vod":
                    async with XtreamAPI(pd["host"], pd["username"], pd["password"]) as api:
                        cats    = await api.get_categories()
                        streams = await api.get_vod_streams()
                else:
                    async with XtreamSeriesAPI(pd["host"], pd["username"], pd["password"]) as api:
                        cats    = await api.get_categories()
                        streams = await api.get_series_list()

                counts = {}
                for s in streams:
                    counts[s.category_id] = counts.get(s.category_id, 0) + 1
                status_lbl.set_text(f"{len(cats)} categorie  |  {len(streams)} elementi totali")

                selected = {}
                with cat_col:
                    with ui.row().style("gap:8px;margin-bottom:6px;"):
                        ui.button("Tutto",   on_click=lambda: [setattr(cb, "value", True)  for cb in selected.values()]).props("flat dense rounded").style("color:#89b4fa;")
                        ui.button("Nessuno", on_click=lambda: [setattr(cb, "value", False) for cb in selected.values()]).props("flat dense rounded").style("color:#f38ba8;")

                    for cid, cname in sorted(cats.items(), key=lambda x: x[1]):
                        cnt = counts.get(cid, 0)
                        with ui.row().classes("items-center").style("gap:6px;"):
                            cb = ui.checkbox("", value=True)
                            ui.label(cname).style("color:#cdd6f4;flex:1;font-size:0.9rem;")
                            ui.label(str(cnt)).style("color:#6c7086;font-size:0.78rem;")
                            selected[cid] = cb

                    ui.separator().style("background:#313244;margin:8px 0;")

                    def _start():
                        chosen = [cid for cid, cb in selected.items() if cb.value]
                        if not chosen:
                            ui.notify("Seleziona almeno una categoria", type="warning")
                            return
                        dialog.close()
                        asyncio.ensure_future(self._do_scan_cats(pd, pd["provider_type"], chosen, cats))
                        ui.notify(f"Scansione avviata: {len(chosen)} categorie", type="positive")

                    ui.button("Scansiona Selezionate", color="positive", on_click=_start).props("rounded")

            except Exception as e:
                status_lbl.set_text(f"Errore: {e}")
                logger.error(e)

        with ui.dialog() as dialog, ui.card().style(f"{CARD}min-width:300px;max-width:660px;width:90vw;padding:18px;"):
            with ui.row().classes("items-center justify-between w-full q-mb-xs"):
                ui.label(pd["name"]).style("color:#cdd6f4;font-weight:700;")
                ui.button(icon="close", on_click=dialog.close).props("flat round dense").style("color:#6c7086;")
            status_lbl = ui.label("...").style("color:#6c7086;font-size:0.82rem;")
            cat_col = ui.column().classes("w-full").style("max-height:55vh;overflow-y:auto;gap:2px;")

        asyncio.ensure_future(_load())

    async def _do_scan_cats(self, pd, ptype, cat_ids, cats):
        pid = pd["id"]
        try:
            if ptype == "vod":
                async with XtreamAPI(pd["host"], pd["username"], pd["password"]) as api:
                    await api.get_categories()
                    for cid in cat_ids:
                        cname   = cats.get(cid, cid)
                        streams = await api.get_vod_streams(category_id=cid)
                        with get_session() as db:
                            prov = db.get(Provider, pid)
                            skipped_m3u = 0
                            for s in streams:
                                if self._is_skipped_by_m3u(s.name, provider_id=pid):
                                    skipped_m3u += 1
                                    continue
                                if not db.exec(select(MediaItem).where(
                                    MediaItem.provider_id == pid,
                                    MediaItem.stream_id == str(s.stream_id)
                                )).first():
                                    db.add(MediaItem(
                                        provider_id=pid, title=s.name,
                                        item_type=ItemType.MOVIE,
                                        url=f"{prov.host}/movie/{prov.username}/{prov.password}/{s.stream_id}.{s.extension}",
                                        icon_url=s.clean_icon, group_title=cname,
                                        stream_id=str(s.stream_id),
                                    ))
                            db.commit()
                            if skipped_m3u:
                                logger.info(f"  M3U skip: {skipped_m3u} film gia presenti")
            else:
                async with XtreamSeriesAPI(pd["host"], pd["username"], pd["password"]) as api:
                    await api.get_categories()
                    for cid in cat_ids:
                        cname = cats.get(str(cid), str(cid))
                        series_in_cat = await api.get_series_list(category_id=str(cid))
                        logger.info(f"Cat {cid} ({cname}): {len(series_in_cat)} serie, recupero episodi...")
                        for series in series_in_cat:
                            det = await api.get_series_info(series.series_id)
                            if not det or not det.seasons:
                                continue
                            added = 0
                            skipped_m3u = 0
                            # Check whole series title against M3U
                            series_skipped = self._is_skipped_by_m3u(det.name, provider_id=pid)
                            if series_skipped:
                                logger.info(f"  M3U skip serie: {det.name}")
                            else:
                                with get_session() as db:
                                    prov = db.get(Provider, pid)
                                    for season in det.seasons:
                                        for ep in season.episodes:
                                            ep_title = f"{det.name} - S{season.season_num:02d}E{ep.episode_num:02d}"
                                            if self._is_skipped_by_m3u(ep_title, provider_id=pid):
                                                skipped_m3u += 1
                                                continue
                                            if not db.exec(select(MediaItem).where(
                                                MediaItem.provider_id == pid,
                                                MediaItem.stream_id == str(ep.id)
                                            )).first():
                                                db.add(MediaItem(
                                                    provider_id=pid,
                                                    title=ep_title,
                                                    original_title=ep.title,
                                                    item_type=ItemType.EPISODE,
                                                    series_name=det.name,
                                                    season_num=season.season_num,
                                                    episode_num=ep.episode_num,
                                                    url=f"{prov.host}/series/{prov.username}/{prov.password}/{ep.id}.{ep.container_extension}",
                                                    icon_url=det.clean_icon, group_title=cname,
                                                    stream_id=str(ep.id),
                                                ))
                                                added += 1
                                    db.commit()
                                if skipped_m3u:
                                    logger.info(f"  M3U skip: {skipped_m3u} ep")
                            logger.info(f"  {det.name}: {added} ep aggiunti")

            _notify("Scansione completata!", "positive")
            self._refresh_media_table(ItemType.MOVIE)
            self._refresh_media_table(ItemType.EPISODE)
            self._refresh_stats()
        except Exception as e:
            _notify(f"Errore scansione: {e}", "negative")
            logger.error(e)

    # -----------------------------------------------------------------------
    # Search dialog (single content search + episode picker)
    # -----------------------------------------------------------------------

    def _show_search(self, pd: dict):
        async def _do_search():
            query = search_inp.value.strip()
            if not query:
                return
            results_col.clear()
            search_status.set_text("Ricerca in corso...")
            try:
                ptype = pd["provider_type"]
                if ptype == "vod":
                    async with XtreamAPI(pd["host"], pd["username"], pd["password"]) as api:
                        await api.get_categories()
                        streams = await api.get_vod_streams()
                    ql = query.lower()
                    found = [s for s in streams if ql in s.name.lower()]
                    search_status.set_text(f"Trovati {len(found)} film")
                    with results_col:
                        for s in found[:100]:
                            with ui.row().classes("items-center w-full").style("gap:8px;padding:4px 0;border-bottom:1px solid #313244;"):
                                ui.label(s.name).style("color:#cdd6f4;flex:1;font-size:0.88rem;")
                                ui.label(s.category_id).style("color:#6c7086;font-size:0.75rem;")
                                ui.button("+ Aggiungi", on_click=lambda s=s: _add_vod(s)).props("flat dense rounded").style("color:#a6e3a1;")
                else:
                    async with XtreamSeriesAPI(pd["host"], pd["username"], pd["password"]) as api:
                        await api.get_categories()
                        series_list = await api.get_series_list()
                    ql = query.lower()
                    found = [s for s in series_list if ql in s.name.lower()]
                    search_status.set_text(f"Trovate {len(found)} serie")
                    with results_col:
                        for s in found[:50]:
                            with ui.row().classes("items-center w-full").style("gap:8px;padding:4px 0;border-bottom:1px solid #313244;"):
                                ui.label(s.name).style("color:#cdd6f4;flex:1;font-size:0.88rem;")
                                ui.button("Scegli episodi", on_click=lambda s=s: _pick_episodes(s)).props("flat dense rounded").style("color:#89b4fa;")
                                ui.button("+ Tutta la serie", on_click=lambda s=s: asyncio.ensure_future(_add_series_all(s))).props("flat dense rounded").style("color:#a6e3a1;")
            except Exception as e:
                search_status.set_text(f"Errore: {e}")
                logger.error(e)

        def _add_vod(stream):
            pid = pd["id"]
            with get_session() as db:
                prov = db.get(Provider, pid)
                if not prov:
                    return
                if db.exec(select(MediaItem).where(
                    MediaItem.provider_id == pid,
                    MediaItem.stream_id == str(stream.stream_id)
                )).first():
                    ui.notify(f"'{stream.name}' gia in lista", type="info")
                    return
                db.add(MediaItem(
                    provider_id=pid, title=stream.name,
                    item_type=ItemType.MOVIE,
                    url=f"{prov.host}/movie/{prov.username}/{prov.password}/{stream.stream_id}.{stream.extension}",
                    icon_url=stream.clean_icon,
                    group_title=str(stream.category_id),
                    stream_id=str(stream.stream_id),
                ))
                db.commit()
            ui.notify(f"Aggiunto: {stream.name}", type="positive")
            self._refresh_stats()

        async def _add_series_all(series):
            pid = pd["id"]
            search_status.set_text(f"Recupero episodi: {series.name}...")
            try:
                async with XtreamSeriesAPI(pd["host"], pd["username"], pd["password"]) as api:
                    await api.get_categories()
                    det = await api.get_series_info(series.series_id)
                if not det or not det.seasons:
                    ui.notify("Nessun episodio trovato", type="warning")
                    return
                count = 0
                with get_session() as db:
                    prov = db.get(Provider, pid)
                    for season in det.seasons:
                        for ep in season.episodes:
                            if not db.exec(select(MediaItem).where(
                                MediaItem.provider_id == pid,
                                MediaItem.stream_id == str(ep.id)
                            )).first():
                                db.add(MediaItem(
                                    provider_id=pid,
                                    title=f"{det.name} - S{season.season_num:02d}E{ep.episode_num:02d}",
                                    original_title=ep.title,
                                    item_type=ItemType.EPISODE,
                                    series_name=det.name,
                                    season_num=season.season_num,
                                    episode_num=ep.episode_num,
                                    url=f"{prov.host}/series/{prov.username}/{prov.password}/{ep.id}.{ep.container_extension}",
                                    icon_url=det.clean_icon,
                                    group_title=det.category_id,
                                    stream_id=str(ep.id),
                                ))
                                count += 1
                    db.commit()
                ui.notify(f"Aggiunti {count} episodi di {det.name}", type="positive")
                self._refresh_stats()
                search_status.set_text("Fatto.")
            except Exception as e:
                ui.notify(f"Errore: {e}", type="negative")
                logger.error(e)

        # _pick_episodes: dialog built synchronously in UI context,
        # episode loading happens in async task that updates the pre-built container.
        def _pick_episodes(series):
            ep_selected = {}

            # Build dialog immediately in UI context (no async here)
            with ui.dialog() as ep_dlg, ui.card().style(
                f"{CARD}min-width:320px;max-width:680px;width:92vw;padding:18px;"
            ):
                with ui.row().classes("items-center justify-between w-full q-mb-sm"):
                    ui.label(series.name).style("color:#cdd6f4;font-weight:700;")
                    ui.button(icon="close", on_click=ep_dlg.close).props("flat round dense").style("color:#6c7086;")

                ep_status = ui.label("Caricamento episodi...").style("color:#6c7086;font-size:0.82rem;")
                ep_scroll  = ui.column().classes("w-full").style("max-height:55vh;overflow-y:auto;gap:2px;")
                btn_row    = ui.row().style("gap:8px;margin-top:10px;flex-wrap:wrap;")

                def _add_selected():
                    pid   = pd["id"]
                    count = 0
                    with get_session() as db:
                        prov = db.get(Provider, pid)
                        for key, (cb, season, ep, det_ref) in ep_selected.items():
                            if not cb.value:
                                continue
                            if db.exec(select(MediaItem).where(
                                MediaItem.provider_id == pid,
                                MediaItem.stream_id == str(ep.id)
                            )).first():
                                continue
                            db.add(MediaItem(
                                provider_id=pid,
                                title=f"{det_ref.name} - S{season.season_num:02d}E{ep.episode_num:02d}",
                                original_title=ep.title,
                                item_type=ItemType.EPISODE,
                                series_name=det_ref.name,
                                season_num=season.season_num,
                                episode_num=ep.episode_num,
                                url=f"{prov.host}/series/{prov.username}/{prov.password}/{ep.id}.{ep.container_extension}",
                                icon_url=det_ref.clean_icon,
                                group_title=det_ref.category_id,
                                stream_id=str(ep.id),
                            ))
                            count += 1
                        db.commit()
                    ep_dlg.close()
                    ui.notify(f"Aggiunti {count} episodi", type="positive")
                    self._refresh_stats()

            ep_dlg.open()

            # Now load episodes asynchronously and populate the pre-built container
            async def _load_episodes():
                try:
                    search_status.set_text(f"Recupero episodi: {series.name}...")
                    async with XtreamSeriesAPI(pd["host"], pd["username"], pd["password"]) as api:
                        det = await api.get_series_info(series.series_id)

                    if not det or not det.seasons:
                        ep_status.set_text("Nessun episodio trovato.")
                        search_status.set_text("Nessun episodio.")
                        return

                    ep_scroll.clear()
                    with ep_scroll:
                        for season in det.seasons:
                            ui.label(f"Stagione {season.season_num}").style(
                                "color:#89b4fa;font-weight:600;margin-top:8px;"
                            )
                            for ep in season.episodes:
                                key = str(ep.id)
                                with ui.row().classes("items-center").style("gap:6px;"):
                                    cb = ui.checkbox("", value=False)
                                    ui.label(ep.display_title).style(
                                        "color:#cdd6f4;flex:1;font-size:0.85rem;"
                                    )
                                    ep_selected[key] = (cb, season, ep, det)

                    btn_row.clear()
                    with btn_row:
                        ui.button("Tutto",   on_click=lambda: [setattr(v[0], "value", True)  for v in ep_selected.values()]).props("flat dense rounded").style("color:#89b4fa;")
                        ui.button("Nessuno", on_click=lambda: [setattr(v[0], "value", False) for v in ep_selected.values()]).props("flat dense rounded").style("color:#f38ba8;")
                        ui.button("Aggiungi Selezionati", color="positive", on_click=_add_selected).props("rounded")

                    total_ep = sum(len(s.episodes) for s in det.seasons)
                    ep_status.set_text(f"{len(det.seasons)} stagioni  |  {total_ep} episodi")
                    search_status.set_text("Pronto.")
                except Exception as e:
                    ep_status.set_text(f"Errore: {e}")
                    logger.error(e)

            asyncio.ensure_future(_load_episodes())

        with ui.dialog() as search_dlg, ui.card().style(
            f"{CARD}min-width:320px;max-width:700px;width:92vw;padding:18px;"
        ):
            with ui.row().classes("items-center justify-between w-full q-mb-sm"):
                ui.label(f"Cerca in {pd['name']}").style("color:#cdd6f4;font-weight:700;")
                ui.button(icon="close", on_click=search_dlg.close).props("flat round dense").style("color:#6c7086;")
            with ui.row().classes("w-full").style("gap:8px;"):
                search_inp = ui.input("Titolo...").props("dark").style("flex:1;")
                ui.button("Cerca", color="primary", on_click=lambda: asyncio.ensure_future(_do_search())).props("rounded")
            search_status = ui.label("").style("color:#6c7086;font-size:0.82rem;")
            results_col = ui.column().classes("w-full").style("max-height:55vh;overflow-y:auto;gap:2px;margin-top:6px;")

        search_dlg.open()

    # -----------------------------------------------------------------------
    # Downloads tab
    # -----------------------------------------------------------------------

    def _build_downloads_tab(self):
        with ui.column().classes("w-full q-pa-md").style("gap:10px;"):
            ui.label("Download in corso / recenti").style("color:#cdd6f4;font-weight:600;font-size:1rem;")

            with ui.row().style("flex-wrap:wrap;gap:8px;align-items:flex-end;"):
                self.dl_filter_provider = ui.select(
                    options=["Tutti"], value="Tutti", label="Provider"
                ).props("dark").style("min-width:140px;")
                self.dl_filter_status = ui.select(
                    options=["Tutti", "pending", "downloading", "completed", "error", "skipped_size"],
                    value="downloading", label="Stato"
                ).props("dark").style("min-width:140px;")
                ui.button("Aggiorna", on_click=self._refresh_downloads).props("flat rounded dense").style("color:#89b4fa;")
                ui.button("Del Pending",      on_click=self._del_pending_all).props("flat rounded dense").style("color:#df8e1d;")
                ui.button("Del Errori",       on_click=self._del_errors_all).props("flat rounded dense").style("color:#d20f39;")
                ui.button("Ripristina Errori",   on_click=self._reset_errors_to_pending).props("flat rounded dense").style("color:#cba6f7;")
                ui.button("Resume Bloccati",     on_click=self._resume_stuck).props("flat rounded dense").style("color:#a6e3a1;")
                ui.button("M3U Comparazione",    on_click=self._show_m3u_dialog).props("flat rounded dense").style("color:#89dceb;")

            with ui.row().style("gap:8px;margin-bottom:4px;"):
                ui.label("Seleziona righe e premi Elimina Selezionati").style("color:#6c7086;font-size:0.78rem;")
                ui.button("Elimina Selezionati", color="negative",
                          on_click=lambda: self._delete_selected(self.dl_table)).props("flat dense rounded")

            self.dl_table = ui.table(
                columns=[
                    {"name": "title",    "label": "Titolo",    "field": "title",    "align": "left",  "sortable": True},
                    {"name": "provider", "label": "Provider",  "field": "provider", "align": "left"},
                    {"name": "group",    "label": "Categoria", "field": "group",    "align": "left"},
                    {"name": "size",     "label": "Dim.",      "field": "size",     "align": "left"},
                    {"name": "speed",    "label": "MB/s",      "field": "speed",    "align": "left"},
                    {"name": "progress", "label": "%",         "field": "progress", "align": "left"},
                    {"name": "status",   "label": "Stato",     "field": "status",   "align": "left",  "sortable": True},
                ],
                rows=[], row_key="id", selection="multiple",
            ).classes("w-full").props("dark")

            self._update_dl_provider_filter()
            self._refresh_downloads()

    def _del_pending_all(self):
        pname = self.dl_filter_provider.value if self.dl_filter_provider else "Tutti"
        with get_session() as db:
            q = select(MediaItem).where(MediaItem.status == "pending")
            if pname != "Tutti":
                prov = db.exec(select(Provider).where(Provider.name == pname)).first()
                if prov:
                    q = q.where(MediaItem.provider_id == prov.id)
            items = db.exec(q).all()
            count = len(items)
            for i in items:
                db.delete(i)
            db.commit()
        ui.notify(f"Rimossi {count} item pending", type="positive")
        self._refresh_downloads()
        self._refresh_stats()

    def _del_errors_all(self):
        pname = self.dl_filter_provider.value if self.dl_filter_provider else "Tutti"
        with get_session() as db:
            q = select(MediaItem).where(MediaItem.status.in_(["error", "skipped_size"]))
            if pname != "Tutti":
                prov = db.exec(select(Provider).where(Provider.name == pname)).first()
                if prov:
                    q = q.where(MediaItem.provider_id == prov.id)
            items = db.exec(q).all()
            count = len(items)
            for i in items:
                db.delete(i)
            db.commit()
        ui.notify(f"Rimossi {count} item (errori/saltati)", type="positive")
        self._refresh_downloads()
        self._refresh_stats()

    def _reset_errors_to_pending(self):
        """Reset error/skipped items back to pending so they get retried."""
        pname = self.dl_filter_provider.value if self.dl_filter_provider else "Tutti"
        with get_session() as db:
            q = select(MediaItem).where(MediaItem.status.in_(["error", "skipped_size"]))
            if pname != "Tutti":
                prov = db.exec(select(Provider).where(Provider.name == pname)).first()
                if prov:
                    q = q.where(MediaItem.provider_id == prov.id)
            items = db.exec(q).all()
            count = len(items)
            for i in items:
                i.status        = "pending"
                i.error_message = None
                i.skip_reason   = None
                i.progress_percent   = 0.0
                i.size_downloaded_mb = 0.0
                i.speed_mbps         = 0.0
            db.commit()
        ui.notify(f"Ripristinati {count} item -> pending", type="positive")
        self._refresh_downloads()
        self._refresh_stats()

    def _resume_stuck(self):
        """Reset items stuck at 'downloading' (app crash) back to pending.
        The download_manager will resume them using HTTP Range on the partial file."""
        pname = self.dl_filter_provider.value if self.dl_filter_provider else "Tutti"
        with get_session() as db:
            q = select(MediaItem).where(MediaItem.status == "downloading")
            if pname != "Tutti":
                prov = db.exec(select(Provider).where(Provider.name == pname)).first()
                if prov:
                    q = q.where(MediaItem.provider_id == prov.id)
            items = db.exec(q).all()
            count = len(items)
            for i in items:
                i.status     = "pending"
                i.speed_mbps = 0.0
            db.commit()
        if count:
            ui.notify(
                f"{count} download ripristinati -> pending (riprenderanno dall'ultimo punto)",
                type="positive"
            )
        else:
            ui.notify("Nessun download bloccato trovato", type="info")
        self._refresh_downloads()
        self._refresh_stats()

    def _show_m3u_dialog(self):
        """Load an M3U file path and build the comparison set."""
        with ui.dialog() as dlg, ui.card().style(
            f"{CARD}padding:18px;min-width:320px;max-width:580px;width:90vw;"
        ):
            with ui.row().classes("items-center justify-between w-full q-mb-sm"):
                ui.label("M3U Comparazione").style("color:#cdd6f4;font-weight:700;")
                ui.button(icon="close", on_click=dlg.close).props("flat round dense").style("color:#6c7086;")

            ui.label(
                "Inserisci il percorso assoluto di un file .m3u sul server. "
                "I titoli presenti nel file verranno saltati durante le scansioni."
            ).style("color:#a6adc8;font-size:0.85rem;margin-bottom:8px;")

            path_inp = ui.input(
                "Percorso file M3U", placeholder="/home/user/lista.m3u"
            ).props("dark").classes("w-full")

            # show current status
            count_lbl = ui.label(
                f"Titoli caricati: {len(self._m3u_titles)}"
            ).style("color:#6c7086;font-size:0.82rem;margin-top:4px;")

            def _load():
                path = path_inp.value.strip()
                if not path:
                    ui.notify("Inserisci un percorso", type="warning")
                    return
                import os
                if not os.path.isfile(path):
                    ui.notify(f"File non trovato: {path}", type="negative")
                    return
                self._m3u_titles = _parse_m3u_titles(path)
                count_lbl.set_text(f"Titoli caricati: {len(self._m3u_titles)}")
                ui.notify(f"M3U caricato: {len(self._m3u_titles)} titoli", type="positive")

            def _clear():
                self._m3u_titles = set()
                count_lbl.set_text("Titoli caricati: 0")
                ui.notify("Lista comparazione azzerata", type="info")

            with ui.row().style("gap:8px;margin-top:10px;"):
                ui.button("Carica", color="primary", on_click=_load).props("rounded")
                ui.button("Azzera", on_click=_clear).props("flat rounded").style("color:#f38ba8;")
                ui.button("Chiudi", on_click=dlg.close).props("flat rounded")

        dlg.open()

    def _is_skipped_by_m3u(self, title: str, provider_id: int = 0) -> bool:
        """Return True if title matches the M3U index for this provider."""
        store = _M3U_STORE.get(provider_id, {})
        idx = store.get("titles")
        if not idx or len(idx) == 0:
            return False
        return idx.match(title)

    def _update_dl_provider_filter(self):
        if not self.dl_filter_provider:
            return
        with get_session() as db:
            names = ["Tutti"] + [p.name for p in db.exec(select(Provider)).all()]
        self.dl_filter_provider.options = names

    def _refresh_downloads(self):
        if not self.dl_table:
            return
        pname  = self.dl_filter_provider.value if self.dl_filter_provider else "Tutti"
        status = self.dl_filter_status.value   if self.dl_filter_status   else "Tutti"
        rows = []
        with get_session() as db:
            q = select(MediaItem, Provider).join(Provider)
            if status != "Tutti":
                q = q.where(MediaItem.status == status)
            if pname != "Tutti":
                q = q.where(Provider.name == pname)
            # Limit to 100 rows; enough for monitoring, avoids loading 10k rows
            q = q.order_by(MediaItem.updated_at.desc()).limit(100)
            for item, prov in db.exec(q).all():
                mb = item.size_total_mb
                sz = f"{mb/1024:.1f}GB" if mb > 1024 else f"{mb:.0f}MB"
                rows.append({
                    "id":       item.id,
                    "title":    item.title,
                    "provider": prov.name,
                    "group":    item.group_title or "",
                    "size":     sz,
                    "speed":    f"{item.speed_mbps:.1f}" if item.status == "downloading" else "",
                    "progress": f"{item.progress_percent:.1f}%" if item.progress_percent > 0 else "",
                    "status":   item.status,
                })
        self.dl_table.rows = rows

    # -----------------------------------------------------------------------
    # Media list with pagination + provider filter
    # -----------------------------------------------------------------------

    PAGE_SIZE = 50

    def _build_media_list(self, item_type: ItemType):
        is_movie = item_type == ItemType.MOVIE
        lbl      = "Film" if is_movie else "Serie TV"
        attr_fp  = "film_filter_prov"  if is_movie else "series_filter_prov"
        attr_pg  = "film_page"         if is_movie else "series_page"
        attr_lbl = "film_page_lbl"     if is_movie else "series_page_lbl"

        with ui.column().classes("w-full q-pa-md").style("gap:10px;"):
            with ui.row().style("flex-wrap:wrap;gap:8px;align-items:center;"):
                ui.label(lbl).style("color:#cdd6f4;font-size:1rem;font-weight:600;")

                fp = ui.select(options=["Tutti"], value="Tutti", label="Provider").props("dark").style("min-width:130px;")
                setattr(self, attr_fp, fp)

                # populate provider list
                with get_session() as db:
                    pnames = ["Tutti"] + [p.name for p in db.exec(select(Provider)).all()]
                fp.options = pnames
                fp.on("update:model-value", lambda _, it=item_type, ap=attr_pg: (setattr(self, ap, 0), self._refresh_media_table(it)))

                ui.button("Aggiorna",    on_click=lambda: self._refresh_media_table(item_type)).props("flat round dense").style("color:#89b4fa;")
                ui.button("Del Errori",  on_click=lambda: self._del_status(item_type, "error")).props("flat rounded dense").style("color:#d20f39;")
                ui.button("Del Saltati", on_click=lambda: self._del_status(item_type, "skipped_size")).props("flat rounded dense").style("color:#df8e1d;")
                ui.button("Del Pending", on_click=lambda: self._del_status(item_type, "pending")).props("flat rounded dense").style("color:#df8e1d;")

            # pagination controls
            with ui.row().classes("items-center").style("gap:8px;"):
                ui.button("< Prec", on_click=lambda: self._page_media(item_type, -1)).props("flat dense rounded").style("color:#89b4fa;")
                page_lbl = ui.label("Pagina 1").style("color:#a6adc8;font-size:0.85rem;")
                setattr(self, attr_lbl, page_lbl)
                ui.button("Succ >", on_click=lambda: self._page_media(item_type, +1)).props("flat dense rounded").style("color:#89b4fa;")

            with ui.row().style("gap:8px;margin-bottom:4px;"):
                ui.label("Seleziona righe e premi Elimina Selezionati").style("color:#6c7086;font-size:0.78rem;")
                # closure trick: capture table ref after creation via list
                table_ref = []
                ui.button("Elimina Selezionati", color="negative",
                          on_click=lambda: self._delete_selected(table_ref[0]) if table_ref else None
                          ).props("flat dense rounded")

            table = ui.table(
                columns=[
                    {"name": "title",    "label": "Titolo",    "field": "title",    "align": "left", "sortable": True},
                    {"name": "provider", "label": "Provider",  "field": "provider", "align": "left"},
                    {"name": "group",    "label": "Categoria", "field": "group",    "align": "left", "sortable": True},
                    {"name": "size",     "label": "Dim.",      "field": "size",     "align": "left"},
                    {"name": "status",   "label": "Stato",     "field": "status",   "align": "left", "sortable": True},
                    {"name": "progress", "label": "%",         "field": "progress", "align": "left"},
                ],
                rows=[], row_key="id", selection="multiple",
            ).classes("w-full").props("dark")
            table_ref.append(table)

            self.tables[item_type] = table
            self._refresh_media_table(item_type)

    def _page_media(self, item_type: ItemType, delta: int):
        attr = "film_page" if item_type == ItemType.MOVIE else "series_page"
        current = getattr(self, attr)
        new_page = max(0, current + delta)
        setattr(self, attr, new_page)
        self._refresh_media_table(item_type)

    def _refresh_media_table(self, item_type: ItemType):
        if item_type not in self.tables:
            return
        is_movie = item_type == ItemType.MOVIE
        attr_fp  = "film_filter_prov"  if is_movie else "series_filter_prov"
        attr_pg  = "film_page"         if is_movie else "series_page"
        attr_lbl = "film_page_lbl"     if is_movie else "series_page_lbl"

        fp       = getattr(self, attr_fp)
        page     = getattr(self, attr_pg)
        page_lbl = getattr(self, attr_lbl)
        pname    = fp.value if fp else "Tutti"

        from sqlalchemy import func, text as sa_text
        from database import engine

        # COUNT with DB-level query -- no load-all
        with engine.connect() as conn:
            if pname == "Tutti":
                total_count = conn.execute(sa_text(
                    "SELECT COUNT(*) FROM media_items WHERE item_type = :t"
                ), {"t": item_type.value}).scalar() or 0
            else:
                total_count = conn.execute(sa_text(
                    "SELECT COUNT(*) FROM media_items mi "
                    "JOIN providers p ON p.id = mi.provider_id "
                    "WHERE mi.item_type = :t AND p.name = :n"
                ), {"t": item_type.value, "n": pname}).scalar() or 0

        total_pages = max(1, (total_count + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        page = min(max(page, 0), total_pages - 1)
        setattr(self, attr_pg, page)

        if page_lbl:
            page_lbl.set_text(f"Pagina {page+1} / {total_pages}  ({total_count} totali)")

        # Fetch only current page from DB
        rows = []
        with get_session() as db:
            q = select(MediaItem, Provider).join(Provider).where(MediaItem.item_type == item_type)
            if pname != "Tutti":
                q = q.where(Provider.name == pname)
            q = q.order_by(MediaItem.title).offset(page * self.PAGE_SIZE).limit(self.PAGE_SIZE)
            for item, prov in db.exec(q).all():
                mb = item.size_total_mb
                sz = f"{mb/1024:.1f}GB" if mb > 1024 else f"{mb:.0f}MB"
                if item.status == "skipped_size":
                    sz += f" (>{prov.max_size_gb}GB)"
                rows.append({
                    "id":       item.id,
                    "title":    item.title,
                    "provider": prov.name,
                    "group":    item.group_title or "",
                    "size":     sz,
                    "status":   item.status,
                    "progress": f"{item.progress_percent:.1f}%" if item.progress_percent > 0 else "",
                })
        self.tables[item_type].rows = rows

    def _delete_selected(self, table):
        """Delete rows currently selected in any table."""
        selected = getattr(table, "selected", [])
        if not selected:
            ui.notify("Nessuna riga selezionata", type="warning")
            return
        ids = [row.get("id") for row in selected if row.get("id")]
        if not ids:
            return
        with get_session() as db:
            for item_id in ids:
                item = db.get(MediaItem, item_id)
                if item:
                    db.delete(item)
            db.commit()
        ui.notify(f"Eliminati {len(ids)} elementi", type="positive")
        # Clear selection and refresh all tables
        table.selected = []
        self._refresh_downloads()
        self._refresh_media_table(ItemType.MOVIE)
        self._refresh_media_table(ItemType.EPISODE)
        self._refresh_stats()

    def _del_status(self, item_type: ItemType, status: str):
        is_movie = item_type == ItemType.MOVIE
        attr_fp  = "film_filter_prov" if is_movie else "series_filter_prov"
        fp       = getattr(self, attr_fp)
        pname    = fp.value if fp else "Tutti"

        with get_session() as db:
            q = select(MediaItem).where(MediaItem.item_type == item_type, MediaItem.status == status)
            if pname != "Tutti":
                prov = db.exec(select(Provider).where(Provider.name == pname)).first()
                if prov:
                    q = q.where(MediaItem.provider_id == prov.id)
            items = db.exec(q).all()
            count = len(items)
            for i in items:
                db.delete(i)
            db.commit()
        ui.notify(f"Eliminati {count} [{status}]", type="positive")
        self._refresh_media_table(item_type)
        self._refresh_stats()

    # -----------------------------------------------------------------------
    # Timer tick
    # -----------------------------------------------------------------------

    def _tick(self):
        self._tick_count = getattr(self, "_tick_count", 0) + 1
        self._refresh_stats()
        down, up = get_net_speed()
        if self.net_down_label:
            self.net_down_label.set_text(f"DN: {down:.1f} MB/s")
        if self.net_up_label:
            self.net_up_label.set_text(f"UP: {up:.1f} MB/s")
        # Disk: every 10 ticks (~30s) to avoid syscall spam
        if self._tick_count % 10 == 0:
            self._refresh_disk()
        # Downloads tab: every tick only if there are active downloads
        from sqlalchemy import text as sa_text
        from database import engine
        with engine.connect() as conn:
            active_count = conn.execute(
                sa_text("SELECT COUNT(*) FROM media_items WHERE status='downloading'")
            ).scalar() or 0
        if active_count > 0:
            self._refresh_downloads()
        # Media tables: only every 5 ticks (~15s), not on every tick
        if self._tick_count % 5 == 0:
            for it in [ItemType.MOVIE, ItemType.EPISODE]:
                if it in self.tables:
                    self._refresh_media_table(it)

    # -----------------------------------------------------------------------
    # Provider actions
    # -----------------------------------------------------------------------

    def _scan_provider(self, provider_id: int):
        async def _do():
            with get_session() as db:
                prov = db.get(Provider, provider_id)
                if not prov:
                    return
                ptype = prov.provider_type
                pname = prov.name
            _notify(f"Scansione: {pname}...", "info")
            try:
                with get_session() as db:
                    p = db.get(Provider, provider_id)
                    if ptype == ProviderType.VOD:
                        await ProviderScanner.scan_vod_provider(p)
                    else:
                        await ProviderScanner.scan_series_provider(p)
                _notify(f"Completato: {pname}", "positive")
                self._refresh_media_table(ItemType.MOVIE)
                self._refresh_media_table(ItemType.EPISODE)
                self._refresh_stats()
            except Exception as e:
                _notify(f"Errore: {e}", "negative")
        asyncio.ensure_future(_do())

    def _start_provider(self, pid: int):
        """Avvia download per un singolo provider (safe wrapper)."""
        async def _do():
            await download_manager.add_provider(pid)
        asyncio.ensure_future(_do())
        ui.notify("Provider avviato")

    def _stop_provider(self, pid: int):
        download_manager.stop_provider(pid)
        ui.notify("Provider fermato")

    def _show_m3u_provider(self, pd: dict):
        """Dialog M3U comparazione legata al provider -- persiste in _M3U_STORE."""
        pid = pd["id"]
        current = _M3U_STORE.get(pid, {})

        with ui.dialog() as dlg, ui.card().style(
            f"{CARD}padding:18px;min-width:320px;max-width:560px;width:90vw;"
        ):
            with ui.row().classes("items-center justify-between w-full q-mb-sm"):
                ui.label(f"M3U comparazione: {pd['name']}").style("color:#cdd6f4;font-weight:700;")
                ui.button(icon="close", on_click=dlg.close).props("flat round dense").style("color:#6c7086;")

            ui.label(
                "I titoli nel file M3U verranno esclusi durante le scansioni di questo provider."
            ).style("color:#a6adc8;font-size:0.85rem;margin-bottom:8px;")

            path_inp  = ui.input(
                "Percorso file M3U", value=current.get("path", ""),
                placeholder="/home/user/lista.m3u"
            ).props("dark").classes("w-full")

            count_lbl = ui.label(
                f"Titoli caricati: {len(current.get('titles', set()))}"
            ).style("color:#6c7086;font-size:0.82rem;margin-top:4px;")

            def _load():
                import os
                path = path_inp.value.strip()
                if not path:
                    ui.notify("Inserisci un percorso", type="warning")
                    return
                if not os.path.isfile(path):
                    ui.notify(f"File non trovato: {path}", type="negative")
                    return
                titles = _parse_m3u_titles(path)
                _M3U_STORE[pid] = {"path": path, "titles": titles}
                count_lbl.set_text(f"Titoli caricati: {len(titles)}")
                ui.notify(f"M3U caricato: {len(titles)} titoli, Jaccard>={int(M3U_THRESHOLD*100)}% per {pd['name']}", type="positive")
                self._refresh_providers()

            def _clear():
                _M3U_STORE.pop(pid, None)
                count_lbl.set_text("Titoli caricati: 0")
                path_inp.set_value("")
                ui.notify("Lista comparazione azzerata", type="info")
                self._refresh_providers()

            with ui.row().style("gap:8px;margin-top:10px;"):
                ui.button("Carica", color="primary", on_click=_load).props("rounded")
                ui.button("Azzera", on_click=_clear).props("flat rounded").style("color:#f38ba8;")
                ui.button("Chiudi", on_click=dlg.close).props("flat rounded")

        dlg.open()

    def _show_edit(self, pd: dict):
        """Dialog per modificare un provider esistente."""
        with ui.dialog() as dlg, ui.card().style(f"{CARD}padding:18px;min-width:320px;max-width:600px;width:90vw;"):
            with ui.row().classes("items-center justify-between w-full q-mb-sm"):
                ui.label(f"Modifica: {pd['name']}").style("color:#cdd6f4;font-weight:700;")
                ui.button(icon="close", on_click=dlg.close).props("flat round dense").style("color:#6c7086;")

            with ui.column().classes("w-full").style("gap:8px;"):
                with ui.row().style("flex-wrap:wrap;gap:8px;"):
                    name_inp = ui.input("Nome", value=pd["name"]).props("dark").style("flex:1;min-width:120px;")
                    host_inp = ui.input("Host", value=pd["host"]).props("dark").style("flex:2;min-width:180px;")
                with ui.row().style("flex-wrap:wrap;gap:8px;"):
                    user_inp = ui.input("Username", value=pd["username"]).props("dark").style("flex:1;min-width:120px;")
                    pass_inp = ui.input("Password", value=pd["password"], password=True).props("dark").style("flex:1;min-width:120px;")
                with ui.row().style("flex-wrap:wrap;gap:8px;align-items:flex-end;"):
                    type_sel  = ui.select(["vod", "series"], label="Tipo", value=pd["provider_type"]).props("dark").style("min-width:90px;")
                    size_inp  = ui.number("Max GB (0=inf)",   value=pd["max_size_gb"],    min=0, format="%.1f").props("dark").style("min-width:130px;flex:1;")
                    speed_inp = ui.number("Max MB/s (0=inf)", value=pd["max_speed_mbps"], min=0, format="%.1f").props("dark").style("min-width:140px;flex:1;")
                    conc_inp  = ui.number("Paralleli",        value=pd["max_concurrent"], min=1, max=10).props("dark").style("min-width:100px;flex:1;")
                with ui.row().style("flex-wrap:wrap;gap:8px;"):
                    ua_inp = ui.input("User-Agent", value=pd["user_agent"]).props("dark").style("flex:1;min-width:220px;")

                with ui.row().style("gap:8px;margin-top:4px;"):
                    def _save():
                        if not name_inp.value or not host_inp.value:
                            ui.notify("Nome e Host obbligatori", type="warning")
                            return
                        with get_session() as db:
                            p = db.get(Provider, pd["id"])
                            if not p:
                                return
                            p.name           = name_inp.value
                            p.host           = host_inp.value.rstrip("/")
                            p.username       = user_inp.value
                            p.password       = pass_inp.value
                            p.user_agent     = ua_inp.value.strip() or USER_AGENTS["Windows Chrome"]
                            p.provider_type  = ProviderType.VOD if type_sel.value == "vod" else ProviderType.SERIES
                            p.max_size_gb    = float(size_inp.value or 0)
                            p.max_speed_mbps = float(speed_inp.value or 0)
                            p.max_concurrent = int(conc_inp.value or 2)
                            db.commit()
                        dlg.close()
                        ui.notify("Provider aggiornato!", type="positive")
                        self._refresh_providers()

                    ui.button("Salva", color="primary", on_click=_save).props("rounded")
                    ui.button("Annulla", on_click=dlg.close).props("flat rounded")

        dlg.open()

    def _start_all(self):
        with get_session() as db:
            ids = [p.id for p in db.exec(select(Provider).where(Provider.enabled == True)).all()]
        for pid in ids:
            asyncio.ensure_future(download_manager.add_provider(pid))
        ui.notify(f"Avviati {len(ids)} provider")

    def _stop_all(self):
        download_manager.stop_all()
        ui.notify("Tutti fermati")

    def _clean_completed(self):
        with get_session() as db:
            items = db.exec(select(MediaItem).where(MediaItem.status == "completed")).all()
            count = len(items)
            for i in items:
                db.delete(i)
            db.commit()
        ui.notify(f"Rimossi {count} completati", type="positive")
        self._refresh_stats()
        for it in [ItemType.MOVIE, ItemType.EPISODE]:
            if it in self.tables:
                self._refresh_media_table(it)


# ---------------------------------------------------------------------------

def init_ui():
    ui.dark_mode()
    ui.add_head_html('<meta name="viewport" content="width=device-width, initial-scale=1.0">')
    ui.add_head_html("""<style>
body { background: #11111b !important; }
/* tables */
.q-table { background: #1e1e2e !important; color: #cdd6f4 !important; }
.q-table th { background: #181825 !important; color: #89b4fa !important; font-weight:600; }
.q-table td { color: #cdd6f4 !important; }
.q-table tr:hover td { background: #313244 !important; }
/* tabs */
.q-tab { color: #a6adc8 !important; }
.q-tab--active { color: #89b4fa !important; }
.q-tabs__content { background: #181825; }
/* inputs - force light text */
.q-field__native, .q-field__input, .q-field__label,
.q-select__dropdown-icon, .q-field__marginal { color: #cdd6f4 !important; }
.q-field__control { background: #313244 !important; }
.q-field--dark .q-field__control { background: #313244 !important; }
/* select dropdown */
.q-menu { background: #1e1e2e !important; color: #cdd6f4 !important; }
.q-item { color: #cdd6f4 !important; }
.q-item:hover { background: #313244 !important; }
/* cards */
.q-card { background: #1e1e2e !important; color: #cdd6f4 !important; }
/* dialog */
.q-dialog__inner > .q-card { background: #1e1e2e !important; }
/* checkbox */
.q-checkbox__label { color: #cdd6f4 !important; }
/* scrollbar */
::-webkit-scrollbar { width:6px; height:6px; }
::-webkit-scrollbar-track { background:#11111b; }
::-webkit-scrollbar-thumb { background:#45475a; border-radius:3px; }
</style>""")
    interface = MainInterface()
    interface.build()