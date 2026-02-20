"""Initialize project scaffold from brief file."""

import re
from datetime import datetime
from pathlib import Path

from agent import Agent

TEMPLATES_DIR = Path(__file__).parent / "templates"


class Bootstrap:
    def __init__(self, brief_path: Path):
        self.brief_path = brief_path
        self.brief_text = brief_path.read_text(encoding="utf-8")
        self.brief = self._parse_brief()
        self.agent = None

    def _get_agent(self) -> Agent:
        if self.agent is None:
            self.agent = Agent()
        return self.agent

    def _parse_brief(self) -> dict:
        result = {
            "name": "my-project",
            "genre": "website",
            "description": "",
            "todo": [],
            "forbidden": [],
            "github": "",
            "raw": self.brief_text,
        }

        current_key = None
        current_list = []

        key_map = {
            "project name": "name",
            "name": "name",
            "プロジェクト名": "name",
            "genre": "genre",
            "ジャンル": "genre",
            "description": "description",
            "説明": "description",
            "todo": "todo_raw",
            "やってほしいこと": "todo_raw",
            "forbidden": "forbidden_raw",
            "禁止": "forbidden_raw",
            "github": "github",
            "githubリポジトリ": "github",
        }

        for line in self.brief_text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            if ":" in line and not line.startswith("-"):
                if current_key and current_list:
                    result[current_key] = current_list
                    current_list = []

                key, _, value = line.partition(":")
                mapped = key_map.get(key.strip().lower(), key_map.get(key.strip(), key.strip().lower()))
                value = value.strip()

                if value and value not in {"なし", "none", "-"}:
                    result[mapped] = value
                    current_key = None
                else:
                    current_key = mapped
            elif line.startswith("-") and current_key:
                current_list.append(line[1:].strip())

        if current_key and current_list:
            result[current_key] = current_list

        if "todo_raw" in result:
            if isinstance(result["todo_raw"], list):
                result["todo"] = result["todo_raw"]
            elif result["todo_raw"]:
                result["todo"] = [result["todo_raw"]]

        if "forbidden_raw" in result:
            if isinstance(result["forbidden_raw"], list):
                result["forbidden"] = result["forbidden_raw"]
            elif result["forbidden_raw"]:
                result["forbidden"] = [result["forbidden_raw"]]

        slug = re.sub(r"[^a-zA-Z0-9_-]", "-", result["name"].lower())
        slug = re.sub(r"-+", "-", slug).strip("-") or "my-project"
        result["slug"] = slug
        return result

    def _load_template(self, genre: str) -> str:
        base = (TEMPLATES_DIR / "_base_stack.md").read_text(encoding="utf-8")

        genre_file = TEMPLATES_DIR / f"{genre}.md"
        if genre_file.exists():
            genre_tmpl = genre_file.read_text(encoding="utf-8")
        else:
            print(f"[bootstrap] template {genre}.md not found; using website.md")
            genre_tmpl = (TEMPLATES_DIR / "website.md").read_text(encoding="utf-8")

        return base + "\n\n" + genre_tmpl

    def _create_project_dir(self) -> Path:
        project_dir = Path(__file__).parent / "projects" / self.brief["slug"]
        (project_dir / "workspace").mkdir(parents=True, exist_ok=True)
        (project_dir / "assets" / "images").mkdir(parents=True, exist_ok=True)
        (project_dir / "assets" / "fonts").mkdir(parents=True, exist_ok=True)
        (project_dir / "assets" / "icons").mkdir(parents=True, exist_ok=True)
        (project_dir / "snapshots").mkdir(parents=True, exist_ok=True)
        return project_dir

    def _generate_spec(self, template: str) -> str:
        prompt = f"""
Create spec.md for an autonomous web project based on the brief and template.

# brief.txt
{self.brief_text}

# template
{template}

Requirements:
- Respect forbidden rules
- Include concrete stack and implementation boundaries
- Include clear iteration checklist for the implementer AI
- Output markdown only
"""
        return self._get_agent().ask(prompt, role="bootstrap")

    def _generate_initial_status(self) -> str:
        if self.brief["todo"]:
            todo_lines = "\n".join(f"- [ ] {t}" for t in self.brief["todo"])
        else:
            todo_lines = "- [ ] Create index.html and initial layout"

        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        return f"""# status.md

## Project
{self.brief['name']}

## Current Iteration
iter-0000

## TODO
{todo_lines}

## Next Iteration Plan
Create initial HTML/CSS/JS skeleton and verify rendering.

## Notes
Initialized at {now}
"""

    def _generate_initial_eval_log(self) -> str:
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        return f"""# eval_log.md

## iter-0000 | {now}
- action: bootstrap
- result: INIT
- note: initial project scaffold created
"""

    def run(self) -> dict:
        print(f"[bootstrap] project: {self.brief['name']}")
        print(f"[bootstrap] genre: {self.brief['genre']}")

        project_dir = self._create_project_dir()
        print(f"[bootstrap] project_dir: {project_dir}")

        template = self._load_template(self.brief["genre"])

        print("[bootstrap] generating spec.md...")
        spec_content = self._generate_spec(template)
        (project_dir / "spec.md").write_text(spec_content, encoding="utf-8")

        (project_dir / "status.md").write_text(self._generate_initial_status(), encoding="utf-8")
        (project_dir / "eval_log.md").write_text(self._generate_initial_eval_log(), encoding="utf-8")
        (project_dir / "brief.txt").write_text(self.brief_text, encoding="utf-8")

        return {
            "name": self.brief["name"],
            "slug": self.brief["slug"],
            "genre": self.brief["genre"],
            "github": self.brief.get("github", ""),
            "project_dir": project_dir,
            "brief": self.brief,
        }
