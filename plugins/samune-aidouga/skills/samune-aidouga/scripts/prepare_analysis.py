#!/usr/bin/env python3
"""Create offline searchable gallery, all-image contact sheets, CSV and review samples."""
from __future__ import annotations
import argparse
import csv
import html
import json
import math
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from PIL import Image, ImageDraw, ImageOps
from collect_channel import read_records, valid_cached, write_json

VISUAL_FIELDS = ['layout', 'subject', 'text_amount', 'text_lines', 'palette', 'decoration', 'copy_pattern', 'notes']

def csv_safe(value):
    value = '' if value is None else str(value)
    return "'" + value if value.lstrip().startswith(('=', '+', '-', '@')) else value

def kind(row):
    tabs = row.get('tabs', [])
    if len(tabs) != 1:
        return 'mixed' if tabs else 'unknown'
    return tabs[0]

def select_sample(rows, size=18):
    """Union of latest, high-view and evenly spread listing ranks per tab; not a CTR ranking."""
    reasons = {}
    for tab in ('videos', 'shorts', 'streams', 'mixed', 'unknown'):
        subset = [r for r in rows if kind(r) == tab]
        recent = sorted(subset, key=lambda r: min(r.get('rank_in_tab', {}).values(), default=10**9))
        views = sorted([r for r in subset if isinstance(r.get('view_count'), (int, float))],
                       key=lambda r: r['view_count'], reverse=True)
        even = [recent[round(i * (len(recent)-1) / max(1, min(size, len(recent))-1))]
                for i in range(min(size, len(recent)))] if recent else []
        for label, selected in [('recent_listing', recent[:size]), ('high_views', views[:size]), ('across_listing', even)]:
            for row in selected:
                reasons.setdefault(row['video_id'], []).append(label)
    return reasons

