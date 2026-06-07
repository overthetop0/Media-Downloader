# Media Downloader

Self-hosted Python + NiceGUI web application for downloading and managing VOD and Series content from Xtream API providers.

## Features

* Xtream Codes API support
* Mass VOD and Series downloads
* Concurrent downloads
* Queue manager
* Realtime dashboard
* M3U compare/skip support
* Download speed limiting
* Custom User-Agent support
* Self-hosted web interface

## Screenshots

<img src="media0.png" width="100%">
<br>
<img src="media1.png" width="100%">

---

## Requirements

Recommended:

* 4 CPU cores
* 4 GB RAM

Minimum:

* Python 3.11+
* Linux server / VPS

---

## Installation

```bash
sudo apt update
sudo apt install -y python3-venv

mkdir md && cd md

python3 -m venv env
source env/bin/activate
```

## Dependencies

```bash
pip install sqlmodel
pip install aiohttp
pip install aiofiles
pip install tqdm
pip install nicegui
```

Or:

```bash
pip install -r requirements.txt
```

---

## Usage

```bash
python main.py
```

Default web UI:

```text
http://SERVER_IP:8080
```

---

## How It Works

1. Enter the IPTV provider URL, username, password and choose Movies or Series.

2. Configure:

   * Number of concurrent downloads
   * Custom User-Agent
   * Maximum download speed

3. Save the provider configuration.

4. Optionally add an M3U playlist to automatically skip already existing content.

5. Search for specific content or use the preview button to browse categories.

6. Add movies directly to the download queue or select a series and choose specific episodes.

7. Use the download queue to:

   * Start downloads
   * Stop downloads
   * Retry failed downloads
   * Remove content

---

## API Compatibility

The application targets Xtream-Codes API endpoints exposed through `player_api.php`.


## Notes

This project is intended for educational and personal use only.

Users are responsible for complying with their local laws and content provider terms of service.

---

## Contributing

Contributions, improvements, bug reports and pull requests are welcome.

Feel free to fork the project and improve it.

---

## License

MIT License
