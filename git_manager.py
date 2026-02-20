"""Git/GitHub ユーティリティ。"""

import os
import subprocess
import sys
from pathlib import Path

import requests
import yaml
from dotenv import load_dotenv

load_dotenv()
CONFIG_PATH = Path(__file__).parent / "config.yaml"


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


class GitManager:
    def __init__(self, project_dir: Path, remote_url: str = "", project_slug: str = ""):
        self.project_dir = project_dir
        self.remote_url = remote_url
        self.project_slug = project_slug
        self.config = load_config()
        self.github_token = os.getenv("GITHUB_TOKEN", "")
        self.push_every = self.config["iteration"].get("push_every", 10)
        github_cfg = self.config.get("github", {})
        self.auto_login_with_gh = github_cfg.get("auto_login_with_gh", True)
        self.gh_login_web = github_cfg.get("gh_login_web", True)

    def _run(self, cmd: list, check=False) -> subprocess.CompletedProcess:
        return subprocess.run(
            cmd,
            cwd=self.project_dir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=check,
        )

    def _gh_installed(self) -> bool:
        try:
            result = subprocess.run(
                ["gh", "--version"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            return result.returncode == 0
        except FileNotFoundError:
            return False

    def _gh_available(self) -> bool:
        if not self._gh_installed():
            return False
        result = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        return result.returncode == 0

    def _is_interactive(self) -> bool:
        try:
            return sys.stdin.isatty() and sys.stdout.isatty()
        except Exception:
            return False

    def _try_gh_login(self) -> bool:
        if not self.auto_login_with_gh:
            return False
        if not self._gh_installed():
            return False
        if self._gh_available():
            return True
        if not self._is_interactive():
            print("[git] gh ログインをスキップ（非インタラクティブセッション）")
            return False

        print("[git] gh が未認証です。自動ログインを試みます...")
        cmd = ["gh", "auth", "login", "--hostname", "github.com", "--git-protocol", "https"]
        if self.gh_login_web:
            cmd.append("--web")
        result = subprocess.run(cmd, cwd=self.project_dir, text=True, encoding="utf-8", errors="replace")
        if result.returncode != 0:
            print("[git] gh auth login が失敗またはキャンセルされました")
            return False
        return self._gh_available()

    def _create_repo_via_gh(self, repo_name: str, visibility: str) -> str:
        print(f"[git] GitHub CLI でリポジトリを作成中: {repo_name}")
        result = subprocess.run(
            ["gh", "repo", "create", repo_name, f"--{visibility}", "--source=.", "--remote=origin", "--push"],
            cwd=self.project_dir,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if result.returncode != 0:
            print(f"[git] gh repo create 失敗: {result.stderr}")
            return ""

        url_result = subprocess.run(
            ["gh", "repo", "view", "--json", "url", "-q", ".url"],
            cwd=self.project_dir,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        url = url_result.stdout.strip()
        print(f"[git] ✅ リポジトリ作成完了: {url}")
        return url

    def _create_repo_via_api(self, repo_name: str, visibility: str) -> str:
        if not self.github_token:
            return ""

        print(f"[git] GitHub API でリポジトリを作成中: {repo_name}")
        payload = {"name": repo_name, "private": visibility == "private", "auto_init": False}
        headers = {
            "Authorization": f"token {self.github_token}",
            "Accept": "application/vnd.github.v3+json",
        }
        resp = requests.post("https://api.github.com/user/repos", json=payload, headers=headers, timeout=10)
        if resp.status_code != 201:
            print(f"[git] API 作成失敗: {resp.status_code} {resp.text[:200]}")
            return ""

        clone_url = resp.json().get("clone_url", "")
        html_url = resp.json().get("html_url", "")
        self._run(["git", "remote", "add", "origin", clone_url])
        print(f"[git] ✅ リポジトリ作成完了: {html_url}")
        return clone_url

    def _auto_create_repo(self) -> str:
        visibility = self.config["github"].get("default_visibility", "public")
        repo_name = self.project_slug or "auto-dev-project"

        if self._gh_available() or self._try_gh_login():
            return self._create_repo_via_gh(repo_name, visibility)

        if self.github_token:
            return self._create_repo_via_api(repo_name, visibility)

        print("[git] gh 認証も GITHUB_TOKEN もありません。リポジトリ自動作成をスキップします")
        return ""

    def init(self):
        git_dir = self.project_dir / ".git"

        gitignore = self.project_dir / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text(
                "snapshots/\n__pycache__/\n*.pyc\n.env\n*.egg-info/\n",
                encoding="utf-8"
            )

        if not git_dir.exists():
            self._run(["git", "init"])
            self._run(["git", "checkout", "-b", "main"])
            print("[git] ローカルリポジトリを初期化しました")

        remote_check = self._run(["git", "remote", "get-url", "origin"])
        if remote_check.returncode == 0:
            self.remote_url = remote_check.stdout.strip()
            print(f"[git] 既存リモートを使用: {self.remote_url}")
            return

        if self.remote_url:
            self._run(["git", "remote", "add", "origin", self.remote_url])
            print(f"[git] リモートを設定しました: {self.remote_url}")
        elif self.config["github"].get("auto_create_repo", True):
            created_url = self._auto_create_repo()
            if created_url:
                self.remote_url = created_url
        else:
            print("[git] リモートなし。ローカルコミットのみ")

    def _stage_paths(self):
        stage_candidates = [
            "workspace", "assets", "status.md", "eval_log.md", "spec.md", "brief.txt",
        ]
        existing = [p for p in stage_candidates if (self.project_dir / p).exists()]
        if not existing:
            self._run(["git", "add", "."])
            return
        self._run(["git", "add", "--"] + existing)

    def commit(self, message: str, iteration: int) -> bool:
        self._stage_paths()
        result = self._run(["git", "commit", "-m", f"[iter-{iteration:04d}] {message}"])

        if result.returncode == 0:
            print(f"[git] コミット完了: [iter-{iteration:04d}] {message}")
            return True

        combined = (result.stdout + result.stderr).lower()
        if "nothing to commit" in combined:
            print("[git] コミットする変更がありません")
            return False

        print(f"[git] コミット失敗: {result.stderr[:200]}")
        return False

    def push(self):
        if not self.remote_url:
            print("[git] リモートが設定されていません。pushをスキップします")
            return

        print("[git] GitHubにpush中...")
        result = self._run(["git", "push", "-u", "origin", "main"])
        if result.returncode == 0:
            print("[git] ✅ push完了")
        else:
            print(f"[git] push失敗: {result.stderr[:200]}")

    def should_push(self, iteration: int) -> bool:
        return self.push_every > 0 and iteration % self.push_every == 0
