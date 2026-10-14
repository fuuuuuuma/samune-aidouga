#!/usr/bin/env python3
"""Import local references and prepare a full, auditable visual review. No generation."""
from __future__ import annotations
import argparse
import hashlib
import html
import json
import math
import re
import shutil
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from PIL import Image, ImageDraw, ImageOps

EXTENSIONS = {'.png', '.jpg', '.jpeg', '.webp', '.avif', '.heic', '.tif', '.tiff'}

def stamp():
    return datetime.now(timezone.utc).isoformat()

def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    tmp.replace(path)

def records(path):
    if path.suffix == '.jsonl':
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    data = json.loads(path.read_text())
    return data if isinstance(data, list) else data['images']

def natural(path):
    return [int(x) if x.isdigit() else x.lower() for x in re.split(r'(\d+)', unicodedata.normalize('NFC', str(path)))]

def import_local(args):
    source, root = args.source_dir.resolve(), args.out_dir.resolve()
    if not source.is_dir() or source == root or source in root.parents:
        raise ValueError('Use an existing source and a separate output tree')
    root.mkdir(parents=True, exist_ok=True)
    all_files = sorted((p for p in source.rglob('*') if p.is_file() and not any(x.startswith('.') for x in p.relative_to(source).parts)), key=natural)
    rows, by_hash, psds, failures, original_count = [], {}, [], [], 0
    for path in all_files:
        stat = path.stat()
        if path.suffix.lower() == '.psd':
            psds.append({'path': str(path), 'name': path.name, 'size_bytes': stat.st_size, 'mtime_ns': stat.st_mtime_ns})
            continue
        if path.suffix.lower() not in EXTENSIONS:
            continue
        original_count += 1
        try:
            payload = path.read_bytes()
            digest = hashlib.sha256(payload).hexdigest()
            with Image.open(path) as im:
                size, fmt = list(im.size), im.format
                im.verify()
            source_record = {'path': str(path), 'relative_path': str(path.relative_to(source)), 'bytes': stat.st_size, 'mtime_ns': stat.st_mtime_ns}
            if digest in by_hash:
                by_hash[digest]['sources'].append(source_record)
                continue
            destination = root / 'raw' / (digest + path.suffix.lower())
            destination.parent.mkdir(exist_ok=True)
            if not destination.exists():
                shutil.copy2(path, destination)
            if hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
                raise ValueError('Stored file hash mismatch')
            row = {'id': 'img-' + digest[:16], 'ordinal': len(rows) + 1, 'title': path.stem, 'thumbnail_path': str(destination.relative_to(root)), 'sha256': digest, 'width': size[0], 'height': size[1], 'format': fmt, 'sources': [source_record], 'kind': 'local_image', 'visual_review': 'pending'}
            rows.append(row)
            by_hash[digest] = row
        except Exception as exc:
            failures.append({'path': str(path), 'error': str(exc)})
    manifest = root / 'manifest.jsonl'
    payload = ''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows)
    if manifest.exists() and manifest.read_text() != payload:
        history = root / 'history' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        history.mkdir(parents=True)
        shutil.copy2(manifest, history / manifest.name)
    manifest.write_text(payload)
    write_json(root / 'psd-inputs.json', psds)
    report = {'at': stamp(), 'name': args.name, 'source_directory': str(source), 'image_files': original_count, 'unique_images': len(rows), 'duplicate_image_files': sum(len(r['sources']) - 1 for r in rows), 'psd_files': len(psds), 'failures': failures, 'originals_modified': False, 'local_scope_only': True}
    write_json(root / 'collection_report.json', report)
    print(json.dumps(report, ensure_ascii=False))
    if failures:
        raise SystemExit(2)

def resolve_images(library):
    images = []
    for source in sorted(library['sources'], key=lambda s: s['priority']):
        for spec in source['manifests']:
            root = Path(spec['root']).expanduser().resolve()
            for row in records(Path(spec['path'])):
                if row.get('visible') is False or row.get('thumbnail_status') not in (None, 'saved'):
                    continue
                rel = row.get('thumbnail_path') or row.get('preview_path') or row.get('path')
                if not rel:
                    continue
                path = (root / rel).resolve()
                if not path.is_relative_to(root):
                    raise ValueError('Image path escapes manifest root')
                with Image.open(path) as im:
                    size = list(im.size)
                    im.verify()
                images.append({'id': source['id'] + ':' + str(row.get('id') or row.get('video_id')), 'source_id': source['id'], 'source_name': source['name'], 'title': row.get('title') or row.get('name') or path.stem, 'path': str(path), 'dimensions': size, 'kind': row.get('kind') or ','.join(row.get('tabs', [])) or spec.get('kind', 'unknown'), 'metadata_path': row.get('metadata_path'), 'manifest': spec['path']})
    ids = [r['id'] for r in images]
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate reference IDs')
    return images

