#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Запуск веб-интерфейса SAOL v2 (существующий мобильный UI с фото + движок v2).

Из папки prototype:
    python -m saol2.serve
Затем открой:  http://localhost:8765
"""
from __future__ import annotations

import os


def main() -> int:
    import uvicorn

    port = int(os.environ.get("SAOL2_PORT", "8765"))
    print(f"SAOL v2 UI: открой http://localhost:{port}  (Ctrl+C — остановить)")
    uvicorn.run("saol2.web_app:app", host="0.0.0.0", port=port, reload=False, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
