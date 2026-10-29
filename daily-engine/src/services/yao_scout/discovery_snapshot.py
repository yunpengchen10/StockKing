"""One prior full-pool observation for prospective change discovery.

Captured time is an availability boundary, never a replacement for provider
source times. Reading this cache cannot refresh, scan, or repair market data.
"""
from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
from uuid import uuid4

import pandas as pd

from .daily_opportunities import clean


class DiscoverySnapshotStore:
    def __init__(self, data_dir):
        self.path = Path(data_dir) / 'opportunity-discovery-v1.json'

    def read(self, now):
        try:
            value = json.loads(self.path.read_text(encoding='utf-8'))
            captured = datetime.fromisoformat(value['captured_at'])
            if (value.get('version') != 1 or not captured.tzinfo or not now.tzinfo
                    or captured > now or captured.astimezone(now.tzinfo).date() != now.date()):
                return None
            frame = pd.DataFrame(value['rows'])
            frame.attrs.update(value.get('attrs') or {})
            frame.attrs['captured_at'] = captured.isoformat()
            return frame
        except (OSError, ValueError, TypeError, KeyError):
            return None

    def save(self, frame, captured_at):
        if frame.empty or not captured_at.tzinfo:
            return False
        value = {'version': 1, 'captured_at': captured_at.isoformat(),
                 'attrs': clean(dict(frame.attrs)), 'rows': clean(frame.to_dict('records'))}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + '.' + uuid4().hex + '.tmp')
        try:
            temporary.write_text(json.dumps(value, ensure_ascii=False, default=str,
                                             allow_nan=False), encoding='utf-8')
            os.replace(temporary, self.path)
            return True
        finally:
            temporary.unlink(missing_ok=True)