def prepare(root, per_page=40):
    rows = list(read_records(root / 'manifest.jsonl').values())
    report = json.loads((root / 'collection_report.json').read_text(encoding='utf-8'))
    run_id = report['run_id']
    current = [r for r in rows if r.get('last_seen_run') == run_id]
    usable = [r for r in current if r.get('thumbnail_status') == 'saved' and valid_cached(root, r)
              and (not report.get('refresh_requested') or r.get('image_checked_run') == run_id)]
    usable.sort(key=lambda r: (kind(r), min(r.get('rank_in_tab', {}).values(), default=10**9), r['video_id']))
    analysis = root / 'analysis' / run_id
    analysis.mkdir(parents=True, exist_ok=True)
    # Keep each run's evidence separate. Regeneration does not overwrite human observations.
    samples = select_sample(usable)
    fields = ['video_id', 'title', 'video_url', 'kind', 'view_count', 'upload_date', 'rank_in_tab',
              'thumbnail_path', 'last_seen_run', 'thumbnail_status']
    with (analysis / 'videos.csv').open('w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            item = {k: row.get(k) for k in fields}
            item['kind'] = kind(row)
            writer.writerow({k: csv_safe(v) for k, v in item.items()})
    obs = analysis / 'observations.csv'
    if not obs.exists():
        with obs.open('w', newline='', encoding='utf-8-sig') as f:
            writer = csv.DictWriter(f, fieldnames=['video_id', 'kind', 'sample_reason', 'inspected'] + VISUAL_FIELDS)
            writer.writeheader()
            for row in usable:
                if row['video_id'] in samples:
                    writer.writerow(dict(video_id=row['video_id'], kind=kind(row),
                                         sample_reason=';'.join(samples[row['video_id']]), inspected='no'))
    pages = []
    indices = {}
    for tab in ('videos', 'shorts', 'streams', 'mixed', 'unknown'):
        subset = [r for r in usable if kind(r) == tab]
        for page in range(math.ceil(len(subset) / per_page)):
            batch = subset[page*per_page:(page+1)*per_page]
            width, cell_h, cols = 256, 173, 5
            sheet = Image.new('RGB', (width*cols, cell_h*math.ceil(len(batch)/cols)), '#161922')
            draw = ImageDraw.Draw(sheet)
            name = f'contact_{tab}_{page+1:03d}.jpg'
            for i, row in enumerate(batch):
                x, y = (i % cols)*width, (i // cols)*cell_h
                with Image.open(root / row['thumbnail_path']) as im:
                    thumb = ImageOps.contain(im.convert('RGB'), (248, 140))
                    sheet.paste(thumb, (x+4+(248-thumb.width)//2, y+4+(140-thumb.height)//2))
                label = f'{page*per_page+i+1:04d} | {row["video_id"]}'
                draw.text((x+5,y+148), label, fill='white')
                indices[row['video_id']] = {'sheet': name, 'index': page*per_page+i+1}
            sheet.save(analysis / name, quality=87)
            pages.append({'file': name, 'kind': tab, 'count': len(batch)})
    write_json(analysis / 'review_index.json', {'current_count': len(current), 'usable_count': len(usable),
               'selected_sample_count': len(samples), 'selection_reasons': samples, 'pages': pages, 'images': indices})
    def esc(value):
        return html.escape(str(value), quote=True)
    cards = []
    for row in usable:
        src = '../../' + quote(row['thumbnail_path'])
        title = esc(row.get('title', ''))
        vid = esc(row['video_id'])
        tab = kind(row)
        sample = row['video_id'] in samples
        cards.append(f'<article data-kind="{tab}" data-sample="{str(sample).lower()}"><a href="{esc(row["video_url"])}" target="_blank" rel="noopener noreferrer"><img loading="lazy" src="{src}" alt="{title}"></a><h2>{title}</h2><p>{vid} · {tab} · views: {esc(row.get("view_count"))}</p><small>{" / ".join(samples.get(row["video_id"], []))}</small></article>')
    state = esc(f'列挙 {len(current)} 件 / 今回利用可能な保存画像 {len(usable)} 枚 / 列挙範囲完了 {report["listed_scope_complete"]}')
    document = '''<!doctype html><html lang="ja"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>サムネッシー チャンネル資料</title><style>body{font:15px system-ui;background:#10131b;color:#e9edf4;margin:24px}header{position:sticky;top:0;padding:15px;background:#10131bef;z-index:1}input,select{padding:10px;margin:5px;background:#242a38;color:white;border:1px solid #65728a;border-radius:6px}main{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:18px}article{background:#202635;border-radius:8px;padding:10px}img{width:100%;aspect-ratio:16/9;object-fit:contain;background:#080a0f}h2{font-size:15px}p,small{color:#aebbd0}article[hidden]{display:none}a{color:#9bbcff}</style><header><h1>チャンネルのサムネ一覧</h1><p>STATE</p><input id="q" placeholder="タイトル・動画IDで検索" aria-label="検索"><select id="kind" aria-label="動画種類"><option value="">すべて</option><option>videos</option><option>shorts</option><option>streams</option><option>mixed</option><option>unknown</option></select><label><input type="checkbox" id="sample">分析候補のみ</label><span id="count"></span><p>再生数はCTRではありません。取得時点の画像です。全保存画像のページ別一覧と対応表は同じフォルダにあります。</p></header><main>CARDS</main><script>const q=document.querySelector('#q'),k=document.querySelector('#kind'),s=document.querySelector('#sample');function filter(){let n=0;document.querySelectorAll('article').forEach(a=>{a.hidden=!(a.textContent.toLowerCase().includes(q.value.toLowerCase())&&(!k.value||a.dataset.kind===k.value)&&(!s.checked||a.dataset.sample==='true'));if(!a.hidden)n++});document.querySelector('#count').textContent=n+'枚表示'}[q,k,s].forEach(e=>e.addEventListener('input',filter));filter();</script></html>'''
    (analysis / 'gallery.html').write_text(document.replace('STATE', state).replace('CARDS', '\n'.join(cards)), encoding='utf-8')
    print(f'analysis={analysis}')
    print(f'gallery={analysis / "gallery.html"}')
    print(f'contact_sheets={len(pages)} sample={len(samples)}')
    return analysis

def summarize(root, observations):
    manifest = read_records(root / 'manifest.jsonl')
    with observations.open(encoding='utf-8-sig', newline='') as f:
        rows = list(csv.DictReader(f))
    inspected, seen = [], set()
    for row in rows:
        vid = row.get('video_id')
        if vid in seen or vid not in manifest:
            raise ValueError(f'Duplicate or unknown observation ID: {vid}')
        seen.add(vid)
        if row.get('inspected', '').strip().lower() in ('yes', 'true', '1'):
            if any(not row.get(field, '').strip() for field in VISUAL_FIELDS[:-1]):
                raise ValueError(f'Incomplete visual observation: {vid}; use unknown or none when appropriate')
            row['kind'] = kind(manifest[vid])
            inspected.append(row)
    summary = {'generated_at': datetime.now(timezone.utc).isoformat(), 'inspected_n': len(inspected),
               'population': 'manually inspected sample only; no inference about CTR', 'groups': {}}
    for tab in sorted({r['kind'] for r in inspected}):
        group = [r for r in inspected if r['kind'] == tab]
        summary['groups'][tab] = {'inspected_n': len(group), 'fields': {}}
        for field in VISUAL_FIELDS[:-1]:
            counts = Counter(r[field] for r in group)
            summary['groups'][tab]['fields'][field] = [dict(value=k, count=v,
                sample_share=round(v/len(group),4), video_ids=[r['video_id'] for r in group if r[field]==k])
                for k,v in counts.most_common()]
    write_json(observations.parent / 'visual_summary.json', summary)
    print(f'inspected={len(inspected)} output={observations.parent / "visual_summary.json"}')

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--channel-dir', required=True, type=Path)
    p.add_argument('--observations', type=Path, help='Summarize completed visual observations instead of preparing gallery')
    args=p.parse_args()
    root=args.channel_dir.expanduser().resolve()
    if args.observations:
        summarize(root,args.observations)
    else:
        prepare(root)

if __name__=='__main__':
    main()
