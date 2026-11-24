#!/usr/bin/env python3
"""Собирает servis-setup.sh — установщик одним файлом (сайт + сервер внутри).
Запуск:  python3 build.py
"""
import base64, pathlib

root = pathlib.Path(__file__).resolve().parent
tpl = (root / 'server' / 'setup-template.sh').read_text(encoding='utf-8')
py = (root / 'server' / 'server.py').read_text(encoding='utf-8').rstrip('\n')
idx = base64.b64encode((root / 'index.html').read_bytes()).decode('ascii')
out = tpl.replace('__SERVER_PY__', py).replace('__INDEX_B64__', idx)
(root / 'servis-setup.sh').write_text(out, encoding='utf-8', newline='\n')
print('servis-setup.sh собран')
