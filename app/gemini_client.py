"""Databricks FMAPI Gemini クライアント.

シーン (フレーム + 音声) から transcript / summary / features を取得する。
"""
from __future__ import annotations

import base64
import json
import logging
import os
from typing import List

import requests
from databricks.sdk import WorkspaceClient

logger = logging.getLogger(__name__)

GEMINI_ENDPOINT = os.environ.get("GEMINI_ENDPOINT", "databricks-gemini-2-5-flash")
EMBEDDING_ENDPOINT = os.environ.get("EMBEDDING_ENDPOINT", "databricks-gte-large-en")

_PROMPT = """この動画シーンを分析し、必ず次の JSON 1 つだけで応答してください。
- Markdown コードフェンス禁止。
- summary の中に JSON を入れ子で書かないこと。
- summary は 2-3 文の日本語、features は文字列配列。

{
  "transcript": "音声を聞き取った日本語の文字起こし。音声が無ければ空文字。",
  "summary": "シーンの要約 (日本語2-3文)。",
  "features": ["タグ1", "タグ2", "..."]
}
"""


class GeminiClient:
    def __init__(self):
        self.w = WorkspaceClient()
        self.host = self.w.config.host.rstrip("/")

    def _headers(self) -> dict:
        h = self.w.config.authenticate()
        h["Content-Type"] = "application/json"
        return h

    def analyze_scene(self, frames_jpeg: List[bytes], audio_wav: bytes | None = None) -> dict:
        """フレーム画像 (+任意で音声) を Gemini に渡し、構造化 JSON を返す."""
        content: list = [{"type": "text", "text": _PROMPT}]

        for jpg in frames_jpeg:
            b64 = base64.b64encode(jpg).decode()
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
            })

        if audio_wav:
            b64 = base64.b64encode(audio_wav).decode()
            content.append({
                "type": "input_audio",
                "input_audio": {"data": b64, "format": "wav"},
            })

        url = f"{self.host}/serving-endpoints/{GEMINI_ENDPOINT}/invocations"
        body = {
            "messages": [{"role": "user", "content": content}],
            "max_tokens": 2048,
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        }
        resp = requests.post(url, headers=self._headers(), json=body, timeout=180)
        if resp.status_code != 200:
            # 音声入力が拒否された場合は画像のみで再試行
            if audio_wav and resp.status_code in (400, 422):
                logger.warning("Audio input rejected (%s). Retrying without audio: %s",
                               resp.status_code, resp.text[:300])
                return self.analyze_scene(frames_jpeg, audio_wav=None)
            raise RuntimeError(f"Gemini error {resp.status_code}: {resp.text[:500]}")

        data = resp.json()
        text = data["choices"][0]["message"]["content"]
        return self._parse_json(text)

    @staticmethod
    def _parse_json(text: str) -> dict:
        """Gemini 出力から JSON を安全に抽出。
        Gemini が時々 {"summary": "<JSON文字列>"} の形でネストして返すケースをアンラップする。"""
        text = text.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:]
            text = text.strip()
        try:
            result = json.loads(text)
        except json.JSONDecodeError:
            start = text.find("{"); end = text.rfind("}")
            if start >= 0 and end > start:
                try:
                    result = json.loads(text[start:end + 1])
                except Exception:
                    return {"transcript": "", "summary": text[:500], "features": []}
            else:
                return {"transcript": "", "summary": text[:500], "features": []}

        # nested JSON unwrap: summary が "{...}" 形式なら 1 段だけ unwrap
        if isinstance(result, dict):
            s = result.get("summary", "")
            if isinstance(s, str) and s.lstrip().startswith("{"):
                try:
                    inner = json.loads(s)
                    if isinstance(inner, dict) and any(
                        k in inner for k in ("summary", "transcript", "features")
                    ):
                        return inner
                except Exception:
                    pass
        return result
