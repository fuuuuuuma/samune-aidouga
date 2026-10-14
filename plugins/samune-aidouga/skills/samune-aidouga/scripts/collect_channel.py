#!/usr/bin/env python3
"""Archive public channel thumbnails only; never download video/audio.
A channel name is resolved by the assistant before this command receives its UC ID.
"""
from __future__ import annotations
import argparse
import hashlib
import io
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from PIL import Image

CHANNEL_RE = re.compile(r'UC[A-Za-z0-9_-]{22}')
VIDEO_RE = re.compile(r'[A-Za-z0-9_-]{11}')
TABS = ('videos', 'shorts', 'streams')

def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')

def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temp.replace(path)

def read_records(path):
    rows = {}
    if path.exists():
        for line in path.read_text(encoding='utf-8').splitlines():
            if line.strip():
                row = json.loads(line)
                rows[row['video_id']] = row
    return rows

def write_records(path, rows):
    temp = path.with_suffix('.tmp')
    temp.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in rows.values()), encoding='utf-8')
    temp.replace(path)

def clean_url(value):
    """Accept image CDN URLs only. Never fetch arbitrary URLs from metadata."""
    if not value:
        return None
    try:
        p = urllib.parse.urlsplit(value)
        host = (p.hostname or '').lower()
        port = p.port
    except ValueError:
        return None
    if p.scheme != 'https' or p.username or p.password or port not in (None, 443):
        return None
    if host not in ('i.ytimg.com', 'i9.ytimg.com', 'img.youtube.com'):
        return None
    return value

def candidates(row):
    vid = row['video_id']
    found = []
    thumbs = sorted(row.get('thumbnails') or [], key=lambda t: (t.get('width') or 0) * (t.get('height') or 0), reverse=True)
    # Prefer actual metadata (particularly Shorts), then conventional fallbacks.
    for t in thumbs:
        url = clean_url(t.get('url'))
        if url and url not in found:
            found.append(url)
    for name in ('maxresdefault.jpg', 'hq720.jpg', 'sddefault.jpg', 'hqdefault.jpg'):
        url = f'https://i.ytimg.com/vi/{vid}/{name}'
        if url not in found:
            found.append(url)
    return found

class RateLimited(Exception):
    pass

class Fetcher:
    def __init__(self, interval=1.0, retries=2):
        self.interval = interval
        self.retries = retries
        self.last = 0.0

    def get(self, url):
        for attempt in range(self.retries + 1):
            time.sleep(max(0, self.interval - (time.monotonic() - self.last)))
            self.last = time.monotonic()
            try:
                request = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 SumnessyTeam/2.0'})
                with urllib.request.urlopen(request, timeout=25) as response:
                    if not clean_url(response.geturl()):
                        raise ValueError('unexpected image redirect')
                    data = response.read(15 * 1024 * 1024 + 1)
                    if len(data) > 15 * 1024 * 1024:
                        raise ValueError('image exceeds 15 MiB')
                    return data
            except urllib.error.HTTPError as exc:
                if exc.code in (403, 429):
                    raise RateLimited(f'HTTP {exc.code}; stopped without bypass') from exc
                if exc.code == 404:
                    raise
                if exc.code < 500 or attempt == self.retries:
                    raise
            except (urllib.error.URLError, TimeoutError):
                if attempt == self.retries:
                    raise
            time.sleep(2 ** attempt)

def valid_cached(root, row):
    rel = row.get('thumbnail_path')
    if not rel:
        return False
    path = (root / rel).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        return False
    try:
        data = path.read_bytes()
        if row.get('sha256') != hashlib.sha256(data).hexdigest():
            return False
        with Image.open(io.BytesIO(data)) as im:
            im.verify()
        return True
    except (OSError, ValueError):
        return False

def download(root, row, fetcher):
    errors = []
    for url in candidates(row):
        try:
            payload = fetcher.get(url)
            with Image.open(io.BytesIO(payload)) as im:
                im.verify()
            with Image.open(io.BytesIO(payload)) as im:
                width, height = im.size
                fmt = im.format
            # Common missing-thumbnail placeholders are 120x90.
            if width < 200 or height < 90:
                raise ValueError('too small or placeholder')
            ext = {'JPEG': '.jpg', 'PNG': '.png', 'WEBP': '.webp'}.get(fmt)
            if not ext:
                raise ValueError('unsupported image format')
            digest = hashlib.sha256(payload).hexdigest()
            dest = root / 'raw' / row['video_id'] / (digest[:20] + ext)
            dest.parent.mkdir(parents=True, exist_ok=True)
            if not dest.exists():
                temp = dest.with_suffix(ext + '.part')
                temp.write_bytes(payload)
                temp.replace(dest)
            return dict(thumbnail_status='saved', thumbnail_path=dest.relative_to(root).as_posix(),
                        thumbnail_url=url, sha256=digest, width=width, height=height,
                        downloaded_at=now(), last_checked_at=now(), download_errors=[])
        except RateLimited:
            raise
        except (OSError, ValueError, urllib.error.URLError, TimeoutError) as exc:
            errors.append({'url': url, 'error': str(exc)})
    return dict(thumbnail_status='failed', download_errors=errors, last_checked_at=now())

