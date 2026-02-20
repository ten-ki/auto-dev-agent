"""
executor.py
AIの指示を実際のファイル操作に変換する
"""

from pathlib import Path


class Executor:
    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.workspace.mkdir(parents=True, exist_ok=True)

    def write_files(self, files: list):
        """
        files: [{"path": "index.html", "content": "..."}, ...]
        workspaceディレクトリ内にのみ書き込む（セキュリティ）
        """
        for file_info in files:
            path_str = file_info.get("path", "")
            content = file_info.get("content", "")

            if not path_str:
                continue

            # パストラバーサル対策
            target = (self.workspace / path_str).resolve()
            if not str(target).startswith(str(self.workspace.resolve())):
                print(f"[executor] 危険なパス検出、スキップ: {path_str}")
                continue

            # ディレクトリ作成
            target.parent.mkdir(parents=True, exist_ok=True)

            # 書き込み
            target.write_text(content, encoding="utf-8")
            print(f"[executor] 書き込み: {path_str} ({len(content)} chars)")
