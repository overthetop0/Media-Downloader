# Media Downloader

Self-hosted Python + NiceGUI web application for downloading and managing VOD and Series content from Xtream API providers.

## Features

- Xtream API support
- VOD + Series support
- Concurrent downloads
- Queue manager
- Realtime dashboard
- M3U Skip
- Download rate limiting
- Custom User-Agent

## Screenshot

<img src="screenshots/media0.png" width="100%">
<img src="screenshots/media1.png" width="100%">
---

## Requirements

Recommended:

- 4 CPU cores
- 4 GB RAM

Required packages:

- Python 3.11+

---

## Install

```bash
sudo apt install python3-venv

mkdir md && cd md

python3 -m venv env

source env/bin/activate

## Dependencies

pip install sqlmodel
pip install aiohttp
pip install aiofiles
pip install tqdm
pip install nicegui
