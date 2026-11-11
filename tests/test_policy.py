import tempfile
import unittest
from pathlib import Path

from vault_mcp.config import Config, load_config
from vault_mcp.vault import Vault, VaultError


class FileDenylistTest(unittest.TestCase):
    def test_file_level_denylist(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "v"
            (root / "05. Archive/x").mkdir(parents=True)
            (root / "05. Archive/x/접속 정보.md").write_text("비밀번호 hunter2 쿠버네티스", encoding="utf-8")
            (root / "05. Archive/x/일반.md").write_text("쿠버네티스 일반 노트 [[접속 정보]]", encoding="utf-8")
            cfg = Config(vault_path=root, denylist=["05. Archive/x/접속 정보.md"],
                         db_path=Path(d) / "i.sqlite", refresh_interval_sec=0)
            v = Vault(cfg)
            try:   # Windows 는 열린 SQLite 파일을 지울 수 없으므로 임시 폴더 정리 전에 닫는다
                v.refresh(force=True)
                self.assertEqual([r["path"] for r in v.search("쿠버네티스")], ["05. Archive/x/일반.md"])
                self.assertEqual(v.search("hunter2"), [])
                with self.assertRaises(VaultError):
                    v.read_note("05. Archive/x/접속 정보.md")
                self.assertTrue(v.get_links("05. Archive/x/일반.md")["outgoing"][0]["restricted"])
            finally:
                v.close()

    def test_example_config_is_generic(self):
        path = Path(__file__).resolve().parent.parent / "config/vault.example.yaml"
        cfg = load_config(path)
        self.assertIn("04. Resource/Personal", cfg.denylist)
        text = path.read_text(encoding="utf-8")
        for private in ["daimon123", "접속 정보"]:   # 개인 경로·노트 제목이 예시 파일에 들어가지 않게
            self.assertNotIn(private, text)


if __name__ == "__main__":
    unittest.main()