class Logger:
    def __init__(self):
        self.warnings = []
        self.errors = []
    def debug(self, msg):
        pass
    def warning(self, msg):
        self.warnings.append(str(msg))
        print('warning: ' + str(msg), file=sys.stderr, flush=True)
    def error(self, msg):
        self.errors.append(str(msg))

def enumerate_tab(channel_id, tab, limit, interval, receive):
    import yt_dlp
    logger = Logger()
    report = dict(status='running', url=f'https://www.youtube.com/channel/{channel_id}/{tab}',
                  entries_seen=0, skipped_entries=0, warnings=[], errors=[])
    opts = dict(extract_flat=True, skip_download=True, quiet=True, logger=logger,
                lazy_playlist=True, ignoreerrors=False, socket_timeout=25,
                retries=2, extractor_retries=2, sleep_interval_requests=interval,
                cachedir=False, noplaylist=False)
    if limit:
        opts['playlistend'] = limit
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(report['url'], download=False)
            if not info or info.get('channel_id') != channel_id:
                raise ValueError('Channel identity could not be verified')
            report['channel_name'] = info.get('channel') or info.get('uploader')
            report['channel_id'] = info.get('channel_id')
            report['declared_playlist_count'] = info.get('playlist_count')
            for rank, entry in enumerate(info.get('entries') or [], start=1):
                report['entries_seen'] += 1
                if not entry or not VIDEO_RE.fullmatch(entry.get('id') or ''):
                    report['skipped_entries'] += 1
                    continue
                if entry.get('channel_id') not in (None, channel_id):
                    report['skipped_entries'] += 1
                    continue
                receive(entry, tab, rank)
            report['status'] = 'limited' if limit else 'enumerated'
    except Exception as exc:
        message = str(exc)
        # Only explicit absent-tab messages count as an empty tab; network failure never does.
        if re.search(r'does not have (?:a |an )?(?:videos|shorts|streams|live) tab', message, re.I):
            report['status'] = 'tab_absent'
        else:
            report['status'] = 'error'
            report['errors'].append(message)
    report['warnings'] = logger.warnings
    report['errors'].extend(logger.errors)
    if report['status'] == 'enumerated' and (logger.warnings or logger.errors or report['skipped_entries']):
        report['status'] = 'incomplete'
    return report

def collect(args):
    root = args.out_dir.expanduser().resolve() / args.channel_id
    root.mkdir(parents=True, exist_ok=True)
    lock = root / '.collection.lock'
    try:
        lock_handle = lock.open('x', encoding='utf-8')
    except FileExistsError:
        raise SystemExit('Collection lock exists. Check that no collector is running before removing this lock.')
    try:
        lock_handle.write(now())
        lock_handle.close()
        return collect_locked(args, root)
    finally:
        lock.unlink(missing_ok=True)

