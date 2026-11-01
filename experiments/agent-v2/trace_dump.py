"""轨迹压缩视图（诊断用，只读）：uv run --project backend python experiments/agent-v2/trace_dump.py <文件名> [题号,…] [评测目录/]

每次运行列出理解出的范围、逐步的工具与状态、模型请求的 token、报告的原因与核验结果、数据集。不打印思考内容（轨迹里本来没有）。"""
import json
import sys

E = sys.argv[3] if len(sys.argv) > 3 else ".demo/eval/"
f, only = sys.argv[1], set(sys.argv[2].split(",")) if len(sys.argv) > 2 and sys.argv[2] else None
d = json.load(open(E + f, encoding='utf-8'))
res = {(r['id'], r['repeat']): r for r in d['results']}
for item in d['runs']:
    if only and item['id'] not in only:
        continue
    run = item['run'] or {}
    r = res[(item['id'], item['repeat'])]
    print('=' * 100)
    print(item['id'], item['repeat'], run.get('model_adapter'), run.get('reasoning_effort'), run.get('status'), run.get('error'))
    print('Q:', run.get('question'))
    print('window', run.get('window'))
    print('score', {k: r[k] for k in ('conclusion', 'conclusion_ok', 'primary_hit', 'key_value_hit', 'sources_failed', 'passed', 'model_calls', 'seconds')})
    print('plan', json.dumps(run.get('plan'), ensure_ascii=False))
    for s in run.get('steps') or []:
        out = s.get('output') or {}
        if s['kind'] == 'model':
            print(f"  #{s['seq']} MODEL in={out.get('input_tokens')} out={out.get('output_tokens')} rsn={out.get('reasoning_tokens')} cache={out.get('cache_hit_tokens')} tools={out.get('tools')} {s['status']} {out.get('error','')}")
            continue
        a = dict(s.get('args') or {})
        a.pop('purpose', None)
        print(f"  #{s['seq']} {s['kind']}:{s['tool']} [{s['status']}] ds={s.get('dataset')} args={json.dumps(a, ensure_ascii=False)[:400]}")
        if s['status'] != 'ok' or s['kind'] == 'note':
            print('      out=', json.dumps(out, ensure_ascii=False)[:500])
    rep = run.get('report') or {}
    print('REPORT conclusion', rep.get('conclusion'), 'scope', json.dumps(rep.get('scope'), ensure_ascii=False))
    print('  title', rep.get('title') if isinstance(rep.get('title'), str) else rep.get('title'))
    print('  summary', (rep.get('summary') or '')[:400] if isinstance(rep.get('summary'), str) else rep.get('summary'))
    for c in rep.get('causes') or []:
        print('  cause', c.get('type'), c.get('direction'), c.get('status'), json.dumps(c.get('entity'), ensure_ascii=False), c.get('label'), (c.get('contribution') or {}).get('text'), c.get('errors'))
    for c in rep.get('claims') or []:
        print('  claim', c.get('type'), c.get('status'), (c.get('text') or '')[:300])
    print('  notes', rep.get('notes'))
    v = run.get('verification') or {}
    print('  verif ok', v.get('ok'), 'unv', v.get('unverified'), v.get('unverified_fields'), v.get('unverified_causes'), 'concl', json.dumps(v.get('conclusion'), ensure_ascii=False)[:300], 'revised', v.get('revised'))
    for ds in run.get('datasets') or []:
        print(f"  {ds['ref']} {ds['kind']} {ds['title'][:90]} rows={ds['row_count']} trunc={ds['truncated']} cols={ds['columns'][:8]}")
