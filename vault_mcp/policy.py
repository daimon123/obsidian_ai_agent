"""접근 정책: denylist(색인·읽기·전송 모두 금지), 색인 제외, 시스템 폴더.

문서 단위 제외(frontmatter `ai: false`)는 본문을 읽어야 알 수 있으므로 index.py 가 처리한다.
"""
from __future__ import annotations

from .config import Config

SYSTEM_DIRS = {".obsidian", ".git", ".trash", ".restructure-log"}


def norm_rel(rel: str) -> str:
    return rel.replace("\\", "/").strip("/")


def _under(rel: str, prefix: str) -> bool:
    rel, prefix = norm_rel(rel).lower(), norm_rel(prefix).lower()
    return rel == prefix or rel.startswith(prefix + "/")


class Policy:
    def __init__(self, cfg: Config):
        self.denylist = [norm_rel(p) for p in cfg.denylist]
        self.index_exclude = [norm_rel(p) for p in cfg.index_exclude]

    def is_system(self, rel: str) -> bool:
        return any(part.lower() in SYSTEM_DIRS for part in norm_rel(rel).split("/"))

    def is_denied(self, rel: str) -> bool:
        """denylist 또는 시스템 폴더: 내용을 열지 않는다."""
        return self.is_system(rel) or any(_under(rel, p) for p in self.denylist)

    def is_index_excluded(self, rel: str) -> bool:
        return any(_under(rel, p) for p in self.index_exclude)
