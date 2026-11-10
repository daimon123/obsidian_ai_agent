"""검색·유사 문서·미완료 작업 중복 묶음."""
from __future__ import annotations

import datetime as dt
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from .index import Index
from .parser import query_terms, split_frontmatter, words

STOP = {"the", "and", "for", "with", "that", "this", "from", "are", "was", "not", "you", "have",
        "있다", "하는", "합니다", "그리고", "있는", "대한", "에서", "으로", "하고"}


def _snippet(root: Path, rel: str, needles: list[str], width: int = 160) -> str:
    try:
        text = split_frontmatter((root / rel).read_text(encoding="utf-8-sig", errors="replace"))[1]
    except OSError:
        return ""
    low = text.lower()
    pos = -1
    for n in needles:
        pos = low.find(n)
        if pos >= 0:
            break
    if pos < 0:
        return re.sub(r"\s+", " ", text[:width]).strip()
    s = max(0, pos - width // 3)
    return re.sub(r"\s+", " ", text[s:s + width]).strip()


def search(idx: Index, query: str, limit: int = 10, folder: str = "", type_: str | None = None) -> list[dict]:
    terms = query_terms(query)
    if not terms:
        return []
    needles = [w for w in words(query) if w]
    rows = _run(idx, " ".join(terms), limit, folder, type_)          # AND
    if not rows and len(terms) > 1:
        rows = _run(idx, " OR ".join(terms), limit, folder, type_)   # OR 대체
    return [{"path": r["path"], "title": r["title"], "type": r["type"],
             "score": round(-r["rank"], 3), "snippet": _snippet(idx.root, r["path"], needles)} for r in rows]


def _run(idx: Index, match: str, limit: int, folder: str, type_: str | None):
    sql = ("SELECT n.path, n.title, n.type, bm25(notes_fts, 5.0, 1.0) AS rank "
           "FROM notes_fts JOIN notes n ON n.id = notes_fts.rowid WHERE notes_fts MATCH ?")
    args: list = [match]
    if folder:
        sql += " AND n.path LIKE ? ESCAPE '\\'"
        args.append(folder.replace("\\", "/").rstrip("/").replace("%", r"\%").replace("_", r"\_") + "/%")
    if type_:
        sql += " AND n.type = ?"
        args.append(type_)
    sql += " ORDER BY rank LIMIT ?"
    args.append(int(limit))
    return idx.db.execute(sql, args).fetchall()


def find_similar(idx: Index, rel: str, limit: int = 10) -> list[dict]:
    row = idx.db.execute("SELECT id,tags,type FROM notes WHERE path=?", (rel,)).fetchone()
    if not row:
        return []
    text = split_frontmatter((idx.root / rel).read_text(encoding="utf-8-sig", errors="replace"))[1]
    cnt = Counter(w for w in words(text) if len(w) >= 2 and w not in STOP and not w.isdigit())
    top = [w for w, _ in cnt.most_common(12)]
    my_tags = set(json.loads(row["tags"] or "[]"))
    terms = query_terms(" ".join(top)) or []
    if not terms:
        return []
    rows = idx.db.execute(
        "SELECT n.path, n.title, n.type, n.tags, bm25(notes_fts, 5.0, 1.0) AS rank "
        "FROM notes_fts JOIN notes n ON n.id = notes_fts.rowid WHERE notes_fts MATCH ? "
        "AND n.path != ? AND n.daily_date IS NULL ORDER BY rank LIMIT ?",
        (" OR ".join(terms), rel, limit * 3)).fetchall()
    out = []
    for r in rows:
        shared = sorted(my_tags & set(json.loads(r["tags"] or "[]")))
        out.append({"path": r["path"], "title": r["title"], "type": r["type"],
                    "score": round(-r["rank"] + 2.0 * len(shared), 3), "shared_tags": shared})
    out.sort(key=lambda x: -x["score"])
    return out[:limit]


def find_tasks(idx: Index, since: str | None = None, until: str | None = None,
               tags: list[str] | None = None, status: str = "open",
               include_occurrences: bool = False, days: int = 30, today: str | None = None) -> list[dict]:
    """Daily Note 의 작업을 날짜별 복사본까지 묶어서 돌려준다.

    규칙: 같은 정규화 문자열(norm)은 한 작업이다. 가장 최근 등장분이 완료면 해결된 작업,
    아니면 미완료다. `ever_done`은 한 번이라도 체크된 적이 있다는 표시.
    """
    end = until or today or dt.date.today().isoformat()
    start = since or (dt.date.fromisoformat(end) - dt.timedelta(days=days)).isoformat()
    rows = idx.db.execute(
        "SELECT path,line,status,text,norm,tags,due,daily_date FROM tasks "
        "WHERE daily_date IS NOT NULL AND daily_date BETWEEN ? AND ? ORDER BY daily_date, path, line",
        (start, end)).fetchall()
    want = {t.lstrip("#") for t in (tags or [])}
    groups: dict[str, list] = defaultdict(list)
    for r in rows:
        if r["status"] in ("cancelled", "other"):
            continue
        groups[r["norm"]].append(r)

    out = []
    for norm, occ in groups.items():
        last = occ[-1]
        cur = "done" if last["status"] == "done" else "open"
        if status != "all" and cur != status:
            continue
        gtags = sorted({t for o in occ for t in json.loads(o["tags"] or "[]")})
        if want and not (want & set(gtags)):
            continue
        item = {
            "text": last["text"], "status": cur,
            "first_seen": occ[0]["daily_date"], "last_seen": last["daily_date"],
            "count": len(occ), "ever_done": any(o["status"] == "done" for o in occ),
            "origin": f'{occ[0]["path"]}:{occ[0]["line"]}',
            "latest": f'{last["path"]}:{last["line"]}',
            "tags": gtags, "due": next((o["due"] for o in reversed(occ) if o["due"]), None),
        }
        if include_occurrences:
            item["occurrences"] = [f'{o["path"]}:{o["line"]}' for o in occ]
        out.append(item)
    out.sort(key=lambda x: (x["first_seen"], x["origin"]))
    return out
