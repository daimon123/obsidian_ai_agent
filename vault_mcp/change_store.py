"""변경 보관소 (plans/03_변경복구_설계안.md).

모든 쓰기는 plan_change → apply_change 를 거치고, 변경 전 원본을 plan_id 단위로 Vault 밖에 보관한다.
revert_change 는 역순 복원 자체를 하나의 새 변경(kind=revert)으로 기록하므로 다시 되돌려(redo) 쓸 수 있다.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import secrets
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import Config
from .parser import parse_note
from .policy import Policy, norm_rel

OPS = ("create", "modify", "move", "delete")
AUTO_PREFIX = "00. Daily/"          # approval=auto 는 Daily 영역의 create/modify 만
MAX_DIFF_LINES = 200


class ChangeError(Exception):
    def __init__(self, msg: str, details: dict | None = None):
        super().__init__(msg)
        self.details = details or {}


def sha1(b: bytes | None) -> str | None:
    return hashlib.sha1(b).hexdigest() if b is not None else None


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name("." + path.name + f".{secrets.token_hex(3)}.vault-mcp.tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _json_write(path: Path, obj: Any) -> None:
    _atomic_write(path, json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8"))


class ChangeStore:
    def __init__(self, cfg: Config, policy: Policy, index=None):
        self.cfg, self.policy, self.index = cfg, policy, index
        self.vault = Path(cfg.vault_path).resolve()
        self.store = Path(cfg.change_store).resolve()
        if self.store == self.vault or self.vault in self.store.parents:
            raise ChangeError("변경 보관소는 Vault 밖에 있어야 합니다")
        self.store.mkdir(parents=True, exist_ok=True)

    # ---- 경로·정책 --------------------------------------------------------
    def _check_path(self, rel: str) -> tuple[str, Path]:
        rel = norm_rel(rel)
        parts = rel.split("/")
        if not rel or ".." in parts or rel.startswith("/") or ":" in parts[0]:
            raise ChangeError(f"잘못된 경로: {rel!r}")
        if not rel.lower().endswith(".md"):
            raise ChangeError(f".md 문서만 변경할 수 있습니다: {rel}")
        if self.policy.is_denied(rel):
            raise ChangeError(f"접근이 제한된 경로입니다 (denylist): {rel}")
        ap = (self.vault / rel)
        if self.vault not in ap.resolve().parents:
            raise ChangeError(f"Vault 밖의 경로입니다: {rel}")
        if self.index is not None and self.index._is_blocked(rel):
            raise ChangeError(f"문서 단위로 제외된 문서입니다 (ai: false): {rel}")
        if ap.is_file() and parse_note(ap.read_text(encoding="utf-8-sig", errors="replace")).ai_blocked:
            raise ChangeError(f"문서 단위로 제외된 문서입니다 (ai: false): {rel}")
        return rel, ap

    @staticmethod
    def _read(ap: Path) -> bytes | None:
        return ap.read_bytes() if ap.is_file() else None

    def _need_writer(self) -> None:
        if not self.cfg.writer:
            raise ChangeError("쓰기가 꺼져 있습니다 (config writer: false)")

    # ---- plan -------------------------------------------------------------
    def plan_change(self, ops: list[dict], command: str = "", summary: str = "") -> dict:
        built = []
        for o in ops:
            kind = o.get("op")
            if kind not in OPS:
                raise ChangeError(f"알 수 없는 op: {kind!r}")
            content = o.get("content")
            if kind in ("create", "modify"):
                if not isinstance(content, str):
                    raise ChangeError(f"{kind} 에는 content(문자열)가 필요합니다: {o.get('path')}")
                content = content.encode("utf-8")
            built.append({"op": kind, "path": o.get("path", ""), "to": o.get("to"), "content": content})
        plan = self._make_plan(built, command, summary, kind="change", user_level=True)
        return self._plan_view(plan)

    def _make_plan(self, ops_in: list[dict], command: str, summary: str, kind: str,
                   reverts: str | None = None, user_level: bool = False) -> dict:
        plan_id = datetime.now().strftime("%Y%m%d-%H%M%S-%f-") + secrets.token_hex(2)
        d = self.store / plan_id
        (d / "new").mkdir(parents=True)
        seen: set[str] = set()
        ops, diff = [], []

        def claim(rel: str):
            if rel.lower() in seen:
                raise ChangeError(f"한 계획에서 같은 파일을 두 번 다룰 수 없습니다: {rel}")
            seen.add(rel.lower())

        for i, o in enumerate(ops_in):
            k = o["op"]
            rel, ap = self._check_path(o["path"])
            claim(rel)
            cur = self._read(ap)
            rec: dict[str, Any] = {"op": k, "path": rel, "to": None, "before_sha1": sha1(cur),
                                   "after_sha1": None, "content_file": None, "made_dirs": []}
            if k == "create":
                if cur is not None:
                    raise ChangeError(f"이미 존재하는 문서입니다: {rel}")
                diff.append(self._diff(rel, None, o["content"]))
            elif k == "modify":
                if cur is None:
                    raise ChangeError(f"존재하지 않는 문서입니다: {rel}")
                if user_level and cur == o["content"]:
                    raise ChangeError(f"변경 내용이 없습니다: {rel}")
                diff.append(self._diff(rel, cur, o["content"]))
            elif k == "delete":
                if cur is None:
                    raise ChangeError(f"존재하지 않는 문서입니다: {rel}")
                diff.append(self._diff(rel, cur, None))
            else:  # move
                if cur is None:
                    raise ChangeError(f"존재하지 않는 문서입니다: {rel}")
                to, to_ap = self._check_path(o.get("to") or "")
                claim(to)
                if to_ap.exists():
                    raise ChangeError(f"이동 대상이 이미 있습니다: {to}")
                rec["to"] = to
                diff.append(f"rename {rel} -> {to}\n")
            if o.get("content") is not None and k in ("create", "modify"):
                rec["content_file"] = f"new/{i:04d}"
                (d / rec["content_file"]).write_bytes(o["content"])
            ops.append(rec)
        if not ops:
            raise ChangeError("작업이 비어 있습니다")
        plan = {"plan_id": plan_id, "kind": kind, "created": _now(), "command": command,
                "summary": summary or self._auto_summary(ops), "status": "planned",
                "reverts": reverts, "reverted_by": None, "approval": None,
                "applied_at": None, "error": None, "ops": ops}
        (d / "plan.diff").write_text("".join(diff), encoding="utf-8")
        self._save(plan)
        self._event("planned", plan)
        return plan

    @staticmethod
    def _auto_summary(ops: list[dict]) -> str:
        n: dict[str, int] = {}
        for o in ops:
            n[o["op"]] = n.get(o["op"], 0) + 1
        return ", ".join(f"{k} {v}" for k, v in n.items())

    @staticmethod
    def _diff(rel: str, old: bytes | None, new: bytes | None) -> str:
        a = old.decode("utf-8", "replace").splitlines(keepends=True) if old is not None else []
        b = new.decode("utf-8", "replace").splitlines(keepends=True) if new is not None else []
        out = "".join(difflib.unified_diff(
            a, b, fromfile=f"a/{rel}" if old is not None else "/dev/null",
            tofile=f"b/{rel}" if new is not None else "/dev/null"))
        return out if out.endswith("\n") or not out else out + "\n"

    def _plan_view(self, plan: dict) -> dict:
        diff = (self.store / plan["plan_id"] / "plan.diff").read_text(encoding="utf-8").splitlines()
        return {"plan_id": plan["plan_id"], "summary": plan["summary"], "status": plan["status"],
                "ops": [{"op": o["op"], "path": o["path"], "to": o["to"]} for o in plan["ops"]],
                "diff": "\n".join(diff[:MAX_DIFF_LINES]),
                "diff_truncated": len(diff) > MAX_DIFF_LINES}

    # ---- apply ------------------------------------------------------------
    def apply_change(self, plan_id: str, approval: str = "manual") -> dict:
        self._need_writer()
        plan = self._load(plan_id)
        if plan["status"] != "planned":
            raise ChangeError(f"적용할 수 없는 상태입니다: {plan['status']}")
        res = self._apply(plan, approval)
        return res

    def _auto_allowed(self, ops: list[dict]) -> bool:
        return all(o["op"] in ("create", "modify") and o["path"].startswith(AUTO_PREFIX) for o in ops)

    def _preflight(self, ops: list[dict]) -> list[dict]:
        bad = []
        for o in ops:
            rel, ap = self._check_path(o["path"])
            cur = sha1(self._read(ap))
            if o["op"] == "create":
                if cur is not None:
                    bad.append({"path": rel, "reason": "계획 이후 같은 이름의 문서가 생겼습니다"})
            else:
                if cur != o["before_sha1"]:
                    bad.append({"path": rel, "reason": "계획 이후 문서가 변경되었거나 사라졌습니다"})
            if o["op"] == "move":
                to, to_ap = self._check_path(o["to"])
                if to_ap.exists():
                    bad.append({"path": to, "reason": "이동 대상이 이미 있습니다"})
        return bad

    def _apply(self, plan: dict, approval: str) -> dict:
        if approval not in ("manual", "auto"):
            raise ChangeError(f"알 수 없는 approval: {approval!r}")
        ops = plan["ops"]
        if approval == "auto" and not self._auto_allowed(ops):
            raise ChangeError("자동 적용은 Daily 영역의 create/modify 만 허용됩니다")
        conflicts = self._preflight(ops)
        if conflicts:
            raise ChangeError("계획 이후 변경이 감지되어 적용을 거부합니다", {"conflicts": conflicts})
        d = self.store / plan["plan_id"]
        for o in ops:  # 변경 전 원본 보관
            if o["op"] in ("modify", "delete", "move"):
                src = self.vault / o["path"]
                dst = d / "before" / o["path"]
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dst)
        done: list[int] = []
        try:
            for i, o in enumerate(ops):
                self._do(o, d)
                done.append(i)
        except Exception as e:  # 부분 적용 방지: 이미 한 것을 되돌린다
            for i in reversed(done):
                self._undo(ops[i], d)
            plan.update(status="failed", error=f"{type(e).__name__}: {e}")
            self._save(plan)
            self._event("failed", plan)
            raise ChangeError(f"적용 중 오류로 전체를 취소했습니다: {e}") from e
        for o in ops:
            if o["op"] in ("create", "modify"):
                o["after_sha1"] = sha1((d / o["content_file"]).read_bytes())
            elif o["op"] == "move":
                o["after_sha1"] = o["before_sha1"]
        plan.update(status="applied", approval=approval, applied_at=_now())
        self._save(plan)
        _json_write(d / "result.json", {"applied_at": plan["applied_at"], "result": "ok",
                                        "approval": approval, "reverted_at": None})
        self._event("applied", plan)
        return {"plan_id": plan["plan_id"], "status": "applied", "approval": approval,
                "changed": [{"op": o["op"], "path": o["path"], "to": o["to"]} for o in ops]}

    def _mkdirs(self, parent: Path) -> list[str]:
        made = []
        todo = []
        p = parent
        while not p.exists():
            todo.append(p)
            p = p.parent
        for p in reversed(todo):
            p.mkdir()
            made.append(str(p))
        return made

    def _rmdirs(self, made: list[str]) -> None:
        for p in reversed(made):
            try:
                Path(p).rmdir()
            except OSError:
                pass

    def _do(self, o: dict, d: Path) -> None:
        ap = self.vault / o["path"]
        k = o["op"]
        if k in ("create", "modify"):
            if k == "create":
                o["made_dirs"] = self._mkdirs(ap.parent)
            _atomic_write(ap, (d / o["content_file"]).read_bytes())
        elif k == "delete":
            ap.unlink()
        else:
            to = self.vault / o["to"]
            o["made_dirs"] = self._mkdirs(to.parent)
            os.rename(ap, to)

    def _undo(self, o: dict, d: Path) -> None:
        ap = self.vault / o["path"]
        k = o["op"]
        if k == "create":
            if ap.exists():
                ap.unlink()
            self._rmdirs(o.get("made_dirs", []))
        elif k in ("modify", "delete"):
            ap.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(ap, (d / "before" / o["path"]).read_bytes())
        else:
            to = self.vault / o["to"]
            if to.exists() and not ap.exists():
                os.rename(to, ap)
            self._rmdirs(o.get("made_dirs", []))

    # ---- revert -----------------------------------------------------------
    def revert_change(self, plan_id: str) -> dict:
        self._need_writer()
        plan = self._load(plan_id)
        if plan["status"] != "applied":
            raise ChangeError(f"되돌릴 수 없는 상태입니다: {plan['status']}")
        d = self.store / plan_id
        conflicts = []
        for o in plan["ops"]:
            rel, ap = self._check_path(o["path"])
            cur = sha1(self._read(ap))
            k = o["op"]
            if k in ("create", "modify"):
                if cur != o["after_sha1"]:
                    conflicts.append({"path": rel, "reason": "적용 이후 문서가 변경되었거나 사라졌습니다"})
            elif k == "delete":
                if cur is not None:
                    conflicts.append({"path": rel, "reason": "삭제했던 경로에 새 문서가 생겼습니다"})
            else:
                to, to_ap = self._check_path(o["to"])
                if sha1(self._read(to_ap)) != o["after_sha1"]:
                    conflicts.append({"path": to, "reason": "이동한 문서가 변경되었거나 사라졌습니다"})
                if ap.exists():
                    conflicts.append({"path": rel, "reason": "이전 경로에 다른 문서가 생겼습니다"})
        if conflicts:
            raise ChangeError("되돌리기 사전 점검에서 충돌이 발견되어 전체를 거부합니다", {"conflicts": conflicts})
        inv = []
        for o in reversed(plan["ops"]):
            k = o["op"]
            before = (d / "before" / o["path"]).read_bytes() if k in ("modify", "delete") else None
            if k == "create":
                inv.append({"op": "delete", "path": o["path"]})
            elif k == "modify":
                inv.append({"op": "modify", "path": o["path"], "content": before})
            elif k == "delete":
                inv.append({"op": "create", "path": o["path"], "content": before})
            else:
                inv.append({"op": "move", "path": o["to"], "to": o["path"]})
        rp = self._make_plan(inv, command="revert", summary=f"revert {plan_id}", kind="revert", reverts=plan_id)
        res = self._apply(rp, "manual")
        for o in plan["ops"]:   # 원래 변경이 만든 폴더가 비었으면 정리
            self._rmdirs(o.get("made_dirs", []))
        plan["status"] = "reverted"
        plan["reverted_by"] = rp["plan_id"]
        self._save(plan)
        _json_write(d / "result.json", {"applied_at": plan["applied_at"], "result": "ok",
                                        "approval": plan["approval"], "reverted_at": _now(),
                                        "reverted_by": rp["plan_id"]})
        self._event("reverted", plan)
        if plan["kind"] == "revert" and plan.get("reverts"):  # redo: 원래 변경이 다시 적용된 상태
            orig = self._load(plan["reverts"])
            orig.update(status="applied", reverted_by=None)
            self._save(orig)
        return {"plan_id": plan_id, "status": "reverted", "revert_plan_id": rp["plan_id"],
                "restored": res["changed"]}

    # ---- 조회·정리 --------------------------------------------------------
    def list_changes(self, limit: int = 20, since: str | None = None) -> list[dict]:
        out = []
        for d in sorted((p for p in self.store.iterdir() if (p / "plan.json").is_file()), reverse=True):
            plan = json.loads((d / "plan.json").read_text(encoding="utf-8"))
            if since and plan["created"] < since:
                continue
            out.append({"plan_id": plan["plan_id"], "created": plan["created"], "kind": plan["kind"],
                        "status": plan["status"], "approval": plan["approval"], "command": plan["command"],
                        "summary": plan["summary"], "paths": [o["path"] for o in plan["ops"]],
                        "reverts": plan["reverts"], "reverted_by": plan["reverted_by"]})
            if len(out) >= max(1, int(limit)):
                break
        return out

    def prune(self, days: int = 90, max_mb: int = 500) -> dict:
        dirs = sorted(p for p in self.store.iterdir() if p.is_dir())
        removed = 0
        cutoff = time.time() - days * 86400
        sizes = {p: sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) for p in dirs}
        total = sum(sizes.values())
        for p in dirs:  # 오래된 것부터
            if p.stat().st_mtime < cutoff or total > max_mb * 1024 * 1024:
                total -= sizes[p]
                shutil.rmtree(p)
                removed += 1
        return {"removed": removed, "remaining_bytes": total}

    # ---- 저장 -------------------------------------------------------------
    def _load(self, plan_id: str) -> dict:
        if not plan_id or "/" in plan_id or "\\" in plan_id or ".." in plan_id:
            raise ChangeError("잘못된 plan_id")
        f = self.store / plan_id / "plan.json"
        if not f.is_file():
            raise ChangeError(f"계획을 찾을 수 없습니다: {plan_id}")
        return json.loads(f.read_text(encoding="utf-8"))

    def _save(self, plan: dict) -> None:
        _json_write(self.store / plan["plan_id"] / "plan.json", plan)

    def _event(self, event: str, plan: dict) -> None:
        line = json.dumps({"ts": _now(), "event": event, "plan_id": plan["plan_id"], "kind": plan["kind"],
                           "command": plan["command"], "summary": plan["summary"],
                           "approval": plan["approval"], "status": plan["status"]}, ensure_ascii=False)
        with open(self.store / "index.jsonl", "a", encoding="utf-8") as f:
            f.write(line + "\n")
