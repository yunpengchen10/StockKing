"""Process-independent claims for official software jobs (never replay a past slot)."""
import json
import sqlite3
import uuid
from pathlib import Path


def run_once(service, slot, kwargs):
    slot = 'live' if slot == 'auto' else slot
    if not getattr(service, 'persistent_claims', False):
        return service._run(slot, **kwargs)
    official = kwargs.get('official')
    if official is False or (official is None and slot == 'live'):
        return service._run(slot, **kwargs)
    slot = '0920' if slot == '0922' else slot
    root = Path(service.yao.data_dir)
    root.mkdir(parents=True, exist_ok=True)
    now = service.clock()
    key = f'v1.1:{now.date().isoformat()}:{slot}'
    with sqlite3.connect(root / 'official-job-claims-v1.sqlite3', timeout=30) as con:
        con.execute('CREATE TABLE IF NOT EXISTS claims (key TEXT PRIMARY KEY, started_at TEXT NOT NULL, result TEXT)')
        try:
            con.execute('INSERT INTO claims VALUES (?,?,NULL)', (key, now.isoformat()))
        except sqlite3.IntegrityError:
            row = con.execute('SELECT result FROM claims WHERE key=?', (key,)).fetchone()
            if row and row[0]:
                result = json.loads(row[0]); result['idempotentReplay'] = True
                return result
            return {'status': 'already_claimed', 'scanSlot': slot, 'candidates': [], 'candidateCount': 0,
                    'official': True, 'message': '同一交易日时段已领取；不重复扫描或补造历史信号'}
    try:
        result = service._run(slot, **kwargs)
    except Exception:
        # Retain the claim; a failed official job must not be replayed with later quotes.
        raise
    with sqlite3.connect(root / 'official-job-claims-v1.sqlite3', timeout=30) as con:
        if result.get('status') == 'not_due':
            con.execute('DELETE FROM claims WHERE key=?', (key,))
            return result
        con.execute('UPDATE claims SET result=? WHERE key=?', (json.dumps(result, ensure_ascii=False, default=str, allow_nan=False), key))
    return result
