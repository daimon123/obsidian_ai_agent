"""설정 로드. 환경 변수 VAULT_MCP_CONFIG / VAULT_MCP_VAULT / VAULT_MCP_DB 로 덮어쓸 수 있다."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_EXCLUDE = [
    "100.Canvas", "200.Excalidraw", "998.Template", "999.Attached",
    ".restructure-log", ".obsidian", "sortspec.md",
]


@dataclass
class Config:
    vault_path: Path
    denylist: list[str] = field(default_factory=lambda: ["04. Resource/Personal"])
    index_exclude: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDE))
    db_path: Path = Path("data/index.sqlite")
    change_store: Path = Path("data/changes")
    writer: bool = False
    refresh_interval_sec: float = 30.0


def load_config(path: str | os.PathLike | None = None) -> Config:
    path = Path(path or os.environ.get("VAULT_MCP_CONFIG") or "config/vault.local.yaml").resolve()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    root = path.parent.parent  # config/ 의 상위 = 저장소 루트

    def rel(p: str) -> Path:
        p = Path(p)
        return p if p.is_absolute() else (root / p)

    vault = os.environ.get("VAULT_MCP_VAULT") or raw.get("vault_path")
    if not vault:
        raise ValueError("vault_path 가 설정되지 않았습니다")
    db = os.environ.get("VAULT_MCP_DB") or raw.get("index_db", "data/index.sqlite")
    return Config(
        vault_path=Path(vault),
        denylist=list(raw.get("denylist", ["04. Resource/Personal"])),
        index_exclude=list(raw.get("index_exclude", DEFAULT_EXCLUDE)),
        db_path=rel(db),
        change_store=rel(raw.get("change_store", "data/changes")),
        writer=bool(raw.get("writer", False)),
        refresh_interval_sec=float(raw.get("refresh_interval_sec", 30)),
    )
