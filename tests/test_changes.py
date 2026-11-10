import hashlib
import tempfile
import unittest
from pathlib import Path

from vault_mcp.change_store import ChangeError
from vault_mcp.config import Config
from vault_mcp.vault import Vault


def snapshot(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): hashlib.sha1(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file()}


class ChangeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.root = t / "vault"
        self.files = {
            "03. Area/A.md": "---\ntype: note\n---\n# A\n본문 A\n",
            "03. Area/B.md": "# B\n[[A]] 를 참고\n",
            "03. Area/C.md": "# C\n[[A]] 와 [[B]]\n",
            "04. Resource/Personal/비밀.md": "비밀\n",
            "03. Area/숨김.md": "---\nai: false\n---\n숨김\n",
        }
        for rel, txt in self.files.items():
            p = self.root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(txt, encoding="utf-8")
        self.cfg = Config(vault_path=self.root, db_path=t / "idx.sqlite", change_store=t / "store",
                          writer=True, refresh_interval_sec=0)
        self.v = Vault(self.cfg)
        self.v.refresh(force=True)
        self.orig = snapshot(self.root)

    def tearDown(self):
        self.v.idx.db.close()
        self.tmp.cleanup()

    def run_plan(self, ops, approval="manual"):
        pl = self.v.plan_change(ops, command="test")
        return pl, self.v.apply_change(pl["plan_id"], approval)

    # --- 승인 흐름 -----------------------------------------------------------
    def test_plan_changes_nothing(self):
        pl = self.v.plan_change([{"op": "modify", "path": "03. Area/A.md", "content": "새 내용\n"}])
        self.assertEqual(snapshot(self.root), self.orig)
        self.assertIn("-본문 A", pl["diff"])
        self.assertIn("+새 내용", pl["diff"])

    def test_apply_requires_existing_plan(self):
        for bad in ["nope", "../x", ""]:
            with self.assertRaises(ChangeError):
                self.v.apply_change(bad)
        self.assertEqual(snapshot(self.root), self.orig)

    def test_apply_only_once(self):
        pl, _ = self.run_plan([{"op": "create", "path": "03. Area/N.md", "content": "n\n"}])
        with self.assertRaises(ChangeError):
            self.v.apply_change(pl["plan_id"])

    def test_writer_off_blocks_apply_and_revert(self):
        pl = self.v.plan_change([{"op": "create", "path": "03. Area/N.md", "content": "n\n"}])
        self.cfg.writer = False
        with self.assertRaises(ChangeError):
            self.v.apply_change(pl["plan_id"])
        self.assertEqual(snapshot(self.root), self.orig)

    def test_hash_changed_after_plan_rejects_all(self):
        pl = self.v.plan_change([
            {"op": "modify", "path": "03. Area/A.md", "content": "x\n"},
            {"op": "modify", "path": "03. Area/B.md", "content": "y\n"}])
        (self.root / "03. Area/B.md").write_text("사용자가 편집\n", encoding="utf-8")
        with self.assertRaises(ChangeError) as cm:
            self.v.apply_change(pl["plan_id"])
        self.assertEqual(cm.exception.details["conflicts"][0]["path"], "03. Area/B.md")
        self.assertEqual((self.root / "03. Area/A.md").read_text(encoding="utf-8"), self.files["03. Area/A.md"])

    # --- 정책 ------------------------------------------------------------
    def test_denylist_aiflase_and_traversal_refused(self):
        for p in ["04. Resource/Personal/비밀.md", "04. Resource/Personal/새.md",
                  "03. Area/숨김.md", "../밖.md", ".obsidian/x.md", "03. Area/a.txt"]:
            with self.assertRaises(ChangeError, msg=p):
                self.v.plan_change([{"op": "create" if "새" in p or "밖" in p or "x.md" in p or "txt" in p else "modify",
                                     "path": p, "content": "x"}])

    def test_store_must_be_outside_vault(self):
        bad = Config(vault_path=self.root, db_path=self.root.parent / "i2.sqlite",
                     change_store=self.root / "store", writer=True)
        with self.assertRaises(ChangeError):
            Vault(bad)

    def test_auto_only_daily_create_modify(self):
        pl = self.v.plan_change([{"op": "modify", "path": "03. Area/A.md", "content": "x\n"}])
        with self.assertRaises(ChangeError):
            self.v.apply_change(pl["plan_id"], approval="auto")
        _, r = self.run_plan([{"op": "create", "path": "00. Daily/2026/2026-11-10 화.md", "content": "d\n"}], "auto")
        self.assertEqual(r["approval"], "auto")
        pl = self.v.plan_change([{"op": "delete", "path": "00. Daily/2026/2026-11-10 화.md"}])
        with self.assertRaises(ChangeError):
            self.v.apply_change(pl["plan_id"], approval="auto")

    # --- 복구 ------------------------------------------------------------
    def test_revert_ten_changes_restores_hashes(self):
        ids = []
        for i in range(4):  # 수정 4
            p = ["A", "B", "C", "A"][i]
            cur = (self.root / f"03. Area/{p}.md").read_text(encoding="utf-8")
            ids.append(self.run_plan([{"op": "modify", "path": f"03. Area/{p}.md", "content": cur + f"추가{i}\n"}])[0]["plan_id"])
        for i in range(3):  # 생성 3
            ids.append(self.run_plan([{"op": "create", "path": f"03. Area/신규/N{i}.md", "content": f"n{i}\n"}])[0]["plan_id"])
        ids.append(self.run_plan([{"op": "move", "path": "03. Area/C.md", "to": "05. Archive/새폴더/C.md"}])[0]["plan_id"])
        ids.append(self.run_plan([{"op": "delete", "path": "03. Area/B.md"}])[0]["plan_id"])
        ids.append(self.run_plan([{"op": "modify", "path": "03. Area/A.md", "content": "마지막\n"}])[0]["plan_id"])
        self.assertEqual(len(ids), 10)
        self.assertNotEqual(snapshot(self.root), self.orig)
        for pid in reversed(ids):
            self.assertEqual(self.v.revert_change(pid)["status"], "reverted")
        self.assertEqual(snapshot(self.root), self.orig)
        self.assertFalse((self.root / "05. Archive/새폴더").exists())   # 만든 폴더 정리
        self.assertFalse((self.root / "03. Area/신규").exists())

    def test_revert_refused_when_user_edited_after(self):
        a = self.run_plan([{"op": "modify", "path": "03. Area/A.md", "content": "v2\n"}])[0]["plan_id"]
        b = self.run_plan([{"op": "create", "path": "03. Area/N.md", "content": "n\n"}])[0]["plan_id"]
        (self.root / "03. Area/A.md").write_text("사용자 편집\n", encoding="utf-8")
        before = snapshot(self.root)
        with self.assertRaises(ChangeError) as cm:
            self.v.revert_change(a)
        self.assertEqual([c["path"] for c in cm.exception.details["conflicts"]], ["03. Area/A.md"])
        self.assertEqual(snapshot(self.root), before)
        self.assertEqual(self.v.revert_change(b)["status"], "reverted")   # 다른 변경은 가능

    def test_multi_op_revert_all_or_nothing(self):
        pid = self.run_plan([
            {"op": "modify", "path": "03. Area/A.md", "content": "v2\n"},
            {"op": "create", "path": "03. Area/N.md", "content": "n\n"}])[0]["plan_id"]
        (self.root / "03. Area/N.md").write_text("편집\n", encoding="utf-8")
        before = snapshot(self.root)
        with self.assertRaises(ChangeError):
            self.v.revert_change(pid)
        self.assertEqual(snapshot(self.root), before)   # A 도 그대로

    def test_move_with_link_updates_revert_restores_links(self):
        def links():
            return {n: sorted((o["target"], o["resolved"]) for o in self.v.get_links(n)["outgoing"])
                    for n in ["03. Area/B.md", "03. Area/C.md"]}
        before_links = links()
        pl, _ = self.run_plan([
            {"op": "move", "path": "03. Area/A.md", "to": "03. Area/Alpha.md"},
            {"op": "modify", "path": "03. Area/B.md", "content": "# B\n[[Alpha]] 를 참고\n"},
            {"op": "modify", "path": "03. Area/C.md", "content": "# C\n[[Alpha]] 와 [[B]]\n"}])
        moved = links()
        self.assertEqual([r for _, r in moved["03. Area/B.md"]], ["03. Area/Alpha.md"])
        self.v.revert_change(pl["plan_id"])
        self.assertEqual(links(), before_links)
        self.assertEqual(snapshot(self.root), self.orig)

    def test_redo_by_reverting_the_revert(self):
        pid = self.run_plan([{"op": "modify", "path": "03. Area/A.md", "content": "v2\n"}])[0]["plan_id"]
        after = snapshot(self.root)
        rid = self.v.revert_change(pid)["revert_plan_id"]
        self.assertEqual(snapshot(self.root), self.orig)
        self.v.revert_change(rid)
        self.assertEqual(snapshot(self.root), after)
        statuses = {c["plan_id"]: c["status"] for c in self.v.list_changes()}
        self.assertEqual(statuses[pid], "applied")
        self.v.revert_change(pid)   # 다시 되돌릴 수 있다
        self.assertEqual(snapshot(self.root), self.orig)

    def test_failure_midway_rolls_back(self):
        pl = self.v.plan_change([
            {"op": "modify", "path": "03. Area/A.md", "content": "v2\n"},
            {"op": "create", "path": "03. Area/N.md", "content": "n\n"}])
        orig_do = self.v.changes._do
        def boom(o, d):
            if o["op"] == "create":
                raise OSError("disk full")
            orig_do(o, d)
        self.v.changes._do = boom
        with self.assertRaises(ChangeError):
            self.v.apply_change(pl["plan_id"])
        self.assertEqual(snapshot(self.root), self.orig)
        self.assertEqual(self.v.list_changes()[0]["status"], "failed")

    # --- 기록 ------------------------------------------------------------
    def test_history_and_index_refresh(self):
        pl, _ = self.run_plan([{"op": "create", "path": "03. Area/새글.md", "content": "오토스케일링 문서\n"}])
        self.assertEqual([r["path"] for r in self.v.search("오토스케일링")], ["03. Area/새글.md"])
        self.v.revert_change(pl["plan_id"])
        self.assertEqual(self.v.search("오토스케일링"), [])
        ch = self.v.list_changes()
        self.assertEqual([c["kind"] for c in ch], ["revert", "change"])
        self.assertEqual(ch[1]["status"], "reverted")
        lines = (Path(self.cfg.change_store) / "index.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual([__import__("json").loads(l)["event"] for l in lines],
                         ["planned", "applied", "planned", "applied", "reverted"])

    def test_every_applied_change_has_approval(self):
        self.run_plan([{"op": "modify", "path": "03. Area/A.md", "content": "v2\n"}])
        self.run_plan([{"op": "create", "path": "00. Daily/2026/2026-11-11 수.md", "content": "d\n"}], "auto")
        self.assertTrue(all(c["approval"] in ("manual", "auto")
                            for c in self.v.list_changes() if c["status"] == "applied"))


if __name__ == "__main__":
    unittest.main()
