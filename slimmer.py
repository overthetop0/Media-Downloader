#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
M3U Series Slimmer
==================
Dato un file M3U, genera una versione "alleggerita" che mantiene:
- TUTTI i canali live, film, sport, ecc. (intatti)
- Per le SERIE TV, solo il PRIMO episodio trovato per ogni serie

Uso:
    python3 m3u_slimmer.py input.m3u output.m3u
"""

import re
import sys
import os
from collections import OrderedDict

# ============================================================
# CONFIGURAZIONE
# ============================================================

# Parole chiave in group-title che indicano serie TV (case-insensitive)
SERIES_GROUP_KEYWORDS = [
    "serie tv", "series", "tv shows", "show", "serietv",
    "serie", "season", "stagione", "episodio", "episode",
    "série", "séries", "tv series", "vod series"
]

# Pattern regex per rilevare episodi nel titolo/canale
EPISODE_PATTERNS = [
    # S01E01, S1E1, S01 E01, S01-E01, S01_E01, S01.E01
    re.compile(r"[\s._\-\[\(]S(?P<season>\d{1,4})[\s._\-]*E(?P<episode>\d{1,4})[\s._\-\]\)]?", re.IGNORECASE),
    # 1x01, 10x12, 1 x 01
    re.compile(r"[\s._\-\[\(](?P<season>\d{1,4})[\s]*x[\s]*(?P<episode>\d{1,4})[\s._\-\]\)]?", re.IGNORECASE),
    # Season 1 Episode 1, Stagione 1 Episodio 1
    re.compile(r"[\s._\-\[\(](?:Season|Stagione|Série|Sezon|Serie)[\s._]*(?P<season>\d{1,4})[\s._\-]*(?:Episode|Episodio|Ep|Épisode|Bölüm)[\s._]*(?P<episode>\d{1,4})[\s._\-\]\)]?", re.IGNORECASE),
    # Ep.1, Ep 1, Episode 1
    re.compile(r"[\s._\-\[\(](?:Ep|Episode|Episodio|Épisode)[\.\s]*(?P<episode>\d{1,4})[\s._\-\]\)]?", re.IGNORECASE),
    # シーズン1 エピソード1
    re.compile(r"シーズン\s*(?P<season>\d{1,4})\s*エピソード\s*(?P<episode>\d{1,4})", re.IGNORECASE),
    # 시즌1 에피소드1
    re.compile(r"시즌\s*(?P<season>\d{1,4})\s*에피소드\s*(?P<episode>\d{1,4})", re.IGNORECASE),
]

# Pattern per rimuovere indicazione episodio dal titolo
CLEAN_SERIES_PATTERNS = [
    re.compile(r"[\s._\-\[\(]S\d{1,4}[\s._\-]*E\d{1,4}[\s._\-\]\)]?", re.IGNORECASE),
    re.compile(r"[\s._\-\[\(]\d{1,4}[\s]*x[\s]*\d{1,4}[\s._\-\]\)]?", re.IGNORECASE),
    re.compile(r"[\s._\-\[\(](?:Season|Stagione|Série|Sezon|Serie)[\s._]*\d{1,4}[\s._\-]*(?:Episode|Episodio|Ep|Épisode|Bölüm)[\s._]*\d{1,4}[\s._\-\]\)]?", re.IGNORECASE),
    re.compile(r"[\s._\-\[\(](?:Ep|Episode|Episodio|Épisode)[\.\s]*\d{1,4}[\s._\-\]\)]?", re.IGNORECASE),
    re.compile(r"シーズン\s*\d{1,4}\s*エピソード\s*\d{1,4}", re.IGNORECASE),
    re.compile(r"시즌\s*\d{1,4}\s*에피소드\s*\d{1,4}", re.IGNORECASE),
]

# ============================================================
# FUNZIONI
# ============================================================

def is_series_by_group(group_title):
    if not group_title:
        return False
    group_lower = group_title.lower()
    return any(kw.lower() in group_lower for kw in SERIES_GROUP_KEYWORDS)

def extract_episode_info(text):
    for pattern in EPISODE_PATTERNS:
        match = pattern.search(text)
        if match:
            season = None
            episode = None
            if "season" in match.groupdict() and match.group("season"):
                season = int(match.group("season"))
            if "episode" in match.groupdict() and match.group("episode"):
                episode = int(match.group("episode"))
            if episode is None and "episode" in match.groupdict():
                # potrebbe essere None se il gruppo non ha catturato
                pass
            return (season, episode)
    return None

def clean_series_name(title):
    name = title
    for pattern in CLEAN_SERIES_PATTERNS:
        name = pattern.sub("", name)
    name = re.sub(r"\s+", " ", name).strip()
    name = re.sub(r"[\s._\-\[\(]+$", "", name)
    name = re.sub(r"^[\s._\-\[\(]+", "", name)
    return name.strip()

def parse_extinf(line):
    result = {"duration": "-1", "attributes": {}, "title": ""}
    if not line.startswith("#EXTINF:"):
        return result
    content = line[8:].strip()
    parts = []
    current = ""
    in_quotes = False
    for char in content:
        if char == '"':
            in_quotes = not in_quotes
            current += char
        elif char == ',' and not in_quotes:
            parts.append(current)
            current = ""
        else:
            current += char
    parts.append(current)
    if len(parts) >= 2:
        attr_part = parts[0]
        title = ",".join(parts[1:]).strip()
    else:
        attr_part = content
        title = ""
    attr_part = attr_part.strip()
    duration_match = re.match(r"^(-?\d+)", attr_part)
    if duration_match:
        result["duration"] = duration_match.group(1)
        attr_part = attr_part[duration_match.end():].strip()
    attr_pattern = re.compile(r'(\w+(?:-\w+)*)\s*=\s*"([^"]*)"')
    for match in attr_pattern.finditer(attr_part):
        key = match.group(1).lower()
        val = match.group(2)
        result["attributes"][key] = val
    result["title"] = title
    return result

def build_extinf(duration, attributes, title):
    attr_strs = []
    for key, val in attributes.items():
        attr_strs.append(f'{key}="{val}"')
    if attr_strs:
        return f"#EXTINF:{duration} {" ".join(attr_strs)},{title}"
    else:
        return f"#EXTINF:{duration},{title}"

def normalize_series_key(name, season):
    key_name = re.sub(r"[^a-zA-Z0-9]", "", name.lower())
    if season is not None:
        return f"{key_name}_s{season}"
    return key_name

def process_m3u(input_path, output_path):
    if not os.path.exists(input_path):
        print(f"Errore: file non trovato: {input_path}")
        sys.exit(1)
    with open(input_path, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    output_lines = []
    series_seen = OrderedDict()
    i = 0
    while i < len(lines):
        line = lines[i].rstrip("\n")
        if line.startswith("#EXTM3U"):
            output_lines.append(line)
            i += 1
            continue
        if not line.startswith("#EXTINF:"):
            output_lines.append(line)
            i += 1
            continue
        extinf_line = line
        extinf_data = parse_extinf(extinf_line)
        title = extinf_data["title"]
        group = extinf_data["attributes"].get("group-title", "")
        tvg_name = extinf_data["attributes"].get("tvg-name", "")
        extra_lines = []
        i += 1
        while i < len(lines) and lines[i].strip().startswith("#") and not lines[i].strip().startswith("#EXTINF:"):
            extra_lines.append(lines[i].rstrip("\n"))
            i += 1
        url = ""
        if i < len(lines):
            url = lines[i].rstrip("\n")
            i += 1
        is_series = False
        season = None
        episode = None
        if is_series_by_group(group):
            is_series = True
            info = extract_episode_info(title)
            if info:
                season, episode = info
        if not is_series:
            for text in [title, tvg_name]:
                info = extract_episode_info(text)
                if info:
                    is_series = True
                    season, episode = info
                    break
        if is_series:
            series_name = clean_series_name(title)
            if not series_name:
                series_name = clean_series_name(tvg_name)
            if not series_name:
                series_name = title
            key = normalize_series_key(series_name, season)
            if key not in series_seen:
                series_seen[key] = True
                output_lines.append(extinf_line)
                for el in extra_lines:
                    output_lines.append(el)
                output_lines.append(url)
        else:
            output_lines.append(extinf_line)
            for el in extra_lines:
                output_lines.append(el)
            output_lines.append(url)
    with open(output_path, "w", encoding="utf-8") as f:
        for line in output_lines:
            f.write(line + "\n")
    total_input = sum(1 for l in lines if l.startswith("#EXTINF:"))
    total_output = sum(1 for l in output_lines if l.startswith("#EXTINF:"))
    series_kept = len(series_seen)
    print(f"\nFatto!")
    print(f"   File input:  {input_path}")
    print(f"   File output: {output_path}")
    print(f"   Entry totali input:  {total_input}")
    print(f"   Entry totali output: {total_output}")
    print(f"   Serie TV uniche mantenute: {series_kept}")
    print(f"   Entry risparmiate: {total_input - total_output}")

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Uso: python3 m3u_slimmer.py <input.m3u> <output.m3u>")
        print("")
        print("Lo script mantiene TUTTO (canali, film, sport...) ma per le Serie TV")
        print("mantiene solo il PRIMO episodio trovato, scartando i duplicati.")
        sys.exit(1)
    input_file = sys.argv[1]
    output_file = sys.argv[2]
    process_m3u(input_file, output_file)
