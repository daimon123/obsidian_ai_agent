"""FastMCP 서버 (읽기 전용). stdio(로컬) / streamable-http(클라우드) 전환."""
from __future__ import annotations

import argparse

from .change_store import ChangeError
from .vault import Vault, VaultError


def build_server(vault: Vault):
    from mcp.server.fastmcp import FastMCP  # 지연 import: 코어 로직은 mcp 없이도 테스트 가능

    mcp = FastMCP("vault-mcp")

    def guard(fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except VaultError as e:
            return {"error": str(e)}
        except ChangeError as e:
            return {"error": str(e), **e.details}

    @mcp.tool()
    def vault_stats() -> dict:
        """Vault 현황: 문서 수, 유형별/폴더별 수, 빈 문서, 깨진 링크, 고아 문서, Daily 미완료 행 수."""
        return guard(vault.vault_stats)

    @mcp.tool()
    def list_notes(folder: str = "", type: str = "", limit: int = 50, offset: int = 0) -> dict:
        """폴더(Vault 상대 경로)와 type(note/project/book/moc/daily)으로 문서 목록 조회."""
        return guard(vault.list_notes, folder, type or None, limit, offset)

    @mcp.tool()
    def read_note(path: str, max_chars: int = 20000) -> dict:
        """문서 본문과 frontmatter 읽기. denylist·ai:false 문서는 거부된다."""
        return guard(vault.read_note, path, max_chars)

    @mcp.tool()
    def get_frontmatter(path: str) -> dict:
        """문서의 frontmatter(속성)만 읽기."""
        return guard(vault.get_frontmatter, path)

    @mcp.tool()
    def get_links(path: str) -> dict:
        """나가는 링크(해석 결과 포함), 들어오는 링크, 깨진 링크."""
        return guard(vault.get_links, path)

    @mcp.tool()
    def search(query: str, limit: int = 10, folder: str = "", type: str = "") -> dict:
        """키워드 검색(BM25, 한글 2-gram). 결과마다 원본 경로와 발췌문 포함."""
        r = guard(vault.search, query, limit, folder, type or None)
        return r if isinstance(r, dict) else {"results": r}

    @mcp.tool()
    def find_similar(path: str, limit: int = 10) -> dict:
        """주어진 문서와 제목·태그·본문이 비슷한 문서(중복·통합 후보)."""
        r = guard(vault.find_similar, path, limit)
        return r if isinstance(r, dict) else {"results": r}

    @mcp.tool()
    def find_tasks(days: int = 30, since: str = "", until: str = "", tags: list[str] | None = None,
                   status: str = "open", include_occurrences: bool = False) -> dict:
        """Daily Note 의 작업을 날짜별 복사본까지 묶어 반환. status: open|done|all. 원본(origin)/최신(latest) 위치 포함."""
        r = guard(vault.find_tasks, since=since or None, until=until or None, tags=tags,
                  status=status, include_occurrences=include_occurrences, days=days)
        return r if isinstance(r, dict) else {"count": len(r), "tasks": r}

    @mcp.tool()
    def plan_change(ops: list[dict], command: str = "", summary: str = "") -> dict:
        """변경 계획 생성(Vault 는 바뀌지 않음). ops 항목: {op: create|modify|move|delete, path, content?, to?}.
        Diff 와 plan_id 를 반환한다. 사용자 승인 후 apply_change 로 반영."""
        return guard(vault.plan_change, ops, command, summary)

    @mcp.tool()
    def apply_change(plan_id: str, approval: str = "manual") -> dict:
        """승인된 계획 반영. 계획 이후 파일이 바뀌었으면 전체 거부. approval=auto 는 Daily 영역 create/modify 만 허용."""
        return guard(vault.apply_change, plan_id, approval)

    @mcp.tool()
    def revert_change(plan_id: str) -> dict:
        """적용한 변경을 역순으로 되돌림. 그 사이 사용자가 고친 파일이 있으면 전체 거부하고 충돌 목록을 반환."""
        return guard(vault.revert_change, plan_id)

    @mcp.tool()
    def list_changes(limit: int = 20, since: str = "") -> dict:
        """최근 변경 이력(어느 명령이 무엇을 바꿨는지)."""
        return {"changes": vault.list_changes(limit, since or None)}

    return mcp


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None)
    ap.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    a = ap.parse_args(argv)
    vault = Vault.from_config(a.config)
    vault.refresh(force=True)
    build_server(vault).run(transport=a.transport)


if __name__ == "__main__":
    main()
