"""Opt-in real desktop gateway probe; no user ledger, training or notification.

Run through TestStockKingDataIngestionLiveProbe so authentication remains in the
child environment. Output contains public market evidence, never credentials.
"""
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import tempfile
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'daily-engine'))
from src.services.software_market import SoftwareMarketClient
from src.services.yao_scout.daily_opportunities import mainboard
from src.services.yao_scout.research_factors import MarketEmotionArchive


def main():
    client = SoftwareMarketClient.from_environment()
    if not client.available:
        raise RuntimeError('An authenticated local test gateway is required')
    output = Path(os.environ['STOCK_KING_BREADTH_PROBE_OUTPUT']).resolve()
    output.mkdir(parents=True, exist_ok=True)
    snapshot = client.snapshot()
    original_attrs = dict(snapshot.attrs)
    original_times = snapshot['source_time'].tolist()
    enriched = client.enrich_snapshot_quotes(mainboard(snapshot), timeout=12)
    received = datetime.fromisoformat(enriched.attrs['market_quote_received_at'])
    with tempfile.TemporaryDirectory(prefix='stock-king-breadth-probe-') as directory:
        observed = MarketEmotionArchive(directory).observe(enriched, received, received_at=received)
    assert snapshot['source_time'].tolist() == original_times
    assert snapshot.attrs == original_attrs
    assert enriched.attrs['stale'] == original_attrs['stale']
    result = {
        'checkedAt': datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
        'directoryRows': len(snapshot), 'directoryComplete': original_attrs.get('directory_complete'),
        'selectionFieldsStale': original_attrs.get('stale'),
        'enrichment': enriched.attrs.get('market_quote_enrichment'),
        'marketBreadth': {key: observed.get(key) for key in
            ('status', 'asOf', 'evaluatedAt', 'source', 'features', 'gaps')},
        'isolatedArchive': True, 'originalFieldsUnchanged': True,
        'meaning': 'Live provider ingestion only; not proof of an intraday entry or profitability.',
    }
    capture = {'attrs': enriched.attrs, 'rows': json.loads(enriched.to_json(orient='records', force_ascii=False))}
    (output / 'market-snapshot.json').write_text(json.dumps(capture, ensure_ascii=False, default=str), encoding='utf-8')
    (output / 'receipt.json').write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=True, default=str))
    features = observed.get('features') or {}
    if not observed.get('asOf') or features.get('advancing_pct') is None or features.get('source_coverage', 0) < .8:
        raise RuntimeError('Live breadth evidence unavailable; inspect the receipt without relaxing gates')


if __name__ == '__main__':
    main()
