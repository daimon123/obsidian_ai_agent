"""SQLite(FTS5) 색인과 증분 동기화. denylist·`ai: false` 문서는 색인에 들어가지 않는다."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path

from .config import Config
from .parser import DATE_IN_NAME, index_tokens, parse_note
from .policy import Policy, norm_rel

SCHEMA = """
CREATE TABLE IF NOT EXISTS notes(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  path TEXT UNIQUE NOT NULL, title TEXT, mtime REAL, size INTEGER, sha1 TEXT,
  type TEXT, status TEXT, tags TEXT, daily_date TEXT, frontmatter TEXT);
CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(title, body, path UNINDEXED);
CREATE TABLE IF NOT EXISTS links(src TEXT, target TEXT, embed INTEGER, kind TEXT, resolved TEXT);
CREATE INDEX IF NOT EXISTS links_src ON links(src);
CREATE INDEX IF NOT EXISTS links_res ON links(resolved);
CREATE TABLE IF NOT EXISTS tasks(
  path TEXT, line INTEGER, status TEXT, text TEXT, norm TEXT, tags TEXT, due TEXT, daily_date TEXT);
CREATE INDEX IF NOT EXISTS tasks_norm ON tasks(norm);
CREATE TABLE IF NOT EXISTS blocked(path TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""


class Index:
    def __init__(self, cfg: Config, policy: Policy):
        self.cfg, self.policy = cfg, policy
        self.root = Path(cfg.vault_path)
        cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(cfg.db_path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    # ---- 파일 목록 --------------------------------------------------------
    def _walk(self):
        """(rel, abs) 전체 파일. 시스템 폴더는 내려가지 않는다."""
        for dirpath, dirs, files in os.walk(self.root):
            dirs[:] = [d for d in dirs if not self.policy.is_system(d)]
            for f in files:
                ap = Path(dirpath) / f
                yield norm_rel(str(ap.relative_to(self.root))), ap

    def sync(self) -> dict:
        names_md: dict[str, list[str]] = {}   # stem.lower() -> [rel]
        names_file: dict[str, str] = {}       # filename.lower() -> rel (비 md)
        rels_md: set[str] = set()             # rel 경로(소문자, .md 제거)
        restricted: set[str] = set()
        candidates: list[tuple[str, Path]] = []

        for rel, ap in self._walk():
            low = rel.lower()
            stem = Path(rel).stem.lower()
            if self.policy.is_denied(rel):
                restricted.add(stem)
                restricted.add(Path(rel).name.lower())
                continue
            if low.endswith(".md"):
                names_md.setdefault(stem, []).append(rel)
                rels_md.add(low[:-3])
                if not self.policy.is_index_excluded(rel):
                    candidates.append((rel, ap))
            else:
                names_file.setdefault(Path(rel).name.lower(), rel)

        existing = {r["path"]: (r["mtime"], r["size"]) for r in self.db.execute("SELECT path,mtime,size FROM notes")}
        was_blocked = {r["path"] for r in self.db.execute("SELECT path FROM blocked")}
        seen: set[str] = set()
        added = updated = skipped = blocked_n = 0

        for rel, ap in candidates:
            st = ap.stat()
            seen.add(rel)
            if existing.get(rel) == (st.st_mtime, st.st_size) or (rel in was_blocked and
                    self._blocked_unchanged(rel, st)):
                skipped += 1
                continue
            raw = ap.read_bytes()
            text = raw.decode("utf-8-sig", errors="replace")
            note = parse_note(text)
            self._remove(rel)
            if note.ai_blocked:
                self.db.execute("INSERT OR REPLACE INTO blocked(path) VALUES(?)", (rel,))
                self.db.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)",
                                (f"blocked:{rel}", f"{st.st_mtime}:{st.st_size}"))
                blocked_n += 1
                continue
            self.db.execute("DELETE FROM blocked WHERE path=?", (rel,))
            title = Path(rel).stem
            fm = note.frontmatter
            m = DATE_IN_NAME.match(title)
            daily = m.group(1) if (m and rel.startswith("00. Daily/")) else None
            cur = self.db.execute(
                "INSERT INTO notes(path,title,mtime,size,sha1,type,status,tags,daily_date,frontmatter)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (rel, title, st.st_mtime, st.st_size, hashlib.sha1(raw).hexdigest(),
                 str(fm.get("type")) if fm.get("type") else None,
                 str(fm.get("status")) if fm.get("status") else None,
                 json.dumps(note.tags, ensure_ascii=False), daily,
                 json.dumps(fm, ensure_ascii=False)))
            nid = cur.lastrowid
            self.db.execute("INSERT INTO notes_fts(rowid,title,body,path) VALUES(?,?,?,?)",
                            (nid, index_tokens(title), index_tokens(note.body), rel))
            self.db.executemany(
                "INSERT INTO links(src,target,embed,kind,resolved) VALUES(?,?,?,?,NULL)",
                [(rel, l.target, int(l.embed), l.kind) for l in note.links])
            self.db.executemany(
                "INSERT INTO tasks(path,line,status,text,norm,tags,due,daily_date) VALUES(?,?,?,?,?,?,?,?)",
                [(rel, t.line, t.status, t.text, t.norm, json.dumps(t.tags, ensure_ascii=False), t.due, daily)
                 for t in note.tasks])
            if rel in existing:
                updated += 1
            else:
                added += 1

        removed = 0
        for rel in set(existing) - seen:
            self._remove(rel)
            removed += 1
        for rel in was_blocked - seen:
            self.db.execute("DELETE FROM blocked WHERE path=?", (rel,))
            self.db.execute("DELETE FROM meta WHERE k=?", (f"blocked:{rel}",))

        self._resolve_links(names_md, names_file, rels_md, restricted)
        self.db.commit()
        nblk = self.db.execute("SELECT COUNT(*) FROM blocked").fetchone()[0]
        return {"added": added, "updated": updated, "removed": removed,
                "skipped": skipped, "ai_blocked": nblk}

    # ---- 내부 -------------------------------------------------------------
    def _is_blocked(self, rel: str) -> bool:
        return self.db.execute("SELECT 1 FROM blocked WHERE path=?", (rel,)).fetchone() is not None

    def _blocked_unchanged(self, rel: str, st) -> bool:
        r = self.db.execute("SELECT v FROM meta WHERE k=?", (f"blocked:{rel}",)).fetchone()
        return bool(r) and r["v"] == f"{st.st_mtime}:{st.st_size}"

    def _remove(self, rel: str) -> None:
        r = self.db.execute("SELECT id FROM notes WHERE path=?", (rel,)).fetchone()
        if r:
            self.db.execute("DELETE FROM notes_fts WHERE rowid=?", (r["id"],))
            self.db.execute("DELETE FROM notes WHERE id=?", (r["id"],))
        self.db.execute("DELETE FROM links WHERE src=?", (rel,))
        self.db.execute("DELETE FROM tasks WHERE path=?", (rel,))

    def _resolve_links(self, names_md, names_file, rels_md, restricted) -> None:
        rows = self.db.execute("SELECT rowid, src, target FROM links").fetchall()
        updates = []
        for r in rows:
            t = r["target"].replace("\\", "/")
            low = t.lower()
            if low.endswith(".md"):
                low = low[:-3]
            res = ""
            if "/" in low:
                if low in rels_md:
                    res = low  # 아래에서 원래 경로로 복원
                    for cands in names_md.get(Path(low).name, []):
                        if cands.lower()[:-3] == low:
                            res = cands
                            break
                else:  # 접미 경로 일치 (폴더 일부만 쓴 경우)
                    for cands in names_md.get(Path(low).name, []):
                        if cands.lower()[:-3].endswith("/" + low):
                            res = cands
                            break
            else:
                c = sorted(names_md.get(low, []))
                if c:
                    res = c[0]
            if not res:
                f = names_file.get(Path(t).name.lower())
                if f:
                    res = f
            if not res and (Path(low).name in restricted or Path(t).name.lower() in restricted):
                res = "__restricted__"
            updates.append((res, r["rowid"]))
        self.db.executemany("UPDATE links SET resolved=? WHERE rowid=?", updates)
