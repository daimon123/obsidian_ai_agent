import os
import tempfile
import time
import unittest
from pathlib import Path

from vault_mcp.config import Config
from vault_mcp.vault import Vault, VaultError


def w(root: Path, rel: str, text: str):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


class VaultTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.root = t / "vault"
        w(self.root, "03. Area/11. IT-Cloud/Kubernetes.md",
          "---\ntype: note\ntags: [it/cloud/k8s]\n---\n쿠버네티스는 컨테이너 오케스트레이션이다. [[Helm(헬름)]] [[없는문서]]\n")
        w(self.root, "03. Area/11. IT-Cloud/Helm(헬름).md", "---\ntype: note\n---\nHelm 은 쿠버네티스 패키지 관리자. #it/cloud/k8s\n")
        w(self.root, "03. Area/20. AI/비밀.md", "---\nai: false\n---\n비밀내용 쿠버네티스\n")
        w(self.root, "04. Resource/Personal/접속정보.md", "비밀번호 쿠버네티스 secret\n")
        w(self.root, "998.Template/Daily 템플릿.md", "- [ ] 템플릿작업\n")
        w(self.root, "03. Area/30. PKM/링크.md", "[[접속정보]] ![[img.png]]\n")
        w(self.root, "999.Attached/img.png", "x")
        for d, a, b in [("2026-11-01", " ", " "), ("2026-11-02", " ", " "), ("2026-11-03", "x", " ")]:
            w(self.root, f"00. Daily/2026/{d} 월.md",
              f"- [{a}] 보고서 작성 📅 2026-11-10 #job\n- [{b}] 운동하기 #private\n")
        cfg = Config(vault_path=self.root, db_path=t / "idx.sqlite", refresh_interval_sec=0)
        self.v = Vault(cfg)
        self.v.refresh(force=True)

    def tearDown(self):
        self.v.idx.db.close()
        self.tmp.cleanup()

    def paths(self, res):
        return {r["path"] for r in res}

    def test_korean_search_and_substring(self):
        self.assertIn("03. Area/11. IT-Cloud/Kubernetes.md", self.paths(self.v.search("쿠버네티스")))
        self.assertIn("03. Area/11. IT-Cloud/Kubernetes.md", self.paths(self.v.search("버네티")))  # 부분 일치
        self.assertEqual(self.v.search("존재하지않는단어xyz"), [])

    def test_denylist_and_ai_false_never_surface(self):
        res = self.paths(self.v.search("쿠버네티스"))
        self.assertFalse(any("Personal" in p or "비밀" in p for p in res))
        self.assertEqual(self.v.search("secret"), [])
        for bad in ["04. Resource/Personal/접속정보.md", "03. Area/20. AI/비밀.md"]:
            with self.assertRaises(VaultError):
                self.v.read_note(bad)
        with self.assertRaises(VaultError):
            self.v.list_notes("04. Resource/Personal")
        self.assertEqual(self.v.vault_stats()["ai_blocked_notes"], 1)

    def test_ai_false_added_after_index_is_blocked_on_read(self):
        rel = "03. Area/11. IT-Cloud/Helm(헬름).md"
        p = self.root / rel
        p.write_text("---\nai: false\n---\n내용", encoding="utf-8")
        with self.assertRaises(VaultError):
            self.v.read_note(rel)

    def test_path_traversal_rejected(self):
        for bad in ["../x.md", "..\\x", "/etc/passwd", "C:/Windows/x"]:
            with self.assertRaises(VaultError):
                self.v.read_note(bad)

    def test_template_not_indexed_but_link_names_resolve(self):
        self.assertEqual(self.v.search("템플릿작업"), [])
        links = self.v.get_links("03. Area/30. PKM/링크.md")
        self.assertIn("restricted", links["outgoing"][0])  # 접속정보 = denylist
        self.assertTrue(any(o["resolved"] == "999.Attached/img.png" for o in links["outgoing"]))
        self.assertEqual(links["unresolved"], [])

    def test_links_in_out_unresolved(self):
        l = self.v.get_links("03. Area/11. IT-Cloud/Kubernetes.md")
        self.assertEqual(l["unresolved"], ["없는문서"])
        h = self.v.get_links("03. Area/11. IT-Cloud/Helm(헬름).md")
        self.assertEqual(h["incoming"], ["03. Area/11. IT-Cloud/Kubernetes.md"])

    def test_tasks_dedupe_latest_wins(self):
        tasks = {t["text"].split(" 📅")[0].split(" #")[0]: t for t in
                 self.v.find_tasks(today="2026-11-10", status="all")}
        rep = tasks["보고서 작성"]
        self.assertEqual((rep["count"], rep["status"], rep["ever_done"]), (3, "done", True))  # 마지막 날짜에 체크됨 = 해결
        self.assertEqual(rep["first_seen"], "2026-11-01")
        self.assertEqual(rep["due"], "2026-11-10")
        run = [t for t in self.v.find_tasks(today="2026-11-10") if "운동" in t["text"]]
        self.assertEqual(len(run), 1)
        self.assertEqual(run[0]["count"], 3)
        # 템플릿 폴더 작업은 포함되지 않는다, 두 번 호출해도 결과 동일
        a = self.v.find_tasks(today="2026-11-10"); b = self.v.find_tasks(today="2026-11-10")
        self.assertEqual(a, b)
        self.assertFalse(any("템플릿" in t["text"] for t in a))

    def test_tag_filter_and_window(self):
        self.assertEqual(self.v.find_tasks(today="2026-11-10", tags=["job"]), [])  # 해결된 작업은 open 에서 제외
        self.assertEqual(len(self.v.find_tasks(today="2026-11-10", tags=["job"], status="all")), 1)
        self.assertEqual(self.v.find_tasks(today="2026-12-30", days=10), [])

    def test_incremental_sync(self):
        rel = "03. Area/11. IT-Cloud/Helm(헬름).md"
        time.sleep(0.01)
        w(self.root, rel, "---\ntype: note\n---\n새로운 내용 오토스케일링\n")
        os.utime(self.root / rel, (time.time() + 5, time.time() + 5))
        r = self.v.refresh(force=True)
        self.assertEqual(r["updated"], 1)
        self.assertIn(rel, self.paths(self.v.search("오토스케일링")))
        (self.root / rel).unlink()
        r = self.v.refresh(force=True)
        self.assertEqual(r["removed"], 1)
        self.assertEqual(self.v.search("오토스케일링"), [])

    def test_similar_and_stats(self):
        sim = self.v.find_similar("03. Area/11. IT-Cloud/Kubernetes.md")
        self.assertEqual(sim[0]["title"], "Helm(헬름)")
        s = self.v.vault_stats()
        self.assertEqual(s["broken_links"]["count"], 1)
        self.assertEqual(s["by_type"].get("note"), 2)


if __name__ == "__main__":
    unittest.main()
