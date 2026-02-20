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
        
        # 新しいSDKのクライアント初期化
        self.client = genai.Client(api_key=api_key)

        self.config = load_config()
        self.switch_wait = self.config.get("model_switch_wait", 10)
        
        agent_cfg = self.config.get("agent", {})
        self.max_retries = agent_cfg.get("max_retries", 5)
        self.json_max_retries = self.config.get("json_max_retries", 2)
        self.rate_cooldown_sec = agent_cfg.get("rate_cooldown_seconds", 60)

        raw_models = self.config
        self.models = self._filter_supported_models(raw_models)
        if not self.models:
            raise RuntimeError("使用可能なモデルがありません。APIキーを確認してください。")

        self._cooldown_until: dict = {m: 0.0 for m in self.models}
        
        model_names = " > ".join(m for m in self.models)
        print(f" 利用可能モデル（優先順）: {model_names}")
        self.model_name = self._pick_best_model()

    def _list_available_generate_models(self) -> set:
        available = set()
        try:
            for m in self.client.models.list():
                name = m.name
                if name.startswith("models/"):
                    name = name
                available.add(name)
        except Exception as e:
            print(f" 警告: モデル一覧取得に失敗 ({e})。設定をそのまま使用します")
        return available

    def _filter_supported_models(self, configured: list) -> list:
        available = self._list_available_generate_models()
        if not available: return configured
        return

    def _pick_best_model(self) -> str:
        now = time.time()
        for m in self.models:
            if self._cooldown_until] <= now:
                return m
        
        earliest = min(self.models, key=lambda m: self._cooldown_until])
        wait = max(0.0, self._cooldown_until] - now)
        print(f" 全モデルがクールダウン中。{wait:.0f}秒待機します...")
        time.sleep(wait + 1)
        return earliest

    def _current_model_name(self) -> str:
        return self.model_name

    def _mark_rate_limited(self, model_name: str):
        until = time.time() + self.rate_cooldown_sec
        self._cooldown_until = until
        self.model_name = self._pick_best_model()

    def refresh_model(self):
        now = time.time()
        cur = self._current_model_name()
        for m in self.models:
            if self._cooldown_until] <= now:
                if m != cur:
                    print(f" 上位モデルに復帰: {cur} → {m}")
                    self.model_name = m
                return

    def ask(self, prompt: str, role: str = "general") -> str:
        for attempt in range(self.max_retries):
            current_name = self._current_model_name()
            try:
                response = self.client.models.generate_content(
                    model=current_name,
                    contents=prompt
                )
                if response.text:
                    return response.text
                raise ValueError("レスポンスが空です")
                
            except Exception as e:
                err_str = str(e).lower()
                if "quota" in err_str or "rate" in err_str or "429" in err_str or "exhausted" in err_str:
                    print(f" レート制限/クォータ検知: {current_name}")
                    time.sleep(self.switch_wait)
                    self._mark_rate_limited(current_name)
                elif "not found" in err_str or "404" in err_str or "unsupported" in err_str:
                    print(f" モデル利用不可: {current_name}。")
                    self._cooldown_until = time.time() + 86400 * 365
                    self.model_name = self._pick_best_model()
                elif attempt < self.max_retries - 1:
                    wait = 5 * (attempt + 1)
                    print(f" エラー ({e})。{wait}秒後リトライ...")
                    time.sleep(wait)
                else:
                    raise RuntimeError(f"API呼び出しの限界に達しました: {e}")

        # Fix: Noneを返さないように例外を発生させる
        raise RuntimeError("有効なレスポンスを取得できませんでした。APIキーの制限などを確認してください。")

    def ask_json(self, prompt: str, role: str = "general") -> dict:
        json_prompt = prompt + "\\n\\nJSONオブジェクト1つだけを返してください。マークダウンのコードブロック不要。説明文不要。"
        for _ in range(self.json_max_retries):
            raw = self.ask(json_prompt, role=role)
            candidate = self._extract_json_candidate(raw)
            if not candidate:
                json_prompt = self._build_retry_prompt(prompt, "JSON抽出失敗")
                continue
            try:
                parsed = json.loads(candidate)
                if role == "implementer":
                    ok, reason = self._validate_implementer_payload(parsed)
                    if not ok:
                        json_prompt = self._build_retry_prompt(prompt, f"スキーマエラー: {reason}")
                        continue
                    return self._normalize_implementer_payload(parsed)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                json_prompt = self._build_retry_prompt(prompt, "JSON構文エラー")
        return {}

    def _build_retry_prompt(self, original_prompt: str, reason: str) -> str:
        return original_prompt + f"\\n\\n前回の出力が不正でした: {reason}\\nJSONオブジェクト1つだけを返してください。"

    def _extract_json_candidate(self, raw: str) -> str:
        text = (raw or "").strip()
        if not text: return ""
        code_blocks = re.findall(r"```(?:json)?\s*(\{*?\})\s*```", text, flags=re.IGNORECASE)
        if code_blocks: return code_blocks.strip()
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end != -1 and end > start:
            return text.strip()
        return ""

    def _validate_implementer_payload(self, payload: Any) -> tuple:
        if not isinstance(payload, dict): return False, "オブジェクトである必要があります"
        if "files" not in payload: return False, "filesがありません"
        return True, ""

    def _normalize_implementer_payload(self, payload: Dict) -> Dict:
        normalized = dict(payload)
        for key in:
            normalized.setdefault(key, "")
        for key in:
            normalized.setdefault(key,