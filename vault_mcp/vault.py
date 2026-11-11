"""읽기 전용 Vault API. MCP 서버와 CLI 가 공통으로 사용한다."""
from __future__ import annotations

import json
import time
from collections import Counter
from pathlib import Path

from . import search as S
from .change_store import ChangeError, ChangeStore
from .config import Config, load_config
from .index import Index
from .parser import parse_note, split_frontmatter
from .policy import Policy, norm_rel


class VaultError(Exception):
    pass


class Vault:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.policy = Policy(cfg)
        self.idx = Index(cfg, self.policy)
        self._last_sync = 0.0
        try:
            self.changes = ChangeStore(cfg, self.policy, self.idx)
        except Exception:
            self.idx.close()   # 생성 실패 시 DB 연결을 남기지 않는다 (Windows 파일 잠금)
            raise

    @classmethod
    def from_config(cls, path=None) -> "Vault":
        return cls(load_config(path))

    def close(self) -> None:
        self.idx.close()

    # ---- 색인 -------------------------------------------------------------
    def refresh(self, force: bool = False) -> dict | None:
        if force or time.time() - self._last_sync >= self.cfg.refresh_interval_sec:
            r = self.idx.sync()
            self._last_sync = time.time()
            return r
        return None

    # ---- 경로 검증 --------------------------------------------------------
    def _safe(self, rel: str) -> str:
        rel = norm_rel(rel)
        if not rel or ".." in rel.split("/") or Path(rel).is_absolute() or ":" in rel.split("/")[0]:
            raise VaultError("잘못된 경로입니다")
        if not rel.lower().endswith(".md"):
            rel += ".md"
        if self.policy.is_denied(rel):
            raise VaultError("접근이 제한된 경로입니다 (denylist)")
        ap = (self.cfg.vault_path / rel).resolve()
        if self.cfg.vault_path.resolve() not in ap.parents:
            raise VaultError("Vault 밖의 경로입니다")
        if self.idx._is_blocked(rel):
            raise VaultError("문서 단위로 제외된 문서입니다 (ai: false)")
        return rel

    # ---- 도구 -------------------------------------------------------------
    def read_note(self, path: str, max_chars: int = 20000) -> dict:
        self.refresh()
        rel = self._safe(path)
        ap = self.cfg.vault_path / rel
        if not ap.is_file():
            raise VaultError(f"문서를 찾을 수 없습니다: {rel}")
        text = ap.read_text(encoding="utf-8-sig", errors="replace")
        if parse_note(text).ai_blocked:   # 색인 이후에 ai: false 가 추가된 경우
            raise VaultError("문서 단위로 제외된 문서입니다 (ai: false)")
        fm, body = split_frontmatter(text)
        return {"path": rel, "frontmatter": fm, "content": body[:max_chars],
                "truncated": len(body) > max_chars, "chars": len(body)}

    def get_frontmatter(self, path: str) -> dict:
        r = self.read_note(path, max_chars=0)
        return {"path": r["path"], "frontmatter": r["frontmatter"]}

    def list_notes(self, folder: str = "", type_: str | None = None, limit: int = 50, offset: int = 0) -> dict:
        self.refresh()
        sql, args = "FROM notes WHERE 1=1", []
        if folder:
            folder = norm_rel(folder)
            if self.policy.is_denied(folder):
                raise VaultError("접근이 제한된 경로입니다 (denylist)")
            sql += " AND path LIKE ? ESCAPE '\\'"
            args.append(folder.replace("%", r"\%").replace("_", r"\_") + "/%")
        if type_:
            sql += " AND type=?"
            args.append(type_)
        total = self.idx.db.execute("SELECT COUNT(*) " + sql, args).fetchone()[0]
        rows = self.idx.db.execute(
            "SELECT path,title,type,status,daily_date " + sql + " ORDER BY path LIMIT ? OFFSET ?",
            args + [min(int(limit), 500), int(offset)]).fetchall()
        return {"total": total, "notes": [dict(r) for r in rows]}

    def get_links(self, path: str) -> dict:
        self.refresh()
        rel = self._safe(path)
        db = self.idx.db
        out = [dict(target=r["target"], resolved=r["resolved"] or None, embed=bool(r["embed"]))
               for r in db.execute("SELECT target,resolved,embed FROM links WHERE src=?", (rel,))]
        for o in out:
            if o["resolved"] == "__restricted__":
                o["resolved"] = None
                o["restricted"] = True
        inc = [r["src"] for r in db.execute(
            "SELECT DISTINCT src FROM links WHERE resolved=? ORDER BY src", (rel,))]
        return {"path": rel, "outgoing": out, "incoming": inc,
                "unresolved": [o["target"] for o in out if not o["resolved"] and not o.get("restricted")]}

    def search(self, query: str, limit: int = 10, folder: str = "", type_: str | None = None) -> list[dict]:
        self.refresh()
        if folder and self.policy.is_denied(folder):
            raise VaultError("접근이 제한된 경로입니다 (denylist)")
        return S.search(self.idx, query, min(int(limit), 50), norm_rel(folder), type_)

    def find_similar(self, path: str, limit: int = 10) -> list[dict]:
        self.refresh()
        return S.find_similar(self.idx, self._safe(path), min(int(limit), 30))

    def find_tasks(self, **kw) -> list[dict]:
        self.refresh()
        return S.find_tasks(self.idx, **kw)

    def vault_stats(self, sample: int = 20) -> dict:
        self.refresh()
        db = self.idx.db
        total = db.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
        by_type = {(r[0] or "(없음)"): r[1] for r in db.execute("SELECT type,COUNT(*) FROM notes GROUP BY type")}
        by_top = Counter(r[0].split("/")[0] for r in db.execute("SELECT path FROM notes"))
        empty = [r[0] for r in db.execute("SELECT path FROM notes WHERE size=0 LIMIT ?", (sample,))]
        n_empty = db.execute("SELECT COUNT(*) FROM notes WHERE size=0").fetchone()[0]
        broken = db.execute(
            "SELECT src,target FROM links WHERE (resolved IS NULL OR resolved='')").fetchall()
        orphans = db.execute(
            "SELECT path FROM notes n WHERE daily_date IS NULL AND COALESCE(type,'')!='moc' "
            "AND NOT EXISTS (SELECT 1 FROM links l WHERE l.resolved=n.path) ORDER BY path").fetchall()
        open_tasks = db.execute(
            "SELECT COUNT(*) FROM tasks WHERE status='open' AND daily_date IS NOT NULL").fetchone()[0]
        return {
            "notes": total, "by_type": by_type, "by_top_folder": dict(by_top),
            "ai_blocked_notes": db.execute("SELECT COUNT(*) FROM blocked").fetchone()[0],
            "empty_notes": {"count": n_empty, "sample": empty},
            "broken_links": {"count": len(broken), "sample": [dict(src=b["src"], target=b["target"]) for b in broken[:sample]]},
            "orphan_notes": {"count": len(orphans), "sample": [o[0] for o in orphans[:sample]]},
            "daily_open_task_rows": open_tasks,
        }

    # ---- 변경 (쓰기는 항상 plan -> apply, config writer: true 일 때만) -----------
    def plan_change(self, ops: list[dict], command: str = "", summary: str = "") -> dict:
        self.refresh(force=True)
        return self.changes.plan_change(ops, command, summary)

    def apply_change(self, plan_id: str, approval: str = "manual") -> dict:
        r = self.changes.apply_change(plan_id, approval)
        self.refresh(force=True)
        return r

    def revert_change(self, plan_id: str) -> dict:
        r = self.changes.revert_change(plan_id)
        self.refresh(force=True)
        return r

    def list_changes(self, limit: int = 20, since: str | None = None) -> list[dict]:
        return self.changes.list_changes(limit, since)
