# Obsidian AI Agent — vault-mcp

Vault 로직을 담은 Python MCP 서버. 쓰기는 `writer: true`일 때만, 항상 `plan_change` → `apply_change`를 거친다(기본 `writer: false`).

## 도구
`vault_stats` · `list_notes` · `read_note` · `get_frontmatter` · `get_links` · `search`(BM25, 한글 2-gram) · `find_similar` · `find_tasks`(Daily 미완료 작업 중복 묶음)

`plan_change` · `apply_change` · `revert_change` · `list_changes` — 변경 보관소(`plans/03_변경복구_설계안.md`). Vault 밖 `change_store`에 원본을 보관해 `plan_id` 단위로 되돌린다.

## 접근 정책
- `denylist`(폴더 또는 파일 경로, 기본 `04. Resource/Personal`)와 시스템 폴더(`.obsidian` 등): 색인·검색·읽기 모두 거부
- `ai: false` frontmatter 문서: 색인에서 제외, 읽기 거부
- `index_exclude`: 색인만 제외(링크 이름 해석에는 사용)

## 실행 (Windows)
```
py -3.11 -m venv .venv && .venv\Scripts\activate
pip install -e ".[dev]"
copy config\vault.example.yaml config\vault.local.yaml
python -m unittest discover -s tests -t .      # 또는 pytest
python -m vault_mcp.cli stats                  # MCP 없이 점검
python -m vault_mcp.cli search "쿠버네티스 보안"
python -m vault_mcp.cli tasks --days 30
python -m vault_mcp.server                     # stdio MCP 서버
```
환경 변수: `VAULT_MCP_CONFIG`, `VAULT_MCP_VAULT`, `VAULT_MCP_DB`.

## 다음 단계
Hermes 연결 → Daily Note Skill(`ensure_daily_note`, `update_section`) → Vault Q&A Skill.
