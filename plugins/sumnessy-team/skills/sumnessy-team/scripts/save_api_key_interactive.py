#!/usr/bin/env python3
"""User-operated, hidden terminal input after hosted key creation is unavailable.

Never pass a secret through command arguments, chat, a log, or agent tool output.
The destination must already have been approved by the user. This helper creates
a new env file only; it never replaces an existing file or follows symlinks.
"""
import argparse
import getpass
import os
from pathlib import Path
import re
import sys


def save_new_key(target, key):
    if not re.fullmatch(r"sk-[A-Za-z0-9_-]{20,}", key):
        raise ValueError("APIキーの形式を確認してください。入力内容は保存していません。")
    if target.parent.is_symlink() or target.is_symlink():
        raise ValueError("リンク先への保存は行いません。")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(target, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write("OPENAI_API_KEY=" + key + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, required=True)
    args = parser.parse_args()
    if not sys.stdin.isatty():
        print("本人が操作するターミナルで実行してください。")
        return 2
    target = args.target.absolute()
    if target.exists() or target.is_symlink():
        print("保存先が既に存在するため、上書きせず停止しました。")
        return 2
    print("OpenAI Platformで作成したキーを貼り付けてEnterを押してください。")
    print("入力した文字は画面に表示されません。チャットには貼らないでください。")
    print("保存先: " + str(target))
    try:
        key = getpass.getpass("APIキー（非表示入力）: ")
        save_new_key(target, key.strip())
        key = None
    except (KeyboardInterrupt, EOFError):
        print("\n保存を中止しました。")
        return 2
    except ValueError as exc:
        print(str(exc))
        return 2
    except OSError:
        print("保存できませんでした。キーは表示しません。保存先を確認してください。")
        return 2
    print("保存しました。Codexに「保存済み」と伝えてください。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
