"""Append validated current context tables; retain original observation times."""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
from zoneinfo import ZoneInfo


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--destination', required=True, type=Path)
    parser.add_argument('--receipt', required=True, type=Path)
    args = parser.parse_args()
    now = datetime.now(ZoneInfo('Asia/Shanghai'))
    imported = []
    for path in sorted(args.source.resolve().glob('*/*.json')):
        data = json.loads(path.read_text(encoding='utf-8'))
        observed = datetime.fromisoformat(data['observedAt'])
        assert observed.tzinfo and observed <= now and (now-observed).total_seconds() < 21600
        assert data['status'] == 'available' and data['kind'] in {'financials', 'forecast', 'unlock'}
        assert data['collection']['complete'] and len(data['rows']) == data['collection']['recordCount']
        for row in data['rows']:
            known = datetime.fromisoformat(row['_firstSeenAt'])
            assert known.tzinfo and known <= observed
        target = args.destination.resolve() / path.relative_to(args.source.resolve())
        target.parent.mkdir(parents=True, exist_ok=True)
        raw = path.read_bytes()
        if target.exists():
            assert target.read_bytes() == raw, 'Conflicting archive is never replaced'
            created = False
        else:
            with target.open('xb') as out:
                out.write(raw)
            created = True
        imported.append({'path': str(target), 'created': created,
                         'sha256': hashlib.sha256(raw).hexdigest(), 'kind': data['kind'],
                         'params': data['params'], 'rows': len(data['rows']),
                         'observedAt': data['observedAt'], 'source': data['source']})
    receipt = {'importedAt': now.isoformat(), 'tables': imported,
               'meaning': 'Current evidence only; historical decisions and source times were not rewritten.'}
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    args.receipt.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(receipt, ensure_ascii=False))


if __name__ == '__main__':
    main()
