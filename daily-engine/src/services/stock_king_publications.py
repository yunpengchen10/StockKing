"""Immutable per-round homepage selections, separate from training and market observations."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3


RECORDS_VERSION = 'published-picks-v1'
TZ = timezone(timedelta(hours=8))


def missing_publication(run):
    return {'runId': run.get('run_id') or run.get('runId'), 'scanSlot': run.get('scanSlot'),
            'asOf': run.get('generatedAt') or run.get('as_of') or run.get('cutoff_at'), 'publishedAt': None,
            'version': None, 'status': 'unrecorded', 'items': []}


def _initialized(db):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='publications'").fetchone()


def published_count(publication):
    return len(publication['items']) if publication and publication.get('status') == 'published' else None


def _instant(stamp):
    try:
        parsed = datetime.fromisoformat(stamp)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=TZ)
    except (ValueError, TypeError):
        return None


class PicksPublications:
    def __init__(self, root):
        self.path = Path(root).resolve() / 'published-picks-v1.sqlite3'

    def get(self, run_id):
        if not run_id or not self.path.exists():
            return None
        with closing(sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=30)) as db:
            if not _initialized(db):
                return None
            row = db.execute('SELECT document FROM publications WHERE run_id=?', (run_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def latest(self):
        if not self.path.exists():
            return None
        with closing(sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=30)) as db:
            if not _initialized(db):
                return None
            row = db.execute('SELECT document FROM publications ORDER BY julianday(as_of) DESC,rowid DESC LIMIT 1').fetchone()
        return json.loads(row[0]) if row else None

    def publish(self, run, make_items, *, reason, persist=None, current_only=False):
        """First writer freezes order/time; a failed parent scan write rolls the publication back."""
        from src.services.yao_scout.potential_picks import VERSION

        run_id = run.get('run_id') or run.get('runId')
        if not run_id:
            return None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=60)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS publications (run_id TEXT PRIMARY KEY, as_of TEXT NOT NULL, version TEXT NOT NULL, document TEXT NOT NULL)')
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT document FROM publications WHERE run_id=?', (run_id,)).fetchone()
            # A round has a place in history once it was eligible to be the current
            # homepage. A slower job cannot introduce a previously unpublished
            # earlier round. At equal completion times the first committed ID wins.
            newest = db.execute('SELECT run_id,as_of FROM publications ORDER BY julianday(as_of) DESC,rowid DESC LIMIT 1').fetchone()
            stamp = _instant(missing_publication(run)['asOf'])
            superseded = newest and newest[0] != run_id and (stamp is None or _instant(newest[1]) >= stamp)
            if current_only and superseded:
                return None
            if row:
                doc = json.loads(row[0])
            else:
                doc = run.get('publishedPicks')
                if not (isinstance(doc, dict) and doc.get('status') == 'published'
                        and doc.get('runId') == run_id and (doc.get('publishedAt') or doc.get('recordedAt'))):
                    if superseded or not stamp or reason in ('history', 'migration'):
                        doc = missing_publication(run)
                        if superseded:
                            doc['status'] = 'superseded'
                        if persist:
                            persist(doc)
                        return doc
                    items = make_items()
                    doc = {**missing_publication(run), 'version': VERSION, 'items': items,
                           'publishedAt': datetime.now(TZ).isoformat(), 'status': 'published',
                           'publicationReason': reason}
                codes = [item['code'] for item in doc['items']]
                if (len(codes) != len(set(codes)) or len(codes) > 5
                        or [item['rank'] for item in doc['items']] != list(range(1, len(codes) + 1))):
                    raise ValueError('Published selection must contain at most five distinct stocks in rank order')
                db.execute('INSERT INTO publications VALUES (?,?,?,?)',
                           (run_id, doc['asOf'], doc['version'], json.dumps(doc, ensure_ascii=False, allow_nan=False)))
            if persist:
                persist(doc)
            return doc

    def restore_verified_legacy(self, run, items, *, version, proof):
        """One-time local maintenance for an independently verified prior homepage list."""
        if not proof or not isinstance(version, str) or not version.startswith('legacy-'):
            raise ValueError('The prior published selection requires a legacy version and provenance')
        doc = {**missing_publication(run), 'items': items, 'version': version, 'status': 'published',
               'recordedAt': datetime.now(TZ).isoformat(), 'publicationReason': 'verified_legacy_homepage',
               'provenance': proof}
        return self.publish({**run, 'publishedPicks': doc}, None, reason='restore')

    def history(self, *, run_id='', date='', symbol='', version=''):
        if not self.path.exists():
            return []
        conditions, values = [], []
        for field, value in (('run_id', run_id), ('substr(as_of,1,10)', date), ('version', version)):
            if value:
                conditions.append(field + '=?')
                values.append(value)
        where = (' WHERE ' + ' AND '.join(conditions)) if conditions else ''
        with closing(sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=30)) as db:
            if not _initialized(db):
                return []
            docs = [json.loads(row[0]) for row in db.execute(
                'SELECT document FROM publications' + where + ' ORDER BY julianday(as_of) DESC, rowid DESC', values)]
        if symbol:
            docs = [{**doc, 'items': [item for item in doc['items'] if item['code'] == symbol or item['name'] == symbol]} for doc in docs]
            docs = [doc for doc in docs if doc['items']]
        return docs