def collect_locked(args, root):
    from yt_dlp.version import __version__
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    rows = read_records(root / 'manifest.jsonl')
    # Small append-only progress journal avoids rewriting a large Drive manifest per image.
    journal = root / 'download_progress.jsonl'
    if journal.exists():
        lines = journal.read_text(encoding='utf-8').splitlines()
        for index, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                if index != len(lines) - 1:
                    raise
                print('warning: ignored truncated final progress record; image will be rechecked', file=sys.stderr)
                break
            rows[record['video_id']] = record
        write_records(root / 'manifest.jsonl', rows)
        journal.unlink()
    previous_count = len(rows)
    run = dict(run_id=run_id, started_at=now(), channel_id=args.channel_id,
               channel_name=args.channel_name, source_url=args.source_url,
               requested_tabs=list(args.tabs), limit_per_tab=args.limit, refresh_requested=args.refresh, yt_dlp_version=__version__,
               archive_semantics='current publicly listed thumbnails; not historical thumbnail versions',
               tabs={}, state='running')
    write_json(root / 'collection_report.json', run)

    def receive(e, tab, rank):
        vid = e['id']
        old = rows.get(vid, {})
        membership = sorted(set(old.get('tabs', [])) | {tab})
        ranks = dict(old.get('rank_in_tab', {}))
        ranks[tab] = rank
        rows[vid] = {**old, 'video_id': vid, 'channel_id': args.channel_id,
                     'video_url': f'https://www.youtube.com/watch?v={vid}',
                     'title': e.get('title') or '', 'tabs': membership, 'rank_in_tab': ranks,
                     'view_count': e.get('view_count'), 'upload_date': e.get('upload_date'),
                     'date_note': 'date may be missing in flat playlist; never infer exact dates from rank',
                     'duration': e.get('duration'), 'live_status': e.get('live_status'),
                     'thumbnails': e.get('thumbnails') or [], 'first_seen_at': old.get('first_seen_at', now()),
                     'last_seen_at': now(), 'last_seen_run': run_id}
        if len(rows) % 100 == 0:
            write_records(root / 'manifest.jsonl', rows)
    try:
        for tab in args.tabs:
            print(f'Enumerating {tab} ...', flush=True)
            run['tabs'][tab] = enumerate_tab(args.channel_id, tab, args.limit, args.interval, receive)
            write_records(root / 'manifest.jsonl', rows)
            write_json(root / 'collection_report.json', run)
            if run['tabs'][tab].get('channel_name'):
                run['channel_name'] = run['tabs'][tab]['channel_name']
            print(f"{tab}: {run['tabs'][tab]['status']} ({run['tabs'][tab]['entries_seen']})", flush=True)
            if run['tabs'][tab]['status'] == 'error':
                # Preserve previous tabs; do not hammer the site after a failure.
                break
        fetcher = Fetcher(args.interval)
        pending = [r for r in rows.values() if r.get('last_seen_run') == run_id]
        for index, row in enumerate(pending, 1):
            if not args.refresh and valid_cached(root, row):
                row['thumbnail_status'] = 'saved'
            else:
                old_image = {k: row.get(k) for k in ('thumbnail_path', 'sha256', 'width', 'height', 'thumbnail_url', 'downloaded_at')}
                result = download(root, row, fetcher)
                # Failed refresh keeps the prior file as archive, but is not a fresh success.
                if result['thumbnail_status'] == 'failed' and valid_cached(root, row):
                    result['previous_image'] = old_image
                row.update(result)
            row['image_checked_run'] = run_id
            with journal.open('a', encoding='utf-8') as progress:
                progress.write(json.dumps(row, ensure_ascii=False) + '\n')
            print(f"{index}/{len(pending)} {row['video_id']} {row['thumbnail_status']}", flush=True)
        run['state'] = 'finished'
    except (KeyboardInterrupt, RateLimited) as exc:
        run['state'] = 'interrupted'
        run['interruption'] = str(exc) or 'KeyboardInterrupt'
    finally:
        write_records(root / 'manifest.jsonl', rows)
        journal.unlink(missing_ok=True)
        current = [r for r in rows.values() if r.get('last_seen_run') == run_id]
        saved = sum(r.get('thumbnail_status') == 'saved' and valid_cached(root, r)
                    and (not args.refresh or r.get('image_checked_run') == run_id) for r in current)
        enumeration_ok = len(run['tabs']) == len(args.tabs) and all(
            t['status'] in ('enumerated', 'tab_absent') for t in run['tabs'].values())
        run.update(finished_at=now(), previous_archive_count=previous_count,
                   enumerated_unique_this_run=len(current), saved_for_this_run=saved,
                   failed_or_pending_this_run=len(current)-saved, archive_unique_total=len(rows),
                   all_requested_tabs_enumerated=enumeration_ok,
                   listed_scope_complete=enumeration_ok and saved == len(current) and run['state'] == 'finished',
                   public_channel_total_verified=False,
                   completeness_note='All enumerated IDs can be accounted for; YouTube public listing completeness cannot be independently guaranteed.')
        write_json(root / 'collection_report.json', run)
        write_json(root / 'runs' / f'{run_id}.json', run)
        write_json(root / 'channel.json', dict(channel_id=args.channel_id, channel_name=run['channel_name'],
                   canonical_url=f'https://www.youtube.com/channel/{args.channel_id}', source_url=args.source_url,
                   updated_at=now()))
    print(json.dumps({k: run[k] for k in ('enumerated_unique_this_run', 'saved_for_this_run',
          'failed_or_pending_this_run', 'listed_scope_complete')}, ensure_ascii=False), flush=True)
    print(f'archive={root}', flush=True)
    return 0 if run['listed_scope_complete'] else 2

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--channel-id', required=True, help='Resolved canonical UC channel ID; never a search query')
    p.add_argument('--channel-name', default='')
    p.add_argument('--source-url', default='')
    p.add_argument('--out-dir', type=Path, required=True, help='Workspace channels folder; data stored below its UC ID')
    p.add_argument('--tabs', nargs='+', choices=TABS, default=list(TABS))
    p.add_argument('--limit', type=int, default=0, help='Test only: entries per tab. 0 means no deliberate cap')
    p.add_argument('--interval', type=float, default=1.0, help='Minimum interval for metadata and thumbnail requests')
    p.add_argument('--refresh', action='store_true', help='Recheck existing thumbnails, retaining previous image versions')
    args = p.parse_args()
    if not CHANNEL_RE.fullmatch(args.channel_id):
        p.error('--channel-id must be a UC channel ID')
    if args.limit < 0 or args.interval < 0.5:
        p.error('--limit >= 0 and --interval >= 0.5 are required')
    args.tabs = list(dict.fromkeys(args.tabs))
    return collect(args)

if __name__ == '__main__':
    raise SystemExit(main())
