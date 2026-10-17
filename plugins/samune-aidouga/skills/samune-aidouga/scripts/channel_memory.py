#!/usr/bin/env python3
"""Persistent YouTube style registry. Network access occurs only in prepare.

The assistant performs visual analysis; this helper never invents style rules.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit, unquote

CHANNEL_RE = re.compile(r"UC[A-Za-z0-9_-]{22}")
VIDEO_RE = re.compile(r"[A-Za-z0-9_-]{11}")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def data_root(value=None):
    return Path(value or os.environ.get("SAMUNE_AIDOUGA_DATA_ROOT")
                or Path.home() / ".samune-aidouga").expanduser().resolve()


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_bytes(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent,
                                     prefix=path.name + ".", delete=False) as handle:
        temp = Path(handle.name)
        handle.write(value)
    try:
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def write_json(path, value):
    write_bytes(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def registry_at(root):
    path = root / "registry.json"
    registry = read_json(path) if path.exists() else {
        "schema_version": 1, "active_channel_id": None, "channels": {}}
    if registry.get("schema_version") != 1 or not isinstance(registry.get("channels"), dict):
        raise ValueError("Unsupported registry format; existing memory was left intact")
    return registry


@contextmanager
def locked(root):
    root.mkdir(parents=True, exist_ok=True)
    path = root / ".registry.lock"
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise ValueError("Memory is in use. Check the other process before clearing .registry.lock") from exc
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(f"pid={os.getpid()} at={now()}\n")
        yield
    finally:
        path.unlink(missing_ok=True)


def normalize_url(value):
    """Return a canonical channel/video URL; reject searches and playlist-only URLs."""
    parts = urlsplit(value.strip())
    if (parts.scheme not in ("http", "https") or parts.username or parts.password
            or parts.port not in (None, 80, 443)):
        raise ValueError("YouTubeのチャンネルURLまたは動画URLを指定してください")
    host = (parts.hostname or "").lower()
    path = parts.path.rstrip("/")
    video_id = None
    if host in ("youtu.be", "www.youtu.be"):
        video_id = path.lstrip("/")
    elif host in ("youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"):
        segments = path.strip("/").split("/")
        if path == "/watch":
            video_id = parse_qs(parts.query).get("v", [""])[0]
        elif len(segments) == 2 and segments[0] in ("shorts", "live", "embed"):
            video_id = segments[1]
        else:
            if segments[-1] in ("videos", "shorts", "streams", "featured", "about"):
                segments = segments[:-1]
            if (len(segments) == 2 and segments[0] == "channel"
                    and CHANNEL_RE.fullmatch(segments[1])):
                return "https://www.youtube.com/channel/" + segments[1]
            if (len(segments) == 1 and segments[0].startswith("@") and len(segments[0]) > 1
                    or len(segments) == 2 and segments[0] in ("c", "user") and segments[1]):
                return "https://www.youtube.com/" + "/".join(segments)
    if video_id and VIDEO_RE.fullmatch(video_id):
        return "https://www.youtube.com/watch?v=" + video_id
    raise ValueError("YouTubeのチャンネルURLまたは動画URLを指定してください（再生リストは対象外）")


def url_key(url):
    normalized = unquote(normalize_url(url))
    # YouTube video IDs and UC IDs are case-sensitive; handles are not.
    return normalized.casefold() if "/@" in normalized or "/user/" in normalized else normalized


def infer_tabs(url):
    path = urlsplit(url).path.rstrip("/")
    if path.endswith("/shorts") or path.startswith("/shorts/"):
        return ["shorts"]
    if path.endswith("/streams") or path.startswith("/live/"):
        return ["streams"]
    return ["videos"]


def resolve_url(url):
    """Resolve a source channel without downloading video/audio or loading user cookies."""
    direct = re.fullmatch(r"https://www.youtube.com/channel/(UC[A-Za-z0-9_-]{22})", url)
    if direct:
        return {"channel_id": direct[1], "channel_name": "", "canonical_url": url,
                "source_url": url}
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import DownloadError
    options = {"skip_download": True, "extract_flat": True, "playlistend": 1,
               "noplaylist": True, "quiet": True, "socket_timeout": 25,
               "retries": 1, "extractor_retries": 1, "cachedir": False,
               "sleep_interval_requests": 1}
    try:
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
    except DownloadError as exc:
        raise ValueError("YouTubeから投稿元を確認できませんでした: " + str(exc)) from exc
    if not info or not CHANNEL_RE.fullmatch(info.get("channel_id") or ""):
        raise ValueError("投稿元チャンネルを確認できませんでした。チャンネルURLで再試行してください")
    channel_id = info["channel_id"]
    return {"channel_id": channel_id, "channel_name": info.get("channel") or info.get("uploader") or "",
            "canonical_url": "https://www.youtube.com/channel/" + channel_id,
            "handle_url": info.get("uploader_url"), "source_url": url,
            "reference_entry": {"id": info["id"], "title": info.get("title", ""),
                                "thumbnails": info.get("thumbnails") or []}
            if VIDEO_RE.fullmatch(info.get("id") or "") else None}


def find_channel(registry, selector):
    matches = []
    if CHANNEL_RE.fullmatch(selector):
        return selector if selector in registry["channels"] else None
    key = selector.casefold()
    if "://" in selector:
        key = url_key(selector)
    for channel_id, item in registry["channels"].items():
        names = [channel_id, item.get("channel_name", ""), *item.get("names", [])]
        urls = [url_key(url) for url in [*item.get("source_urls", []),
                item.get("canonical_url"), item.get("handle_url")] if url]
        if key in [str(name).casefold() for name in names] + urls:
            matches.append(channel_id)
    if len(matches) > 1:
        raise ValueError("同名の登録が複数あります。チャンネルIDを指定してください")
    return matches[0] if matches else None


def channel_dir(root, channel_id):
    if not CHANNEL_RE.fullmatch(channel_id):
        raise ValueError("Invalid channel ID in memory")
    return root / "channels" / channel_id


def ready(root, channel_id, item, *, snapshot=False):
    folder = channel_dir(root, channel_id)
    if snapshot:
        snapshot_id = item.get("last_snapshot", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", snapshot_id):
            return False
        profile_path = folder / "profile_history" / (snapshot_id + ".json")
        analysis_path = folder / "profile_history" / (snapshot_id + ".md")
    else:
        profile_path = folder / "channel-profile.json"
        analysis_path = folder / "analysis.md"
    if not profile_path.is_file() or not analysis_path.is_file():
        return False
    try:
        profile = read_json(profile_path)
        evidence_files = item.get("evidence_files") or []
        if not evidence_files:
            return False
        for saved in evidence_files:
            path = (folder / saved["path"]).resolve()
            if (not path.is_relative_to(folder.resolve()) or not path.is_file()
                    or hashlib.sha256(path.read_bytes()).hexdigest() != saved["sha256"]):
                return False
        return (profile.get("channel_id") == channel_id
                and profile.get("status") in ("learned", "approved")
                and item.get("profile_sha256") == hashlib.sha256(profile_path.read_bytes()).hexdigest()
                and item.get("analysis_sha256") == hashlib.sha256(analysis_path.read_bytes()).hexdigest())
    except (ValueError, OSError, KeyError, TypeError):
        return False


def describe(root, channel_id, item):
    folder = channel_dir(root, channel_id)
    return {**item, "channel_id": channel_id, "ready": ready(root, channel_id, item),
            "channel_dir": str(folder), "profile": str(folder / "channel-profile.json"),
            "analysis": str(folder / "analysis.md"), "memory_dir": str(folder / "memory")}


def status(root):
    registry = registry_at(root)
    channels = [describe(root, cid, item) for cid, item in registry["channels"].items()]
    active = next((item for item in channels if item["channel_id"] == registry["active_channel_id"]), None)
    return {"data_root": str(root), "active": active, "channels": channels,
            "next_action": "create" if active and active["ready"] else "learn_or_select"}


def activate(root, selector):
    with locked(root):
        registry = registry_at(root)
        channel_id = find_channel(registry, selector)
        if not channel_id or not ready(root, channel_id, registry["channels"][channel_id]):
            raise ValueError("再利用できる学習記録がありません。先に登録・目視分析を完了してください")
        registry["active_channel_id"] = channel_id
        write_json(root / "registry.json", registry)
    return describe(root, channel_id, registry["channels"][channel_id])


def prepare(root, url, *, update=False, refresh=False, limit=30, tabs=None):
    tabs = tabs or infer_tabs(url)
    url = normalize_url(url)
    registry = registry_at(root)
    channel_id = find_channel(registry, url)
    identity = None
    if not channel_id:
        identity = resolve_url(url)
        channel_id = identity["channel_id"]
    item = registry["channels"].get(channel_id, {})
    if (ready(root, channel_id, item) and not update and not refresh
            and not set(tabs).issubset(item.get("learned_tabs", []))):
        # Keep distinct video kinds without discarding a previously learned style.
        cached = next((saved for saved in item.get("saved_styles", {}).values()
                       if set(tabs).issubset(saved.get("learned_tabs", []))
                       and ready(root, channel_id, saved, snapshot=True)), None)
        if cached:
            with locked(root):
                registry = registry_at(root)
                item = registry["channels"][channel_id]
                folder = channel_dir(root, channel_id)
                history = folder / "profile_history" / cached["last_snapshot"]
                write_bytes(folder / "channel-profile.json", history.with_suffix(".json").read_bytes())
                write_bytes(folder / "analysis.md", history.with_suffix(".md").read_bytes())
                item.update(cached)
                write_json(root / "registry.json", registry)
    if (ready(root, channel_id, item) and not update and not refresh
            and set(tabs).issubset(item.get("learned_tabs", []))
            and (limit != 0 or item.get("full_scope_learned", False))):
        # A new video URL from an already learned channel also reuses its memory.
        with locked(root):
            registry = registry_at(root)
            item = registry["channels"][channel_id]
            item["source_urls"] = sorted(set(item.get("source_urls", [])) | {url})
            registry["active_channel_id"] = channel_id
            write_json(root / "registry.json", registry)
        return {"state": "reused", **describe(root, channel_id, item)}
    folder = channel_dir(root, channel_id)
    # An interrupted attempt stays visible and never becomes active merely by registration.
    with locked(root):
        registry = registry_at(root)
        item = registry["channels"].setdefault(channel_id, {})
        if identity:
            item.update({k: v for k, v in identity.items() if k not in ("source_url", "reference_entry") and v})
        item["source_urls"] = sorted(set(item.get("source_urls", [])) | {url})
        item.setdefault("created_at", now())
        item["last_collection_requested_at"] = now()
        write_json(root / "registry.json", registry)
    from collect_channel import collect
    from prepare_analysis import prepare as make_gallery
    args = SimpleNamespace(channel_id=channel_id, channel_name=item.get("channel_name", ""),
                           source_url=url, out_dir=root / "channels", tabs=tabs,
                           limit=limit, interval=1.0, refresh=refresh,
                           reference_entry=(identity or {}).get("reference_entry"))
    # On retries the saved URL may already resolve offline; keep the exact video as evidence.
    if not args.reference_entry and "/watch?" in url:
        args.reference_entry = {"id": parse_qs(urlsplit(url).query)["v"][0], "title": ""}
    code = collect(args)
    analysis = make_gallery(folder)
    report = read_json(folder / "collection_report.json")
    with locked(root):
        registry = registry_at(root)
        item = registry["channels"][channel_id]
        item.update(channel_name=report.get("channel_name") or item.get("channel_name", channel_id),
                    pending_analysis=str(analysis.relative_to(root)), last_collection_code=code)
        write_json(root / "registry.json", registry)
    return {"state": "needs_visual_analysis", **describe(root, channel_id, item),
            "analysis_dir": str(analysis), "collection_report": report}


def finalize(root, selector):
    """Validate evidence before marking a style reusable. Does not perform visual review."""
    with locked(root):
        registry = registry_at(root)
        channel_id = find_channel(registry, selector)
        if not channel_id:
            raise ValueError("登録チャンネルが見つかりません")
        folder = channel_dir(root, channel_id)
        profile_path = folder / "channel-profile.json"
        profile = read_json(profile_path)
        evidence = profile.get("evidence") or {}
        if profile.get("channel_id") != channel_id or profile.get("status") not in ("learned", "approved"):
            raise ValueError("対象IDと learned / approved のプロファイルが必要です")
        if not profile.get("observed_patterns") or not profile.get("production_defaults"):
            raise ValueError("観察した特徴と制作初期値を保存してください")
        observation_path = (folder / str(evidence.get("observation_file") or "")).resolve()
        if not observation_path.is_relative_to(folder.resolve()) or not observation_path.is_file():
            raise ValueError("チャンネル内の observations.csv を指定してください")
        import csv
        with observation_path.open(encoding="utf-8-sig", newline="") as handle:
            observations = [row for row in csv.DictReader(handle)
                            if row.get("inspected", "").lower() in ("yes", "true", "1")]
        ids = [row["video_id"] for row in observations]
        from prepare_analysis import VISUAL_FIELDS
        if any(not row.get(field, "").strip() for row in observations for field in VISUAL_FIELDS[:-1]):
            raise ValueError("目視した画像の特徴を observations.csv へ記入してください")
        refs = evidence.get("reference_video_ids") or []
        if (not ids or len(set(ids)) != len(ids) or evidence.get("inspected_count") != len(ids)
                or not refs or not set(refs).issubset(ids)):
            raise ValueError("目視件数・重複・根拠動画IDを observations.csv と一致させてください")
        from collect_channel import read_records, valid_cached
        manifest = read_records(folder / "manifest.jsonl")
        if any(vid not in manifest or not valid_cached(folder, manifest[vid]) for vid in ids):
            raise ValueError("観察元の保存画像が不足または破損しています")
        run_id = profile.get("collection_run_id")
        if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
            raise ValueError("収集実行IDをプロファイルへ記録してください")
        report = read_json(folder / "runs" / (run_id + ".json"))
        if (report.get("channel_id") != channel_id
                or evidence.get("enumerated_count") != report.get("enumerated_unique_this_run")
                or evidence.get("saved_count") != report.get("saved_for_this_run")
                or any(manifest[vid].get("last_seen_run") != run_id for vid in ids)):
            raise ValueError("根拠画像と件数を収集実行IDのレポートに対応させてください")
        if (report.get("limit_per_tab") or not report.get("listed_scope_complete")) and not evidence.get("limitations"):
            raise ValueError("限定取得・未完了の範囲を evidence.limitations に記録してください")
        analysis_path = folder / "analysis.md"
        if not analysis_path.is_file() or not analysis_path.read_text(encoding="utf-8").strip():
            raise ValueError("根拠と制作ルールを記した analysis.md が必要です")
        item = registry["channels"][channel_id]
        digest = hashlib.sha256(profile_path.read_bytes()).hexdigest()
        history_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "_" + digest[:12]
        # Keep exact bytes so the saved snapshot has the same signature as the active profile.
        history_dir = folder / "profile_history"
        history_dir.mkdir(parents=True, exist_ok=True)
        (history_dir / (history_id + ".json")).write_bytes(profile_path.read_bytes())
        (folder / "profile_history" / (history_id + ".md")).write_bytes(analysis_path.read_bytes())
        evidence_files = [{"path": str(observation_path.relative_to(folder.resolve())),
                           "sha256": hashlib.sha256(observation_path.read_bytes()).hexdigest()}]
        evidence_files.extend({"path": manifest[vid]["thumbnail_path"], "sha256": manifest[vid]["sha256"]}
                              for vid in dict.fromkeys(refs))
        learned_tabs = sorted({tab for vid in ids for tab in manifest[vid].get("tabs", [])})
        item.update(channel_name=profile.get("channel_name") or item.get("channel_name", channel_id),
                    learned_at=now(), profile_sha256=digest, last_snapshot=history_id,
                    analysis_sha256=hashlib.sha256(analysis_path.read_bytes()).hexdigest(),
                    evidence_files=evidence_files, learned_tabs=learned_tabs,
                    full_scope_learned=report.get("listed_scope_complete") is True)
        style_key = "+".join(learned_tabs) or "unknown"
        item.setdefault("saved_styles", {})[style_key] = {
            key: item[key] for key in ("learned_at", "profile_sha256", "last_snapshot",
                                      "analysis_sha256", "evidence_files", "learned_tabs", "full_scope_learned")}
        item.pop("pending_analysis", None)
        registry["active_channel_id"] = channel_id
        write_json(root / "registry.json", registry)
    return {"state": "learned", **describe(root, channel_id, item)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", help="Default: SAMUNE_AIDOUGA_DATA_ROOT or ~/.samune-aidouga")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="Offline: list saved styles and the active channel")
    select = commands.add_parser("select", help="Offline: select a learned style by name, ID or saved URL")
    select.add_argument("channel")
    learn = commands.add_parser("prepare", help="Collect references only when learning is needed")
    learn.add_argument("url")
    learn.add_argument("--update", action="store_true")
    learn.add_argument("--refresh", action="store_true", help="Also recheck cached thumbnails")
    learn.add_argument("--limit", type=int, default=30, help="Recent entries per tab; 0 = all")
    learn.add_argument("--tabs", nargs="+", choices=("videos", "shorts", "streams"),
                       help="Default: infer from URL; otherwise videos")
    finish = commands.add_parser("finalize", help="Validate completed visual learning and remember it")
    finish.add_argument("channel")
    args = parser.parse_args()
    try:
        root = data_root(args.data_root)
        if args.command == "status":
            result = status(root)
        elif args.command == "select":
            result = activate(root, args.channel)
        elif args.command == "finalize":
            result = finalize(root, args.channel)
        else:
            if args.limit < 0:
                parser.error("--limit must be >= 0")
            result = prepare(root, args.url, update=args.update, refresh=args.refresh,
                             limit=args.limit, tabs=list(dict.fromkeys(args.tabs)) if args.tabs else None)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, OSError, ImportError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
