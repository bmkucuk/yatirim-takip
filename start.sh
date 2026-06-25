#!/bin/bash
python3 -c "
import sys
sys.path.insert(0, '.')
from app import app, init_db
with app.app_context():
    init_db()
print('DB initialized.')
"
gunicorn app:app \
  --bind 0.0.0.0:${PORT:-10000} \
  --workers 2 \
  --timeout 120 \
  --preload
