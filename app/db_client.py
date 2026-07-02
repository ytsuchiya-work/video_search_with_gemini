"""Databricks SQL warehouse / Vector Search クライアント."""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Iterable, List

import requests
from databricks.sdk import WorkspaceClient
from databricks import sql as dbsql

logger = logging.getLogger(__name__)

CATALOG = os.environ.get("CATALOG", "classic_stable_ytcy_catalog")
SCHEMA = os.environ.get("SCHEMA", "mulitmodal_video_search_with_gemini")
VOLUME = os.environ.get("VOLUME", "media")
WAREHOUSE_ID = os.environ.get("DATABRICKS_WAREHOUSE_ID", "e351c2d1b16eae95")
VS_ENDPOINT = os.environ.get("VS_ENDPOINT_NAME", "video-search-endpoint")
VS_INDEX = os.environ.get(
    "VS_INDEX_NAME", f"{CATALOG}.{SCHEMA}.scene_analysis_index"
)
VS_EMBEDDING_MODEL = os.environ.get("EMBEDDING_ENDPOINT", "databricks-gte-large-en")

T_VIDEOS = f"{CATALOG}.{SCHEMA}.videos"
T_SCENES = f"{CATALOG}.{SCHEMA}.scenes"
T_ANALYSIS = f"{CATALOG}.{SCHEMA}.scene_analysis"
VOLUME_ROOT = f"/Volumes/{CATALOG}/{SCHEMA}/{VOLUME}"


class DBClient:
    def __init__(self):
        self.w = WorkspaceClient()
        self.host = self.w.config.host.rstrip("/")

    def _conn(self):
        token = self.w.config.authenticate()["Authorization"].split(" ", 1)[1]
        return dbsql.connect(
            server_hostname=self.host.replace("https://", ""),
            http_path=f"/sql/1.0/warehouses/{WAREHOUSE_ID}",
            access_token=token,
        )

    def exec(self, statement: str, params=None) -> None:
        with self._conn() as c, c.cursor() as cur:
            if params is None:
                cur.execute(statement)
            else:
                cur.execute(statement, parameters=params)

    def query(self, statement: str, params=None) -> list[dict]:
        with self._conn() as c, c.cursor() as cur:
            if params is None:
                cur.execute(statement)
            else:
                cur.execute(statement, parameters=params)
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in rows]

    # ── Volume file IO via SDK files API ──
    def upload_to_volume(self, local_path: str, volume_path: str) -> None:
        """ローカルファイルを UC Volume にアップロード (SDK 経由)."""
        with open(local_path, "rb") as f:
            self.w.files.upload(file_path=volume_path, contents=f, overwrite=True)

    def download_from_volume(self, volume_path: str, local_path: str) -> None:
        resp = self.w.files.download(file_path=volume_path)
        with open(local_path, "wb") as out:
            stream = resp.contents
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)

    def _auth(self) -> dict:
        return self.w.config.authenticate()

    # ── Vector Search ──
    def trigger_sync(self) -> dict:
        url = f"{self.host}/api/2.0/vector-search/indexes/{VS_INDEX}/sync"
        h = self._auth(); h["Content-Type"] = "application/json"
        r = requests.post(url, headers=h, timeout=30)
        return {"status": r.status_code, "body": r.text[:400]}

    def index_status(self) -> dict:
        url = f"{self.host}/api/2.0/vector-search/indexes/{VS_INDEX}"
        r = requests.get(url, headers=self._auth(), timeout=30)
        return r.json()

    def recreate_index(self) -> dict:
        """インデックスを削除して同一スペックで再作成する.

        Delta sync インデックスは長期間同期しないとソーステーブルの変更履歴が
        delta.deletedFileRetentionDuration を超えて消え、ONLINE_PIPELINE_FAILED
        から復旧できなくなる。その場合の公式な修復手段が再作成
        (初回同期でソーステーブル全件を取り込み直す)。
        """
        del_url = f"{self.host}/api/2.0/vector-search/indexes/{VS_INDEX}"
        r = requests.delete(del_url, headers=self._auth(), timeout=30)
        logger.info("index delete: status=%s body=%s", r.status_code, r.text[:200])

        body = {
            "name": VS_INDEX,
            "endpoint_name": VS_ENDPOINT,
            "primary_key": "scene_id",
            "index_type": "DELTA_SYNC",
            "delta_sync_index_spec": {
                "source_table": T_ANALYSIS,
                "pipeline_type": "TRIGGERED",
                "embedding_source_columns": [
                    {
                        "name": "embedding_text",
                        "embedding_model_endpoint_name": VS_EMBEDDING_MODEL,
                    }
                ],
            },
        }
        h = self._auth(); h["Content-Type"] = "application/json"
        create_url = f"{self.host}/api/2.0/vector-search/indexes"
        last = None
        # 削除が非同期に完了するため、名前衝突が解けるまでリトライ
        for _ in range(6):
            last = requests.post(create_url, headers=h, json=body, timeout=30)
            if last.ok:
                logger.info("index recreated: %s", VS_INDEX)
                return {"action": "recreated", "status": last.status_code}
            time.sleep(5)
        raise RuntimeError(
            f"index recreate failed: {last.status_code} {last.text[:300]}"
        )

    def sync_index(self) -> dict:
        """同期をトリガーする。インデックスが失敗状態/不存在なら再作成にフォールバック."""
        try:
            info = self.index_status()
        except Exception:
            info = {}
        status = info.get("status")
        state = (status or {}).get("detailed_state", "")
        if status is None or "FAILED" in state:
            logger.warning("index state=%r -> recreating", state or info.get("error_code"))
            return self.recreate_index()
        sync = self.trigger_sync()
        sync["action"] = "sync_triggered" if sync["status"] == 200 else "sync_failed"
        return sync

    def search(self, query_text: str, num_results: int = 10) -> list[dict]:
        url = f"{self.host}/api/2.0/vector-search/indexes/{VS_INDEX}/query"
        h = self._auth(); h["Content-Type"] = "application/json"
        body = {
            "query_text": query_text,
            "columns": [
                "scene_id", "video_id", "scene_index", "start_sec", "end_sec",
                "scene_path", "summary", "transcript", "features",
            ],
            "num_results": num_results,
        }
        r = requests.post(url, headers=h, json=body, timeout=30)
        r.raise_for_status()
        data = r.json()
        result_cols = [c["name"] for c in data["manifest"]["columns"]]
        rows = data.get("result", {}).get("data_array", []) or []
        out = []
        for r_ in rows:
            d = dict(zip(result_cols, r_))
            out.append(d)
        return out
