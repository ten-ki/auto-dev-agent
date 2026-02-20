"""Gemini APIラッパー。起動時チェック + クールダウン方式のモデル自動復帰付き（google-genai SDK対応版）。"""

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

from google import genai
import yaml
from dotenv import load_dotenv

load_dotenv()

CONFIG_PATH = Path(__file__).parent / "config.yaml"


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


class Agent:
    def __init__(self):
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise ValueError(".env に GEMINI_API_KEY が設定されていません")

        self.client = genai.Client(api_key=api_key)

        self.config = load_config()
        self.switch_wait     = self.config.get("model_switch_wait", 10)
        self.exhausted_wait  = self.config.get("all_models_exhausted_wait", 3600)

        agent_cfg = self.config.get("agent", {})
        eval_cfg  = self.config.get("evaluation", {})
        self.max_retries       = agent_cfg.get("max_retries", eval_cfg.get("max_retries", 3))
        self.json_max_retries  = self.config.get("json_max_retries", 2)
        self.rate_cooldown_sec = agent_cfg.get("rate_cooldown_seconds", 60)

        # ① 起動時に1回だけ使えるモデルをフィルタリング
        raw_models = self.config.get("models", [])
        self.models: list = self._filter_supported_models(raw_models)
        if not self.models:
            raise RuntimeError("使用可能なモデルがありません。APIキーとモデル設定を確認してください。")

        # ② モデル名 → クールダウン解除時刻 (Unix timestamp)。0 = 使用可能
        self._cooldown_until: dict = {m["name"]: 0.0 for m in self.models}

        model_names = " > ".join(m["name"] for m in self.models)
        print(f"[agent] 利用可能モデル（優先順）: {model_names}")

        self.model_name = self._pick_best_model_name()

    # ------------------------------------------------------------------
    # 起動時チェック（1回だけ）
    # ------------------------------------------------------------------

    def _list_available_generate_models(self) -> set:
        available = set()
        try:
            for m in self.client.models.list():
                name = getattr(m, "name", "") or ""
                if name.startswith("models/"):
                    name = name[len("models/"):]
                if name:
                    available.add(name)
        except Exception as e:
            print(f"[agent] 警告: モデル一覧取得に失敗 ({e})。設定をそのまま使用します")
        return available

    def _filter_supported_models(self, configured: list) -> list:
        print("[agent] 利用可能モデルをチェック中...")
        available = self._list_available_generate_models()
        if not available:
            print("[agent] モデル一覧取得不可。設定ファイルのモデルをそのまま使用します")
            return configured

        filtered = [m for m in configured if m.get("name") in available]
        removed  = [m["name"] for m in configured if m.get("name") not in available]
        if removed:
            print(f"[agent] 非対応モデルをスキップ: {', '.join(removed)}")
        return filtered

    # ------------------------------------------------------------------
    # イテレーションごとのモデル選択（クールダウン考慮）
    # ------------------------------------------------------------------

    def _pick_best_model_name(self) -> str:
        """クールダウン中でない最上位モデル名を返す。全滅なら最短クールダウン解除まで待つ。"""
        now = time.time()

        for m in self.models:
            name = m["name"]
            if self._cooldown_until[name] <= now:
                print(f"[agent] モデル選択: {name}")
                return name

        # 全モデルがクールダウン中 → 一番早く解除されるまで待機
        earliest_name = min(self.models, key=lambda m: self._cooldown_until[m["name"]])["name"]
        wait = max(0.0, self._cooldown_until[earliest_name] - now)
        print(f"[agent] 全モデルがクールダウン中。{wait:.0f}秒後に {earliest_name} が復帰します...")
        time.sleep(wait + 1)
        print(f"[agent] クールダウン解除。モデル選択: {earliest_name}")
        return earliest_name

    def _current_model_name(self) -> str:
        return self.model_name

    def _mark_rate_limited(self, model_name: str):
        """レート制限を記録し、次の上位モデルへ切り替える（デフォルト秒数）。"""
        self._mark_rate_limited_with_wait(model_name, float(self.rate_cooldown_sec))

    def refresh_model(self):
        """イテレーション開始時に呼ぶ。クールダウン解除済みの上位モデルがあれば復帰。"""
        now = time.time()
        cur = self._current_model_name()

        for m in self.models:
            name = m["name"]
            if self._cooldown_until[name] <= now:
                if name != cur:
                    print(f"[agent] 上位モデルに復帰: {cur} → {name}")
                    self.model_name = name
                return

    # ------------------------------------------------------------------
    # API 呼び出し
    # ------------------------------------------------------------------

    def _parse_retry_delay(self, e: Exception) -> float:
        """APIエラーから推奨待機秒数を取り出す。なければ switch_wait を返す。"""
        import re as _re
        # "Please retry in 25.26s" / "retryDelay: '25s'" 形式を探す
        m = _re.search(r"retry[^0-9]*?(\d+(?:\.\d+)?)\s*s", str(e), _re.IGNORECASE)
        if m:
            return float(m.group(1)) + 2  # 少し余裕を持つ
        return float(self.switch_wait)

    def _mark_rate_limited_with_wait(self, model_name: str, wait_sec: float):
        """指定秒数でクールダウン登録して次の上位モデルへ切り替える。"""
        until = time.time() + wait_sec
        self._cooldown_until[model_name] = until
        until_str = datetime.fromtimestamp(until).strftime("%H:%M:%S")
        print(f"[agent] {model_name} クールダウン登録（{until_str} まで / {wait_sec:.0f}秒）")
        self.model_name = self._pick_best_model_name()

    def ask(self, prompt: str, role: str = "general") -> str:
        """Geminiにプロンプトを投げてテキストを返す。レート制限は無限リトライ。"""
        _ = role
        non_rate_attempts = 0

        while True:
            current_name = self._current_model_name()
            try:
                response = self.client.models.generate_content(
                    model=current_name,
                    contents=prompt
                )
                if response.text:
                    return response.text
                raise ValueError("レスポンスのテキストが空でした")

            except Exception as e:
                err_str = str(e).lower()

                if "quota" in err_str or "rate" in err_str or "429" in err_str or "exhausted" in err_str:
                    # APIが推奨する待機時間を使う（"retry in 25s" などを解析）
                    wait = self._parse_retry_delay(e)
                    print(f"[agent] レート制限検知: {current_name}。{wait:.0f}秒クールダウン登録")
                    self._mark_rate_limited_with_wait(current_name, wait)
                    non_rate_attempts = 0  # レート制限はモデル変えれば続けられるのでリセット

                elif "not found" in err_str or "404" in err_str or "is not supported" in err_str:
                    print(f"[agent] モデル利用不可: {current_name}。永続スキップします")
                    self._cooldown_until[current_name] = time.time() + 86400 * 365
                    self.model_name = self._pick_best_model_name()

                else:
                    non_rate_attempts += 1
                    if non_rate_attempts < self.max_retries:
                        wait = 5 * non_rate_attempts
                        print(f"[agent] エラー ({e})。{wait}秒後リトライ...")
                        time.sleep(wait)
                    else:
                        print(f"[agent] リトライ上限に達しました: {e}")
                        raise

    def ask_json(self, prompt: str, role: str = "general") -> dict:
        """JSONオブジェクトを期待するask。"""
        json_prompt = (
            prompt
            + "\n\nJSONオブジェクト1つだけを返してください。マークダウンのコードブロック不要。説明文不要。"
        )

        for _ in range(self.json_max_retries):
            raw = self.ask(json_prompt, role=role)
            candidate = self._extract_json_candidate(raw)
            if not candidate:
                print("[agent] JSONの抽出に失敗。リトライします...")
                json_prompt = self._build_retry_prompt(prompt, "JSONオブジェクトの抽出に失敗")
                continue

            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError as e:
                print(f"[agent] JSONパースエラー: {e}")
                print(f"[agent] レスポンス先頭: {candidate[:200]}...")
                json_prompt = self._build_retry_prompt(prompt, "JSON構文エラー")
                continue

            if role == "implementer":
                ok, reason = self._validate_implementer_payload(parsed)
                if not ok:
                    print(f"[agent] 実装AIの出力が不正: {reason}")
                    json_prompt = self._build_retry_prompt(prompt, f"スキーマエラー: {reason}")
                    continue
                return self._normalize_implementer_payload(parsed)

            if isinstance(parsed, dict):
                return parsed

            json_prompt = self._build_retry_prompt(prompt, "トップレベルはJSONオブジェクトである必要があります")

        return {}

    # ------------------------------------------------------------------
    # ユーティリティ
    # ------------------------------------------------------------------

    def _build_retry_prompt(self, original_prompt: str, reason: str) -> str:
        return (
            original_prompt
            + f"\n\n前回の出力が不正でした: {reason}\n"
              "再試行の要件:\n"
              "- JSONオブジェクト1つだけを返す\n"
              "- マークダウンのコードブロック不要\n"
              "- JSON前後に説明文を入れない\n"
        )

    def _extract_json_candidate(self, raw: str) -> str:
        text = (raw or "").strip()
        if not text:
            return ""
        code_blocks = re.findall(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text, flags=re.IGNORECASE)
        if code_blocks:
            return code_blocks[0].strip()
        start = text.find("{")
        end   = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return ""
        return text[start:end + 1].strip()

    def _validate_implementer_payload(self, payload: Any) -> tuple:
        if not isinstance(payload, dict):
            return False, "ペイロードはオブジェクトである必要があります"
        required = ["files", "commit_message", "status_update", "todo_done", "todo_add"]
        for key in required:
            if key not in payload:
                return False, f"必須キーがありません: {key}"
        files = payload.get("files")
        if not isinstance(files, list):
            return False, "files は配列である必要があります"
        for i, item in enumerate(files):
            if not isinstance(item, dict):
                return False, f"files[{i}] はオブジェクトである必要があります"
            if not isinstance(item.get("path"), str) or not item["path"].strip():
                return False, f"files[{i}].path が不正です"
            if not isinstance(item.get("content"), str):
                return False, f"files[{i}].content は文字列である必要があります"
        for key in ["commit_message", "status_update"]:
            if not isinstance(payload.get(key), str):
                return False, f"{key} は文字列である必要があります"
        for key in ["todo_done", "todo_add", "implemented_features", "ui_elements"]:
            value = payload.get(key, [])
            if not isinstance(value, list):
                return False, f"{key} は配列である必要があります"
            if any(not isinstance(v, str) for v in value):
                return False, f"{key} の要素は文字列である必要があります"
        assertions = payload.get("assertions", [])
        if not isinstance(assertions, list):
            return False, "assertions は配列である必要があります"
        for i, a in enumerate(assertions):
            if not isinstance(a, dict):
                return False, f"assertions[{i}] はオブジェクトである必要があります"
            if not isinstance(a.get("type", ""), str):
                return False, f"assertions[{i}].type は文字列である必要があります"
        return True, ""

    def _normalize_implementer_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        normalized = dict(payload)
        normalized.setdefault("thought", "")
        normalized.setdefault("action_type", "add_feature")
        normalized.setdefault("implemented_features", [])
        normalized.setdefault("ui_elements", [])
        normalized.setdefault("assertions", [])
        return normalized
