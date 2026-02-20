"""生成されたワークスペースの評価器。
ブラウザインスタンスを使い回して高速化（毎回起動しない）。
評価AIへのAPI呼び出しは不要。静的チェック + Playwright直実行のみ。
"""

from html.parser import HTMLParser
from pathlib import Path
from typing import Dict, List, Optional, Set

import yaml

CONFIG_PATH = Path(__file__).parent / "config.yaml"


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


class _IndexScanner(HTMLParser):
    """id/class の存在チェック用の簡易HTMLスキャナー。"""

    def __init__(self) -> None:
        super().__init__()
        self.ids: Set[str] = set()
        self.classes: Set[str] = set()

    def handle_starttag(self, tag, attrs):
        attr_map = dict(attrs)
        if eid := attr_map.get("id", ""):
            self.ids.add(eid)
        if cls := attr_map.get("class", ""):
            for c in cls.split():
                self.classes.add(c)


class Evaluator:
    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.config    = load_config()
        self.eval_cfg  = self.config.get("evaluation", {})

        # Playwright ブラウザを起動時に1回だけ立ち上げて使い回す
        self._pw      = None
        self._browser = None
        self._init_browser()

    def _init_browser(self):
        if not self.eval_cfg.get("use_playwright", True):
            return
        try:
            from playwright.sync_api import sync_playwright
            self._pw      = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=True)
            print("[evaluator] Playwright ブラウザ起動（使い回しモード）")
        except Exception as e:
            print(f"[evaluator] Playwright 利用不可: {e}。静的チェックのみ実行します")
            self._pw = self._browser = None

    def close(self):
        """Orchestrator の終了時に呼ぶ。"""
        try:
            if self._browser:
                self._browser.close()
            if self._pw:
                self._pw.stop()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 静的チェック（超高速・毎回実行）
    # ------------------------------------------------------------------

    def _check_required_files(self) -> Dict:
        if not (self.workspace / "index.html").exists():
            return {"ok": False, "reason": "index.html が存在しません"}
        return {"ok": True}

    def _check_ui_elements(self, ui_elements: List[str]) -> Dict:
        if not ui_elements:
            return {"ok": True, "missing": [], "note": ""}

        html   = (self.workspace / "index.html").read_text(encoding="utf-8", errors="ignore")
        parser = _IndexScanner()
        parser.feed(html)

        missing, skipped = [], []
        for sel in ui_elements:
            if sel.startswith("#"):
                if sel[1:] not in parser.ids:
                    missing.append(sel)
            elif sel.startswith("."):
                if sel[1:] not in parser.classes:
                    missing.append(sel)
            else:
                skipped.append(sel)

        return {
            "ok":      len(missing) == 0,
            "missing": missing,
            "note":    f"スキップ: {', '.join(skipped)}" if skipped else "",
        }

    def _run_assertions(self, assertions: List[dict]) -> Dict:
        if not assertions:
            return {"ok": True, "note": ""}

        failures = []
        for i, a in enumerate(assertions):
            if not isinstance(a, dict):
                failures.append(f"assertions[{i}] がオブジェクトではありません")
                continue
            atype = a.get("type", "")

            if atype == "file_exists":
                path = str(a.get("path", "")).strip()
                if not path or not (self.workspace / path).exists():
                    failures.append(f"file_exists 失敗: {path}")

            elif atype == "text_in_file":
                path, text = str(a.get("path", "")).strip(), str(a.get("text", ""))
                target = self.workspace / path
                if not path or not target.exists():
                    failures.append(f"text_in_file: ファイルなし: {path}")
                elif text not in target.read_text(encoding="utf-8", errors="ignore"):
                    failures.append(f"text_in_file: テキストなし in {path}")

            elif atype == "selector_exists":
                sel = str(a.get("selector", "")).strip()
                if not self._check_ui_elements([sel])["ok"]:
                    failures.append(f"selector_exists 失敗: {sel}")

            else:
                failures.append(f"未対応の assertion type: {atype}")

        if failures:
            return {"ok": False, "reason": "; ".join(failures)}
        return {"ok": True, "note": f"assertions 全通過: {len(assertions)}件"}

    # ------------------------------------------------------------------
    # Playwright スモークテスト（ブラウザ使い回し・最小待機）
    # ------------------------------------------------------------------

    def _playwright_smoke(self) -> Dict:
        if not self.eval_cfg.get("use_playwright", True):
            return {"ok": True, "note": "Playwright無効"}
        if self._browser is None:
            return {"ok": True, "note": "Playwright未起動。静的チェックのみ"}

        timeout_ms = int(self.eval_cfg.get("test_timeout_seconds", 10)) * 1000
        index_uri  = (self.workspace / "index.html").resolve().as_uri()
        page       = None

        try:
            page = self._browser.new_page()

            page_errors, console_errors = [], []
            page.on("pageerror", lambda e: page_errors.append(str(e)))
            page.on("console",   lambda m: console_errors.append(m.text) if m.type == "error" else None)

            page.goto(index_uri, wait_until="domcontentloaded", timeout=timeout_ms)
            # 最小限の待機（50ms）
            page.wait_for_timeout(50)

            # ボタンを1つだけクリック
            clicked = 0
            if page.locator("button").count() > 0:
                try:
                    page.locator("button").first.click(timeout=800)
                    clicked = 1
                except Exception:
                    pass

            body_len = len(page.locator("body").inner_text())

            if page_errors:
                return {"ok": False, "reason": f"ランタイムエラー: {page_errors[0]}"}
            if console_errors:
                return {"ok": False, "reason": f"コンソールエラー: {console_errors[0]}"}
            if body_len == 0:
                return {"ok": False, "reason": "body が空（真っ白）"}

            return {"ok": True, "note": f"スモークテスト通過 (body:{body_len}chars clicks:{clicked})"}

        except Exception as e:
            return {"ok": False, "reason": f"Playwright エラー: {e}"}

        finally:
            if page:
                try:
                    page.close()
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # 総合評価エントリポイント
    # ------------------------------------------------------------------

    def evaluate(
        self,
        implemented_features: List[str],
        ui_elements: List[str],
        assertions: Optional[List[dict]] = None,
    ) -> dict:

        # ① index.html の存在確認
        r = self._check_required_files()
        if not r["ok"]:
            return {"passed": False, "reason": r["reason"], "note": ""}

        # ② UI要素の静的チェック
        ui = self._check_ui_elements(ui_elements)
        if not ui["ok"]:
            return {"passed": False, "reason": f"UI要素なし: {', '.join(ui['missing'])}", "note": ui["note"]}

        # ③ assertions
        ar = self._run_assertions(assertions or [])
        if not ar["ok"]:
            return {"passed": False, "reason": ar["reason"], "note": ""}

        # ④ Playwright スモークテスト
        smoke = self._playwright_smoke()
        if not smoke["ok"]:
            return {"passed": False, "reason": smoke["reason"], "note": ""}

        notes = [f"機能数:{len(implemented_features)}"]
        for n in [ui.get("note"), ar.get("note"), smoke.get("note")]:
            if n:
                notes.append(n)

        return {"passed": True, "reason": "", "note": " | ".join(notes)}
