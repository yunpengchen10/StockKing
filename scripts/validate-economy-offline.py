"""Read-only user-cache audit; all output and training objects stay in artifacts."""
import json
import os
import sqlite3
import sys
from pathlib import Path
import pandas as pd

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'daily-engine'))
from src.quant.tier_models import TierModelService
from src.services.economy_review import summarize, encode, review_body, DEFAULTS
out=ROOT/'artifacts/v2.5.0/offline-validation'
out.mkdir(parents=True,exist_ok=True)
cache=Path(os.environ['LOCALAPPDATA'])/'Stock King/cache/screening'
files=sorted((cache/'quant/panel-store').glob('*.pkl'))
# Broad deterministic sample; this audit does not qualify the entire universe.
sample=files[::max(1,len(files)//80)][:80]
panel=pd.concat([pd.read_pickle(p).tail(1200) for p in sample],ignore_index=True)
service=TierModelService(out/'models',out/'cache')
panel=service._finalize_panel(panel)
artifact=service._train_regular(panel)
result={'sampleSymbols':panel.symbol.nunique(),'availableCachedSymbols':len(files),'rows':len(panel),
        'from':str(panel.date.min()),'through':str(panel.date.max()),
        'model':artifact.registry_payload(),
        'scope':'cached, deterministic 80-symbol sample; not full-universe qualification; installed champion unchanged',
        'executionValidation':{'status':'not_verifiable_from_current_cache',
           'reason':'Cached daily bars are adjusted and do not supply verified historical limit prices, corporate actions or contemporaneous tradability. No executable performance is fabricated.'}}
(out/'local-five-day.json').write_text(encode(result),encoding='utf-8')
db_path=Path(os.environ['APPDATA'])/'Stock King/data/daily/stock-analysis.db'
db=sqlite3.connect(db_path.as_uri()+'?mode=ro',uri=True)
history=[json.loads(r[0]) for r in db.execute('SELECT result_json FROM yao_runs ORDER BY id DESC LIMIT 100')]
db.close()
candidate_run=next((r for r in history if r.get('candidates')), {})
facts={p['code']:{k:p.get(k) for k in ('code','name','rank','features','quote','modelStatus','modelVersion','risks','thesis')} for p in candidate_run.get('candidates',[])[:5]}
snapshot=json.loads((cache/'snapshot.last_good.json').read_text(encoding='utf-8'))
old={'universe':snapshot,'history':history}
config={'model':'deepseek-v4-pro','max_tokens':2000}
body,_,budget=review_body(config,'审查候选风险，不改变排名',summarize(facts),list(facts),DEFAULTS)
old_bytes=len(encode(old).encode());new_bytes=len(encode(body).encode())
comparison={'historicalRun':candidate_run.get('run_id'),'oldPacketBytes':old_bytes,'newPacketBytes':new_bytes,
            'reduction':1-new_bytes/old_bytes,'budget':budget,'actualProviderTokens':None,
            'note':'Same cached history/candidates; byte-size comparison, no billable API calls; includes saved snapshot wrapper in old packet.'}
(out/'request-size.json').write_text(encode(comparison),encoding='utf-8')
print(encode({'localFiveDay':artifact.metrics.get('localFiveDay'),'comparison':comparison}))
