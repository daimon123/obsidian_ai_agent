"""MCP 없이 도구를 점검하는 CLI. 예: python -m vault_mcp.cli search 쿠버네티스"""
from __future__ import annotations

import argparse
import json
import sys

from .change_store import ChangeError
from .vault import Vault, VaultError


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="vault-mcp-cli")
    ap.add_argument("--config", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("sync")
    sub.add_parser("stats")
    p = sub.add_parser("search"); p.add_argument("query"); p.add_argument("--limit", type=int, default=10); p.add_argument("--folder", default="")
    p = sub.add_parser("note"); p.add_argument("path"); p.add_argument("--max-chars", type=int, default=2000)
    p = sub.add_parser("links"); p.add_argument("path")
    p = sub.add_parser("similar"); p.add_argument("path"); p.add_argument("--limit", type=int, default=10)
    p = sub.add_parser("tasks"); p.add_argument("--days", type=int, default=30); p.add_argument("--today"); p.add_argument("--tag", action="append"); p.add_argument("--status", default="open")
    p = sub.add_parser("changes"); p.add_argument("--limit", type=int, default=20)
    p = sub.add_parser("prune"); p.add_argument("--days", type=int, default=90); p.add_argument("--max-mb", type=int, default=500)
    a = ap.parse_args(argv)
    try:
        v = Vault.from_config(a.config)
        if a.cmd == "sync":
            out = v.refresh(force=True)
        elif a.cmd == "stats":
            out = v.vault_stats()
        elif a.cmd == "search":
            out = v.search(a.query, a.limit, a.folder)
        elif a.cmd == "note":
            out = v.read_note(a.path, a.max_chars)
        elif a.cmd == "links":
            out = v.get_links(a.path)
        elif a.cmd == "similar":
            out = v.find_similar(a.path, a.limit)
        elif a.cmd == "changes":
            out = v.list_changes(a.limit)
        elif a.cmd == "prune":
            out = v.changes.prune(a.days, a.max_mb)
        else:
            out = v.find_tasks(days=a.days, today=a.today, tags=a.tag, status=a.status)
    except (VaultError, ChangeError) as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False)); return 1
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
