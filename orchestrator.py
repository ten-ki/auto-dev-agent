"""イテレーションループコントローラー。"""

import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import yaml

from agent import Agent
from evaluator import Evaluator
from executor import Executor
from git_manager import GitManager

CONFIG_PATH = Path(__file__).parent / "config.yaml"

CURRENT_ITER_HEADERS = ["## Current Iteration", "## 現在のイテレーション"]
TODO_HEADERS         = ["## TODO", "## やること"]
NEXT_PLAN_HEADERS    = ["## Next Iteration Plan", "## 次のイテレーション計画"]


class Orchestrator:
    def __init__(
        self,
        project: dict,
        max_iterations: int = 0,
        interval: int = -1,       # -1 = config.yamlの値を使う
        max_minutes: int = 0,     # 0 = 無制限
    ):
        self.project     = project
        self.project_dir = project["project_dir"]
        self.workspace   = self.project_dir / "workspace"
        self.iteration   = 0

        with open(CONFIG_PATH, encoding="utf-8") as f:
            self.config = yaml.safe_load(f)

        iter_cfg = self.config.get("iteration", {})

        # 停止条件
        self.max_iterations  = max_iterations if max_iterations > 0 else iter_cfg.get("max_iterations", 100)
        self.max_minutes     = max_minutes    if max_minutes    > 0 else iter_cfg.get("max_minutes", 0)
        self.stop_after_consecutive_passes      = iter_cfg.get("stop_after_consecutive_passes", 0)
        self.stop_after_no_change_iterations    = iter_cfg.get("stop_after_no_change_iterations", 0)

        # インターバル（-1はconfig値を使う、config値が0以下なら即時）
        cfg_interval   = iter_cfg.get("interval_seconds", 0)  # 0=即時連続
        self.interval  = interval if interval >= 0 else cfg_interval

        # 開始時刻（時間制限の計算用）
        self.started_at = datetime.now()

        log_cfg = self.config.get("logging", {})
        self.eval_log_max_chars            = log_cfg.get("eval_log_max_chars", 2000)
        self.show_thought                  = log_cfg.get("show_thought", True)
        self.thought_preview_chars         = log_cfg.get("thought_preview_chars", 150)
        self.prompt_workspace_file_limit   = log_cfg.get("prompt_workspace_file_limit", 20)
        self.prompt_workspace_chars_per_file = log_cfg.get("prompt_workspace_chars_per_file", 3000)

        self.consecutive_passes  = 0
        self.no_change_streak    = 0

        self.agent    = Agent()
        self.executor = Executor(self.workspace)
        self.evaluator = Evaluator(self.workspace)
        self.git = GitManager(
            self.project_dir,
            remote_url=project.get("github", ""),
            project_slug=project.get("slug", ""),
        )
        self.git.init()
        self._ensure_playwright_runtime()

    # ------------------------------------------------------------------
    # Playwright セットアップ
    # ------------------------------------------------------------------

    def _playwright_runtime_ready(self) -> bool:
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                browser.close()
            return True
        except Exception:
            return False

    def _run_setup_cmd(self, cmd: list):
        result = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"コマンド失敗: {' '.join(cmd)}\n"
                f"stdout: {result.stdout[-400:]}\n"
                f"stderr: {result.stderr[-400:]}"
            )

    def _ensure_playwright_runtime(self):
        eval_cfg = self.config.get("evaluation", {})
        if not eval_cfg.get("use_playwright", True):
            return
        if self._playwright_runtime_ready():
            print("[orchestrator] Playwright 実行環境: 準備済み")
            return
        print("[orchestrator] Playwright が未準備です。インストールします...")
        self._run_setup_cmd([sys.executable, "-m", "pip", "install", "playwright"])
        self._run_setup_cmd([sys.executable, "-m", "playwright", "install", "chromium"])
        if not self._playwright_runtime_ready():
            raise RuntimeError("Playwright のセットアップに失敗しました")
        print("[orchestrator] Playwright セットアップ完了")

    # ------------------------------------------------------------------
    # コンテキスト生成
    # ------------------------------------------------------------------

    def _list_assets(self) -> str:
        assets_dir = self.project_dir / "assets"
        files = [f for f in assets_dir.rglob("*") if f.is_file()]
        return "（なし）" if not files else "\n".join(
            f"- assets/{f.relative_to(assets_dir)}" for f in files
        )

    def _list_workspace(self) -> str:
        files = [f for f in self.workspace.rglob("*") if f.is_file()]
        return "（ファイルなし）" if not files else "\n".join(
            f"- {f.relative_to(self.workspace)}" for f in files
        )

    def _workspace_content_for_prompt(self) -> str:
        files = [f for f in self.workspace.rglob("*") if f.is_file()]
        if not files:
            return "（ファイルなし）"
        parts = []
        for f in sorted(files)[: self.prompt_workspace_file_limit]:
            rel     = f.relative_to(self.workspace)
            content = f.read_text(encoding="utf-8", errors="ignore")[: self.prompt_workspace_chars_per_file]
            parts.append(f"### {rel}\n```\n{content}\n```")
        return "\n\n".join(parts)

    def _read_context(self) -> str:
        spec     = (self.project_dir / "spec.md").read_text(encoding="utf-8")
        status   = (self.project_dir / "status.md").read_text(encoding="utf-8")
        eval_log = (self.project_dir / "eval_log.md").read_text(encoding="utf-8")[-self.eval_log_max_chars:]

        return f"""
# spec.md
{spec}

# status.md
{status}

# eval_log.md（最近の履歴）
{eval_log}

# 利用可能な素材（assets/）
{self._list_assets()}

# workspaceのファイル一覧
{self._list_workspace()}

# workspaceのファイル内容（抜粋）
{self._workspace_content_for_prompt()}
"""

    def _implementer_prompt(self, context: str, feedback: str = "") -> str:
        feedback_section = ""
        if feedback:
            feedback_section = f"""
# 前回の試行に対するフィードバック
{feedback}
このフィードバックを元に失敗原因を修正してください。
"""
        return f"""
あなたは自律的なWeb実装エージェントです。
以下のコンテキストを読み、JSONオブジェクト1つだけを出力してください。

{context}
{feedback_section}

ルール:
- spec.md と status.md のルールを必ず守ること
- 小さく、具体的で、テスト可能な変更にすること
- コードは常に動く状態を維持すること
- 既存ファイルを変更する場合、動作中の機能を壊さないこと

JSONスキーマ:
{{
  "thought": "なぜこれをやるかの簡潔な理由",
  "action_type": "init|add_feature|improve_ui|fix_bug|refactor",
  "files": [{{"path":"index.html","content":"..."}}],
  "implemented_features": ["..."],
  "ui_elements": ["#id", ".class"],
  "assertions": [
    {{"type":"file_exists","path":"index.html"}},
    {{"type":"text_in_file","path":"index.html","text":"Start"}},
    {{"type":"selector_exists","selector":"#app"}}
  ],
  "commit_message": "feat: ...",
  "status_update": "次のイテレーション計画",
  "todo_done": ["..."],
  "todo_add": ["..."]
}}
"""

    # ------------------------------------------------------------------
    # スナップショット / ロールバック
    # ------------------------------------------------------------------

    def _take_snapshot(self, stage: str) -> Path:
        snapshot_dir = self.project_dir / "snapshots" / f"iter-{self.iteration:04d}-{stage}"
        if self.workspace.exists():
            shutil.copytree(self.workspace, snapshot_dir, dirs_exist_ok=True)
        keep = self.config["iteration"].get("snapshot_keep", 20)
        snapshots_root = self.project_dir / "snapshots"
        if snapshots_root.exists():
            for old in sorted(snapshots_root.iterdir())[:-keep]:
                shutil.rmtree(old, ignore_errors=True)
        return snapshot_dir

    def _rollback(self, snapshot_dir: Path):
        if snapshot_dir.exists():
            shutil.rmtree(self.workspace, ignore_errors=True)
            shutil.copytree(snapshot_dir, self.workspace)
            print(f"[orchestrator] ロールバック完了: {snapshot_dir.name}")

    # ------------------------------------------------------------------
    # status.md 更新
    # ------------------------------------------------------------------

    def _find_heading_range(self, lines: list, headings: list):
        start = -1
        for i, line in enumerate(lines):
            if line.strip() in headings:
                start = i
                break
        if start == -1:
            return None
        end = len(lines)
        for i in range(start + 1, len(lines)):
            if lines[i].startswith("## "):
                end = i
                break
        return start, end

    def _replace_section_body(self, status_text: str, headings: list, new_body: str) -> str:
        lines   = status_text.splitlines()
        section = self._find_heading_range(lines, headings)
        if not section:
            return status_text
        start, end  = section
        body_lines  = [l.rstrip() for l in new_body.strip().splitlines()] if new_body.strip() else [""]
        new_lines   = lines[: start + 1] + body_lines + lines[end:]
        return "\n".join(new_lines) + ("\n" if status_text.endswith("\n") else "")

    def _set_current_iteration(self, status_text: str) -> str:
        lines   = status_text.splitlines()
        section = self._find_heading_range(lines, CURRENT_ITER_HEADERS)
        if not section:
            return status_text
        start, end = section
        iter_line  = f"iter-{self.iteration:04d}"
        if start + 1 < end:
            lines[start + 1] = iter_line
            if end - (start + 1) > 1:
                del lines[start + 2: end]
        else:
            lines.insert(start + 1, iter_line)
        return "\n".join(lines) + ("\n" if status_text.endswith("\n") else "")

    def _insert_todo_if_missing(self, status_text: str, todo_item: str) -> str:
        if f"- [ ] {todo_item}" in status_text or f"- [x] {todo_item}" in status_text:
            return status_text
        lines = status_text.splitlines()
        todo_section = self._find_heading_range(lines, TODO_HEADERS)
        if todo_section:
            _, end = todo_section
            lines.insert(end, f"- [ ] {todo_item}")
            return "\n".join(lines) + ("\n" if status_text.endswith("\n") else "")
        next_section = self._find_heading_range(lines, NEXT_PLAN_HEADERS)
        if next_section:
            start, _ = next_section
            lines.insert(start, f"- [ ] {todo_item}")
            return "\n".join(lines) + ("\n" if status_text.endswith("\n") else "")
        return status_text + f"\n- [ ] {todo_item}\n"

    def _update_status(self, impl_result: dict):
        status_path = self.project_dir / "status.md"
        status = status_path.read_text(encoding="utf-8")
        for done in impl_result.get("todo_done", []):
            status = status.replace(f"- [ ] {done}", f"- [x] {done}")
        for add in impl_result.get("todo_add", []):
            status = self._insert_todo_if_missing(status, add)
        status = self._set_current_iteration(status)
        next_plan = impl_result.get("status_update", "").strip()
        if next_plan:
            status = self._replace_section_body(status, NEXT_PLAN_HEADERS, next_plan)
        status_path.write_text(status, encoding="utf-8")

    def _append_eval_log(self, action_type: str, commit_msg: str, result: str, note: str = ""):
        log_path = self.project_dir / "eval_log.md"
        elapsed  = datetime.now() - self.started_at
        entry = f"""
## iter-{self.iteration:04d} | {datetime.now().strftime('%Y-%m-%d %H:%M')} | 経過{str(elapsed).split('.')[0]}
- アクション: {action_type}
- コミット: {commit_msg}
- 結果: {result}
- 備考: {note}
"""
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(entry)

    # ------------------------------------------------------------------
    # 1イテレーション
    # ------------------------------------------------------------------

    def _run_single_attempt(self, feedback: str = "") -> tuple:
        context     = self._read_context()
        impl_result = self.agent.ask_json(
            self._implementer_prompt(context, feedback), role="implementer"
        )
        if not impl_result or "files" not in impl_result:
            return {}, {"passed": False, "reason": "実装AIの出力が不正", "note": ""}

        print(f"[orchestrator] アクション: {impl_result.get('action_type')}")
        if self.show_thought:
            thought = impl_result.get("thought", "")[: self.thought_preview_chars]
            print(f"[orchestrator] 思考: {thought}...")

        self.executor.write_files(impl_result["files"])
        print(f"[orchestrator] {len(impl_result['files'])}ファイル書き込み完了")

        eval_result = self.evaluator.evaluate(
            impl_result.get("implemented_features", []),
            impl_result.get("ui_elements", []),
            impl_result.get("assertions", []),
        )
        return impl_result, eval_result

    def _run_iteration(self):
        self.iteration += 1
        elapsed = datetime.now() - self.started_at

        print(f"\n{'=' * 50}")
        print(f"  イテレーション {self.iteration:04d}  |  経過時間 {str(elapsed).split('.')[0]}")
        print(f"{'=' * 50}")

        # クールダウン解除済みの上位モデルへ復帰チェック
        self.agent.refresh_model()

        pre_snapshot = self._take_snapshot("pre")

        impl_result, eval_result = self._run_single_attempt()

        if not eval_result.get("passed"):
            reason = eval_result.get("reason", "不明なエラー")
            print(f"[orchestrator] 1回目の試行 失敗: {reason}")
            self._rollback(pre_snapshot)
            impl_result, eval_result = self._run_single_attempt(
                feedback=f"評価失敗の理由: {reason}"
            )

        if eval_result.get("passed"):
            print("[orchestrator] ✅ PASS")
            self._update_status(impl_result)
            commit_msg = impl_result.get("commit_message", f"iter-{self.iteration:04d}")
            committed  = self.git.commit(commit_msg, self.iteration)
            self.no_change_streak    = 0 if committed else self.no_change_streak + 1
            self.consecutive_passes += 1
            if self.git.should_push(self.iteration):
                self.git.push()
            self._take_snapshot("post-pass")
            self._append_eval_log(
                impl_result.get("action_type", ""), commit_msg, "PASS", eval_result.get("note", "")
            )
        else:
            reason = eval_result.get("reason", "評価失敗")
            print(f"[orchestrator] ❌ FAIL (リトライ後も失敗): {reason}")
            self._rollback(pre_snapshot)
            self.consecutive_passes = 0
            self._take_snapshot("post-fail")
            self._append_eval_log(
                impl_result.get("action_type", ""),
                impl_result.get("commit_message", ""),
                "FAIL",
                reason,
            )

    # ------------------------------------------------------------------
    # 停止判定
    # ------------------------------------------------------------------

    def _should_stop(self) -> tuple:
        if self.max_iterations > 0 and self.iteration >= self.max_iterations:
            return True, f"最大イテレーション数 {self.max_iterations} 回に達しました"

        if self.max_minutes > 0:
            elapsed_min = (datetime.now() - self.started_at).total_seconds() / 60
            if elapsed_min >= self.max_minutes:
                return True, f"実行時間 {self.max_minutes} 分に達しました（実際: {elapsed_min:.1f}分）"

        if self.stop_after_consecutive_passes > 0 and self.consecutive_passes >= self.stop_after_consecutive_passes:
            return True, f"連続PASS {self.stop_after_consecutive_passes} 回に達しました"

        if self.stop_after_no_change_iterations > 0 and self.no_change_streak >= self.stop_after_no_change_iterations:
            return True, f"変更なし {self.stop_after_no_change_iterations} 回に達しました"

        return False, ""

    # ------------------------------------------------------------------
    # メインループ
    # ------------------------------------------------------------------

    def run(self):
        # 停止条件サマリを表示
        conditions = []
        if self.max_minutes > 0:
            conditions.append(f"{self.max_minutes}分")
        if self.max_iterations > 0:
            conditions.append(f"{self.max_iterations}回")
        if not conditions:
            conditions.append("無制限（Ctrl+C で停止）")
        print(f"[orchestrator] 停止条件: {' / '.join(conditions)}")
        if self.interval > 0:
            print(f"[orchestrator] イテレーション間隔: {self.interval}秒")
        else:
            print("[orchestrator] イテレーション間隔: なし（連続実行）")

        try:
            while True:
                self._run_iteration()

                stop, reason = self._should_stop()
                if stop:
                    print(f"\n[orchestrator] 停止: {reason}")
                    break

                if self.interval > 0:
                    print(f"[orchestrator] {self.interval}秒待機...")
                    time.sleep(self.interval)

        except KeyboardInterrupt:
            print("\n[orchestrator] 中断されました")

        finally:
            elapsed = datetime.now() - self.started_at
            print(f"[orchestrator] 総イテレーション数: {self.iteration}回")
            print(f"[orchestrator] 総経過時間: {str(elapsed).split('.')[0]}")
            self.evaluator.close()
            self.git.push()
            print("[orchestrator] 最新状態をGitHubにpushしました")
