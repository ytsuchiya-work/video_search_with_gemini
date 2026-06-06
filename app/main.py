"""FastAPI backend for Video Search with Gemini.

3 つの責務:
  1. /api/upload          動画をアップロード → UC Volume に保存 → videos テーブル登録
  2. /api/process/{vid}   PySceneDetect でシーン分割 + ffmpeg 音声抽出
  3. /api/analyze/{vid}   Gemini で transcript/summary/features を生成 → scene_analysis 登録 → Sync
  4. /api/search          Vector Search で類似シーン検索
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from db_client import (
    CATALOG, SCHEMA, VOLUME, VOLUME_ROOT, T_VIDEOS, T_SCENES, T_ANALYSIS,
    VS_INDEX, VS_ENDPOINT, DBClient,
)
from gemini_client import GeminiClient, GEMINI_ENDPOINT, EMBEDDING_ENDPOINT
from video_processing import (
    detect_scenes, split_video, extract_frames, get_video_duration,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("vsg")

app = FastAPI(title="Video Search with Gemini")

db = DBClient()
gemini = GeminiClient()

# 進捗を共有するためのインメモリストア (Databricks Apps は単一インスタンスなので OK)
JOBS: dict[str, dict] = {}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _job(job_id: str, **kwargs) -> dict:
    j = JOBS.setdefault(job_id, {"events": [], "status": "running"})
    for k, v in kwargs.items():
        j[k] = v
    return j


def _log_event(job_id: str, message: str, level: str = "info") -> None:
    e = {"t": _now(), "level": level, "message": message}
    JOBS.setdefault(job_id, {"events": [], "status": "running"})["events"].append(e)
    logger.log(getattr(logging, level.upper(), logging.INFO), "[%s] %s", job_id, message)


# ── 1) Upload ────────────────────────────────────────────────────────────────

class UploadResult(BaseModel):
    video_id: str
    filename: str
    video_path: str
    duration: float


@app.post("/api/upload", response_model=UploadResult)
async def upload_video(file: UploadFile = File(...)):
    if not file.filename:
        raise HTTPException(400, "filename is empty")
    suffix = Path(file.filename).suffix.lower() or ".mp4"
    if suffix not in {".mp4", ".mov", ".mkv", ".webm", ".avi"}:
        raise HTTPException(400, f"unsupported extension: {suffix}")

    video_id = uuid.uuid4().hex[:12]
    safe_name = f"{video_id}{suffix}"
    volume_path = f"{VOLUME_ROOT}/uploads/{safe_name}"

    # 一時ファイル → Volume にアップロード
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        local = tmp.name
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            tmp.write(chunk)

    try:
        duration = get_video_duration(local)
        db.upload_to_volume(local, volume_path)
        db.exec(
            f"""INSERT INTO {T_VIDEOS}
                  (video_id, filename, video_path, duration, num_scenes, status, uploaded_at)
                VALUES (?, ?, ?, ?, NULL, 'uploaded', current_timestamp())""",
            params=(video_id, file.filename, volume_path, float(duration)),
        )
    finally:
        os.unlink(local)

    logger.info("uploaded video_id=%s path=%s", video_id, volume_path)
    return UploadResult(
        video_id=video_id,
        filename=file.filename,
        video_path=volume_path,
        duration=duration,
    )


# ── 2) Scene split + audio extract ──────────────────────────────────────────

@app.post("/api/process/{video_id}")
async def process_video(video_id: str):
    """同期実行: PySceneDetect で分割 → ffmpeg で各シーンを切り出し → 音声抽出 → Volume 保存."""
    job_id = f"proc-{video_id}"
    JOBS[job_id] = {"events": [], "status": "running", "video_id": video_id}

    rows = db.query(
        f"SELECT video_path, status FROM {T_VIDEOS} WHERE video_id = ?",
        params=(video_id,),
    )
    if not rows:
        raise HTTPException(404, f"video_id not found: {video_id}")
    src_path = rows[0]["video_path"]

    try:
        # 再処理に備えて既存シーン/解析を削除
        db.exec(f"DELETE FROM {T_ANALYSIS} WHERE video_id = ?", params=(video_id,))
        db.exec(f"DELETE FROM {T_SCENES} WHERE video_id = ?", params=(video_id,))

        with tempfile.TemporaryDirectory() as td:
            _log_event(job_id, f"download from Volume: {src_path}")
            local_src = os.path.join(td, "src.mp4")
            db_download_from_volume(src_path, local_src)

            _log_event(job_id, "detect scenes (PySceneDetect threshold=22, min=2.5s, max=25s)")
            scenes = detect_scenes(
                local_src,
                threshold=22.0,
                min_scene_len_sec=2.5,
                max_scene_len_sec=25.0,
            )
            if not scenes:
                duration = get_video_duration(local_src)
                scenes = [(0.0, duration)]
                _log_event(job_id, "no scenes detected, falling back to single scene")
            _log_event(job_id, f"detected {len(scenes)} scenes")

            scene_local_dir = os.path.join(td, "scenes")
            audio_local_dir = os.path.join(td, "audio")
            split = split_video(local_src, scenes, scene_local_dir, audio_local_dir, video_id)

            # アップロード & 行追加
            for s in split:
                scene_volume = f"{VOLUME_ROOT}/scenes/{os.path.basename(s.scene_path)}"
                audio_volume = f"{VOLUME_ROOT}/audio/{os.path.basename(s.audio_path)}"
                db.upload_to_volume(s.scene_path, scene_volume)
                db.upload_to_volume(s.audio_path, audio_volume)

                scene_id = f"{video_id}_{s.index:04d}"
                db.exec(
                    f"DELETE FROM {T_SCENES} WHERE scene_id = ?",
                    params=(scene_id,),
                )
                db.exec(
                    f"""INSERT INTO {T_SCENES}
                          (scene_id, video_id, scene_index, start_sec, end_sec,
                           scene_path, audio_path, status, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, 'split', current_timestamp())""",
                    params=(
                        scene_id, video_id, s.index, s.start_sec, s.end_sec,
                        scene_volume, audio_volume,
                    ),
                )
                _log_event(job_id, f"saved scene {s.index}  ({s.start_sec:.1f}-{s.end_sec:.1f}s)")

            db.exec(
                f"UPDATE {T_VIDEOS} SET num_scenes = ?, status = 'split' WHERE video_id = ?",
                params=(len(split), video_id),
            )

        JOBS[job_id]["status"] = "completed"
        JOBS[job_id]["num_scenes"] = len(split)
        return {"job_id": job_id, "num_scenes": len(split), "status": "completed"}

    except Exception as e:
        JOBS[job_id]["status"] = "failed"
        JOBS[job_id]["error"] = str(e)
        _log_event(job_id, f"ERROR: {e}\n{traceback.format_exc()}", level="error")
        raise HTTPException(500, str(e))


def db_download_from_volume(volume_path: str, local_path: str) -> None:
    db.download_from_volume(volume_path, local_path)


# ── 3) Gemini analyze ───────────────────────────────────────────────────────

@app.post("/api/analyze/{video_id}")
async def analyze_video(video_id: str):
    job_id = f"ana-{video_id}"
    JOBS[job_id] = {"events": [], "status": "running", "video_id": video_id}

    # 未解析のシーンを対象
    scenes = db.query(
        f"""SELECT s.scene_id, s.video_id, s.scene_index, s.start_sec, s.end_sec,
                   s.scene_path, s.audio_path
            FROM {T_SCENES} s
            LEFT JOIN {T_ANALYSIS} a USING (scene_id)
            WHERE s.video_id = ? AND a.scene_id IS NULL
            ORDER BY s.scene_index""",
        params=(video_id,),
    )
    if not scenes:
        JOBS[job_id]["status"] = "completed"
        return {"job_id": job_id, "analyzed": 0, "status": "completed",
                "message": "全シーン解析済み"}

    _log_event(job_id, f"analyzing {len(scenes)} scenes via {GEMINI_ENDPOINT}")
    analyzed = 0

    try:
        for sc in scenes:
            scene_id = sc["scene_id"]
            with tempfile.TemporaryDirectory() as td:
                local_scene = os.path.join(td, "scene.mp4")
                local_audio = os.path.join(td, "scene.wav")
                db_download_from_volume(sc["scene_path"], local_scene)
                db_download_from_volume(sc["audio_path"], local_audio)

                frames = extract_frames(local_scene, num_frames=4)
                with open(local_audio, "rb") as f:
                    audio_bytes = f.read()

                # 音声サイズが大きすぎる場合(>4MB)は省略
                audio = audio_bytes if len(audio_bytes) < 4 * 1024 * 1024 else None
                result = gemini.analyze_scene(frames, audio_wav=audio)

            transcript = result.get("transcript", "") or ""
            summary = result.get("summary", "") or ""
            features = result.get("features", []) or []
            feat_str = ", ".join(features) if isinstance(features, list) else str(features)

            embedding_text = (
                f"要約: {summary}\n"
                f"特徴: {feat_str}\n"
                f"音声: {transcript}"
            )[:8000]

            db.exec(
                f"DELETE FROM {T_ANALYSIS} WHERE scene_id = ?",
                params=(scene_id,),
            )
            db.exec(
                f"""INSERT INTO {T_ANALYSIS}
                      (scene_id, video_id, scene_index, start_sec, end_sec,
                       scene_path, audio_path, transcript, summary, features,
                       embedding_text, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, current_timestamp())""",
                params=(
                    scene_id, sc["video_id"], sc["scene_index"],
                    sc["start_sec"], sc["end_sec"],
                    sc["scene_path"], sc["audio_path"],
                    transcript, summary, feat_str, embedding_text,
                ),
            )
            analyzed += 1
            _log_event(job_id, f"  scene {sc['scene_index']}: {summary[:80]}")

        _log_event(job_id, "triggering Vector Search sync...")
        sync = db.trigger_sync()
        _log_event(job_id, f"sync triggered: {sync}")

        JOBS[job_id]["status"] = "completed"
        JOBS[job_id]["analyzed"] = analyzed
        return {"job_id": job_id, "analyzed": analyzed, "status": "completed"}

    except Exception as e:
        JOBS[job_id]["status"] = "failed"
        JOBS[job_id]["error"] = str(e)
        _log_event(job_id, f"ERROR: {e}\n{traceback.format_exc()}", level="error")
        raise HTTPException(500, str(e))


# ── 4) Search ────────────────────────────────────────────────────────────────

class SearchRequest(BaseModel):
    query: str
    num_results: int = 10


@app.post("/api/search")
async def search(req: SearchRequest):
    if not req.query.strip():
        raise HTTPException(400, "empty query")
    try:
        results = db.search(req.query, num_results=req.num_results)
    except Exception as e:
        logger.exception("search failed")
        raise HTTPException(500, str(e))
    return {"query": req.query, "results": results}


# ── 補助 API ─────────────────────────────────────────────────────────────────

@app.get("/api/videos")
async def list_videos():
    rows = db.query(
        f"""SELECT v.video_id, v.filename, v.duration, v.num_scenes, v.status, v.uploaded_at,
                   (SELECT COUNT(*) FROM {T_ANALYSIS} a WHERE a.video_id = v.video_id) AS analyzed_scenes
            FROM {T_VIDEOS} v ORDER BY v.uploaded_at DESC"""
    )
    for r in rows:
        if r.get("uploaded_at") is not None:
            r["uploaded_at"] = str(r["uploaded_at"])
    return {"videos": rows}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    if job_id not in JOBS:
        raise HTTPException(404, f"job not found: {job_id}")
    return JOBS[job_id]


@app.post("/api/sync")
async def manual_sync():
    return db.trigger_sync()


@app.get("/api/index/status")
async def index_status():
    s = db.index_status()
    return {
        "name": s.get("name"),
        "ready": s.get("status", {}).get("ready"),
        "detailed_state": s.get("status", {}).get("detailed_state"),
        "indexed_row_count": s.get("status", {}).get("indexed_row_count"),
        "message": s.get("status", {}).get("message"),
    }


@app.get("/api/scene/{scene_id}/video")
async def stream_scene(scene_id: str):
    rows = db.query(
        f"SELECT scene_path FROM {T_SCENES} WHERE scene_id = ?",
        params=(scene_id,),
    )
    if not rows:
        raise HTTPException(404, scene_id)
    return StreamingResponse(
        _stream_volume(rows[0]["scene_path"]),
        media_type="video/mp4",
    )


def _stream_volume(volume_path: str):
    resp = db.w.files.download(file_path=volume_path)
    stream = resp.contents
    while True:
        chunk = stream.read(64 * 1024)
        if not chunk:
            break
        yield chunk


@app.get("/api/config")
async def get_config():
    return {
        "catalog": CATALOG,
        "schema": SCHEMA,
        "volume": VOLUME,
        "vs_endpoint": VS_ENDPOINT,
        "vs_index": VS_INDEX,
        "gemini_endpoint": GEMINI_ENDPOINT,
        "embedding_endpoint": EMBEDDING_ENDPOINT,
    }


# ── Static frontend ──────────────────────────────────────────────────────────

static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(static_dir):
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