def prepare(args):
    registry = json.loads(args.library.read_text())
    images = resolve_images(registry)
    out = args.out_dir.resolve()
    out.mkdir(parents=True, exist_ok=True)
    if (out / 'review-index.json').exists():
        raise ValueError('Use a new review folder for each request; do not replace earlier visual evidence')
    per_page, columns, width, cell_h = 24, 4, 384, 242
    pages, cards = [], []
    for source in sorted(registry['sources'], key=lambda s: s['priority']):
        rows = [r for r in images if r['source_id'] == source['id']]
        for page in range(math.ceil(len(rows) / per_page)):
            batch = rows[page * per_page:(page + 1) * per_page]
            pid = source['id'] + '-' + f'{page + 1:03d}'
            name = 'contact-' + pid + '.jpg'
            canvas = Image.new('RGB', (width * columns, cell_h * math.ceil(len(batch) / columns)), '#111521')
            draw = ImageDraw.Draw(canvas)
            for i, row in enumerate(batch):
                x, y = i % columns * width, i // columns * cell_h
                with Image.open(row['path']) as im:
                    preview = ImageOps.contain(ImageOps.exif_transpose(im).convert('RGB'), (376, 212))
                    canvas.paste(preview, (x + 4 + (376 - preview.width) // 2, y + 4 + (212 - preview.height) // 2))
                row['page_id'], row['position'] = pid, i + 1
                row['review_number'] = page * per_page + i + 1
                draw.text((x + 6, y + 221), f'{row["review_number"]:04d} | {row["id"].split(":",1)[1]}', fill='white')
            canvas.save(out / name, quality=91)
            pages.append({'page_id': pid, 'file': name, 'source_id': source['id'], 'count': len(batch), 'image_ids': [r['id'] for r in batch]})
    for row in images:
        rel = Path(row['path'])
        import os
        src = quote(os.path.relpath(rel, out))
        title = html.escape(row['title'])
        cards.append(f'<article data-source="{html.escape(row["source_id"])}"><a href="{src}"><img loading="lazy" src="{src}" alt="{title}"></a><h2>{title}</h2><p>{html.escape(row["source_name"])} · {row["review_number"]:04d}</p><small>{html.escape(row["id"])}</small></article>')
    page_html = '''<!doctype html><html lang="ja"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>サムネイル参考ライブラリ</title><style>body{background:#10131b;color:#edf3fa;font:15px system-ui;margin:24px}header{position:sticky;top:0;background:#10131bef;padding:12px;z-index:1}input{padding:10px;width:min(500px,80%)}main{display:grid;grid-template-columns:repeat(auto-fill,minmax(310px,1fr));gap:16px}article{background:#202635;padding:10px;border-radius:8px}img{width:100%;aspect-ratio:16/9;object-fit:contain}h2{font-size:14px}p,small{color:#aebbd0}a{color:#aec9ff}article[hidden]{display:none}</style><header><h1>サムネイル参考ライブラリ</h1><p>検索を解除すると全画像を表示します。</p><input id="q" placeholder="チャンネル・タイトル・IDで検索"><span id="n"></span></header><main>CARDS</main><script>const q=document.querySelector('#q');function f(){let n=0;document.querySelectorAll('article').forEach(a=>{a.hidden=!a.textContent.toLowerCase().includes(q.value.toLowerCase());if(!a.hidden)n++});document.querySelector('#n').textContent=' '+n+'枚'}q.addEventListener('input',f);f();</script></html>'''
    (out / 'gallery.html').write_text(page_html.replace('CARDS', '\n'.join(cards)))
    index = {'created_at': stamp(), 'library': str(args.library.resolve()), 'image_count': len(images), 'page_count': len(pages), 'pages': pages, 'images': images, 'review_status': 'pending_visual_inspection'}
    write_json(out / 'review-index.json', index)
    (out / 'page-reviews.jsonl').touch()
    print(json.dumps({'out_dir': str(out), 'images': len(images), 'pages': len(pages), 'status': 'pending_visual_inspection'}, ensure_ascii=False))

def verify(args):
    index = json.loads((args.review_dir / 'review-index.json').read_text())
    reviews = records(args.review_dir / 'page-reviews.jsonl')
    valid = {r['page_id']: r for r in reviews if r.get('viewed') is True and r.get('notes')}
    pages = {p['page_id'] for p in index['pages']}
    unknown = set(valid) - pages
    missing = pages - set(valid)
    result = {'page_count': len(pages), 'reviewed_pages': len(pages & set(valid)), 'missing_pages': sorted(missing), 'unknown_pages': sorted(unknown), 'all_pages_recorded': not missing and not unknown, 'limit': 'Records validate coverage only; the assistant must actually view every page.'}
    write_json(args.review_dir / 'coverage.json', result)
    print(json.dumps(result, ensure_ascii=False))
    if missing or unknown:
        raise SystemExit(2)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest='mode', required=True)
    p = subs.add_parser('import-local'); p.add_argument('--source-dir', type=Path, required=True); p.add_argument('--out-dir', type=Path, required=True); p.add_argument('--name', required=True); p.set_defaults(func=import_local)
    p = subs.add_parser('prepare'); p.add_argument('--library', type=Path, required=True); p.add_argument('--out-dir', type=Path, required=True); p.set_defaults(func=prepare)
    p = subs.add_parser('verify'); p.add_argument('--review-dir', type=Path, required=True); p.set_defaults(func=verify)
    args = parser.parse_args(); args.func(args)

if __name__ == '__main__':
    main()
