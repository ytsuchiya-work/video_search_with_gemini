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

    # ── Volume file IO via /api/2.0/fs/files ──
    def upload_to_volume(self, local_path: str, volume_path: str) -> None:
        """ローカルファイルを UC Volume にアップロード."""
        url = f"{self.host}/api/2.0/fs/files{volume_path}?overwrite=true"
        with open(local_path, "rb") as f:
            r = requests.put(url, headers=self._auth(), data=f, timeout=600)
        r.raise_for_status()

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
