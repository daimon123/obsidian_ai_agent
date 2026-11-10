"""Markdown 파싱: frontmatter, 링크, 태그, Tasks 플러그인 문법, 한글 토크나이저."""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote

import yaml

FM_RE = re.compile(r"\A﻿?---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)
FENCE_RE = re.compile(r"^(```|~~~).*?^\1[ \t]*$", re.S | re.M)
INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
WIKI_RE = re.compile(r"(!?)\[\[([^\[\]\n]+?)\]\]")
MDLINK_RE = re.compile(r"(!?)\[[^\]\n]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
TAG_RE = re.compile(r"(?<![\w/#&\]\)])#([A-Za-z0-9_\-/가-힣]*[A-Za-z_\-/가-힣][A-Za-z0-9_\-/가-힣]*)")
TASK_RE = re.compile(r"^[ \t>]*[-*+][ \t]+\[(.)\][ \t]+(.*\S)[ \t]*$")
DATE_IN_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2})")

_EMOJI_META = re.compile(
    r"(?:📅|⏳|🛫|✅|➕|❌)\s*\d{4}-\d{2}-\d{2}|[⏫🔼🔽⏬🔺]|🔁[^📅⏳🛫✅➕❌#]*|🆔\s*\S+|⛔\s*\S+"
)
_DUE_RE = re.compile(r"📅\s*(\d{4}-\d{2}-\d{2})")
_WORD_RE = re.compile(r"[가-힣]+|[^\W_가-힣]+")
_HANGUL = re.compile(r"^[가-힣]+$")


@dataclass
class Link:
    target: str
    embed: bool
    kind: str  # wiki | md


@dataclass
class Task:
    line: int
    status: str   # open | done | cancelled | other
    text: str     # 원문 (체크박스 제외)
    norm: str     # 중복 판별용 정규화 문자열
    tags: list[str]
    due: str | None


@dataclass
class Note:
    frontmatter: dict[str, Any] = field(default_factory=dict)
    body: str = ""
    links: list[Link] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    tasks: list[Task] = field(default_factory=list)
    ai_blocked: bool = False


def _jsonable(v: Any) -> Any:
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, set)):
        return [_jsonable(x) for x in v]
    return v


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    m = FM_RE.match(text)
    if not m:
        return {}, text
    try:
        data = yaml.safe_load(m.group(1)) or {}
        if not isinstance(data, dict):
            data = {}
    except yaml.YAMLError:
        data = {"_error": "invalid frontmatter"}
    return _jsonable(data), text[m.end():]


def mask_code(text: str) -> str:
    """코드 블록·인라인 코드를 같은 길이의 공백/빈 줄로 바꿔 줄 번호를 유지한다."""
    def blank(m: re.Match) -> str:
        return re.sub(r"[^\n]", " ", m.group(0))
    return INLINE_CODE_RE.sub(blank, FENCE_RE.sub(blank, text))


def _clean_target(t: str) -> str:
    t = t.split("|", 1)[0]
    t = t.split("#", 1)[0]
    t = t.split("^", 1)[0]
    return t.strip()


def extract_links(masked: str) -> list[Link]:
    out: list[Link] = []
    for m in WIKI_RE.finditer(masked):
        t = _clean_target(m.group(2))
        if t:
            out.append(Link(t, bool(m.group(1)), "wiki"))
    for m in MDLINK_RE.finditer(masked):
        url = m.group(2)
        if re.match(r"^[a-z][a-z0-9+.-]*:", url, re.I):  # http:, mailto: ...
            continue
        t = unquote(url.split("#", 1)[0]).strip().lstrip("<").rstrip(">")
        if t:
            out.append(Link(t, bool(m.group(1)), "md"))
    return out


def extract_tags(masked_body: str, fm: dict[str, Any]) -> list[str]:
    tags: list[str] = []
    fmt = fm.get("tags", fm.get("tag"))
    if isinstance(fmt, str):
        fmt = re.split(r"[,\s]+", fmt)
    if isinstance(fmt, list):
        tags += [str(t).lstrip("#") for t in fmt if t]
    stripped = WIKI_RE.sub(" ", masked_body)
    stripped = MDLINK_RE.sub(" ", stripped)
    tags += [m.group(1) for m in TAG_RE.finditer(stripped)]
    seen, uniq = set(), []
    for t in tags:
        if t and t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq


def normalize_task(text: str) -> str:
    s = _EMOJI_META.sub(" ", text)
    s = WIKI_RE.sub(lambda m: (m.group(2).split("|")[-1] if "|" in m.group(2) else _clean_target(m.group(2))), s)
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)
    s = TAG_RE.sub(" ", s)
    s = re.sub(r"[*_~`]+", "", s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


def extract_tasks(masked: str) -> list[Task]:
    tasks: list[Task] = []
    for i, line in enumerate(masked.splitlines(), start=1):
        m = TASK_RE.match(line)
        if not m:
            continue
        ch, text = m.group(1), m.group(2)
        status = {" ": "open", "/": "open", "x": "done", "X": "done", "-": "cancelled"}.get(ch, "other")
        norm = normalize_task(text)
        if not norm:
            continue
        due = _DUE_RE.search(text)
        tags = [t.group(1) for t in TAG_RE.finditer(text)]
        tasks.append(Task(i, status, text, norm, tags, due.group(1) if due else None))
    return tasks


def parse_note(text: str) -> Note:
    fm, body = split_frontmatter(text)
    ai = fm.get("ai")
    blocked = ai is False or (isinstance(ai, str) and ai.strip().lower() == "false")
    masked_full = mask_code(text)
    masked_body = mask_code(body)
    return Note(
        frontmatter=fm,
        body=body,
        links=extract_links(masked_body),
        tags=extract_tags(masked_body, fm),
        tasks=extract_tasks(masked_full),
        ai_blocked=blocked,
    )


# ---- 검색용 토크나이저 -------------------------------------------------------

def words(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower())


def index_tokens(text: str) -> str:
    """한글은 2-gram, 그 외는 단어 단위로 쪼개 공백으로 이어 FTS5 에 넣는다."""
    out: list[str] = []
    for w in words(text):
        if _HANGUL.match(w) and len(w) >= 2:
            out.extend(w[i:i + 2] for i in range(len(w) - 1))
        else:
            out.append(w)
    return " ".join(out)


def query_terms(query: str) -> list[str]:
    """질의를 FTS5 구문(큰따옴표 구문)의 목록으로 바꾼다. 한글 단어는 2-gram 구문이 된다."""
    terms: list[str] = []
    for w in words(query):
        if _HANGUL.match(w) and len(w) >= 2:
            terms.append('"' + " ".join(w[i:i + 2] for i in range(len(w) - 1)) + '"')
        else:
            terms.append('"' + w.replace('"', "") + '"')
    return terms
