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
        host='0.0.0.0',           # Accetta connessioni da qualsiasi IP
        port=8080,                # Porta di ascolto
        password='tua_password',  # 🔒 CAMBIA QUESTA PASSWORD!
        reload=False,             # Disabilita auto-reload in produzione
        show=False                # Modalità headless (no browser automatico)
    )