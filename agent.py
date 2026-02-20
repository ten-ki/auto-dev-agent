"""Gemini APIラッパー。起動時チェック + クールダウン方式のモデル自動復帰付き。

【モデル選択の仕組み】
  起動時  : list_models() で使えないモデルを除外（1回だけ）
  各呼び出し: クールダウン中でないモデルの中で最上位（index最小）を選ぶ
  レート制限: そのモデルにクールダウン時刻を記録 → 次の上位モデルへ降格
  時間経過 : クールダウン解除 → 自動的に上位モデルに復帰
"""

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

import google.generativeai as genai
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
        genai.configure(api_key=api_key)

        self.config = load_config()
        self.switch_wait     = self.config.get("model_switch_wait", 10)
        self.exhausted_wait  = self.config.get("all_models_exhausted_wait", 3600)

        agent_cfg = self.config.get("agent", {})
        eval_cfg  = self.config.get("evaluation", {})
        self.max_retries       = agent_cfg.get("max_retries", eval_cfg.get("max_retries", 3))
        self.json_max_retries  = self.config.get("json_max_retries", 2)
        self.rate_cooldown_sec = agent_cfg.get("rate_cooldown_seconds", 60)

        # ① 起動時に1回だけ使えるモデルをフィルタリング
        raw_models = self.config["models"]
        self.models: list = self._filter_supported_models(raw_models)
        if not self.models:
            raise RuntimeError("使用可能なモデルがありません。APIキーとモデル設定を確認してください。")

        # ② モデルごとのクールダウン解除時刻（Unix timestamp）。0 = 使用可能
        self._cooldown_until: dict = {m["name"]: 0.0 for m in self.models}

        model_names = " > ".join(m["name"] for m in self.models)
        print(f"[agent] 利用可能モデル（優先順）: {model_names}")

        self.model = self._pick_best_model()

    # ------------------------------------------------------------------
    # 起動時チェック（1回だけ）
    # ------------------------------------------------------------------

    def _list_available_generate_models(self) -> set:
        available = set()
        try:
            for m in genai.list_models():
                methods = set(getattr(m, "supported_generation_methods", []) or [])
                if "generateContent" in methods:
                    name = getattr(m, "name", "")
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

    def _pick_best_model(self) -> genai.GenerativeModel:
        """クールダウン中でない最上位モデルを返す。全滅なら最短クールダウン解除まで待つ。"""
        now = time.time()

        for m in self.models:
            name = m["name"]
            if self._cooldown_until[name] <= now:
                print(f"[agent] モデル選択: {name}")
                return genai.GenerativeModel(name)

        # 全モデルがクールダウン中 → 一番早く解除されるまで待機
        earliest = min(self.models, key=lambda m: self._cooldown_until[m["name"]])
        earliest_name = earliest["name"]
        wait = max(0.0, self._cooldown_until[earliest_name] - now)
        print(f"[agent] 全モデルがクールダウン中。{wait:.0f}秒後に {earliest_name} が復帰します...")
        time.sleep(wait + 1)
        print(f"[agent] クールダウン解除。モデル選択: {earliest_name}")
        return genai.GenerativeModel(earliest_name)

    def _current_model_name(self) -> str:
        # GenerativeModel.model_name は "models/gemini-xxx" 形式
        return self.model.model_name.replace("models/", "")

    def _mark_rate_limited(self, model_name: str):
        """レート制限を記録し、次の上位モデルへ切り替える。"""
        until = time.time() + self.rate_cooldown_sec
        self._cooldown_until[model_name] = until
        until_str = datetime.fromtimestamp(until).strftime("%H:%M:%S")
        print(f"[agent] {model_name} をクールダウン登録（{until_str} まで）")
        self.model = self._pick_best_model()

    def refresh_model(self):
        """イテレーション開始時に呼ぶ。クールダウン解除済みの上位モデルがあれば復帰。"""
        now = time.time()
        cur = self._current_model_name()

        for m in self.models:
            name = m["name"]
            if self._cooldown_until[name] <= now:
                if name != cur:
                    print(f"[agent] 上位モデルに復帰: {cur} → {name}")
                    self.model = genai.GenerativeModel(name)
                return  # 最上位が使えるなら終了

    # ------------------------------------------------------------------
    # API 呼び出し
    # ------------------------------------------------------------------

    def ask(self, prompt: str, role: str = "general") -> str:
        """Geminiにプロンプトを投げてテキストを返す。"""
        _ = role
        for attempt in range(self.max_retries):
            current_name = self._current_model_name()
            try:
                response = self.model.generate_content(prompt)
                return response.text

            except Exception as e:
                err_str = str(e).lower()

                if "quota" in err_str or "rate" in err_str or "429" in err_str:
                    print(f"[agent] レート制限検知: {current_name}")
                    time.sleep(self.switch_wait)
                    self._mark_rate_limited(current_name)
                    # attempt はリセットせず継続（次のモデルで再試行）

                elif "not found" in err_str or "404" in err_str or "is not supported for generatecontent" in err_str:
                    print(f"[agent] モデル利用不可: {current_name}。永続スキップします")
                    # 実質的に永久にクールダウン
                    self._cooldown_until[current_name] = time.time() + 86400 * 365
                    self.model = self._pick_best_model()

                elif attempt < self.max_retries - 1:
                    wait = 5 * (attempt + 1)
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
