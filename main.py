#!/usr/bin/env python3
import logging
from nicegui import ui
from ui_components import init_ui

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

if __name__ == '__main__':
    init_ui()
    ui.run(
        host='0.0.0.0',
        port=8080,
        reload=False,
        show=False
    )