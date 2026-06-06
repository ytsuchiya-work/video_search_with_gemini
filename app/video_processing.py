"""シーン分割 (PySceneDetect) と音声抽出 (ffmpeg) ヘルパ.

Databricks Apps の slim runtime には ffmpeg/ffprobe が無いため、
- duration / フレーム抽出: OpenCV (cv2) を使用
- mp4 / wav の切り出し: imageio-ffmpeg にバンドルされた ffmpeg バイナリを使用
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from typing import List

import cv2
import imageio_ffmpeg
from scenedetect import detect, ContentDetector

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()


@dataclass
class Scene:
    index: int
    start_sec: float
    end_sec: float
    scene_path: str
    audio_path: str


def get_video_duration(path: str) -> float:
    cap = cv2.VideoCapture(path)
    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
        nframes = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0
        if fps <= 0 or nframes <= 0:
            return 0.0
        return float(nframes) / float(fps)
    finally:
        cap.release()


def detect_scenes(
    video_path: str,
    threshold: float = 22.0,
    min_scene_len_sec: float = 2.5,
    max_scene_len_sec: float = 25.0,
) -> List[tuple]:
    """シーン境界を検出。

    - threshold が小さいほど敏感に分割される (デフォルト 22; 元値 30 から下げ)
    - min_scene_len_sec: 短すぎる分割を抑制 (デフォルト 2.5 秒)
    - max_scene_len_sec: ContentDetector が拾えない長尺シーン (アニメ/screencast) を
      固定時間でサブ分割するための上限 (デフォルト 25 秒)
    """
    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    cap.release()
    min_frames = max(1, int(min_scene_len_sec * fps))
    detector = ContentDetector(threshold=threshold, min_scene_len=min_frames)
    raw = detect(video_path, detector)
    if raw:
        boundaries = [(s[0].get_seconds(), s[1].get_seconds()) for s in raw]
    else:
        # ContentDetector が境界を検出できない動画 (アニメ/screencast/単色など)
        # の場合は動画全体を 1 シーンとして渡し、後段で max_scene_len_sec 等分割する
        duration = get_video_duration(video_path)
        if duration <= 0:
            return []
        boundaries = [(0.0, duration)]

    # 長すぎるシーンを max_scene_len_sec を上限に等分割
    result: List[tuple] = []
    for start, end in boundaries:
        length = end - start
        if length <= max_scene_len_sec:
            result.append((start, end))
            continue
        n_chunks = int((length + max_scene_len_sec - 1) // max_scene_len_sec)
        chunk = length / n_chunks
        for k in range(n_chunks):
            cs = start + k * chunk
            ce = start + (k + 1) * chunk if k < n_chunks - 1 else end
            result.append((cs, ce))
    return result


def split_video(
    video_path: str,
    scenes: List[tuple],
    out_scene_dir: str,
    out_audio_dir: str,
    video_id: str,
) -> List[Scene]:
    """各シーンを mp4 と wav に分割。ffmpeg は imageio_ffmpeg のバイナリを使用."""
    os.makedirs(out_scene_dir, exist_ok=True)
    os.makedirs(out_audio_dir, exist_ok=True)

    result: List[Scene] = []
    for i, (start, end) in enumerate(scenes):
        duration = max(end - start, 0.05)
        scene_path = os.path.join(out_scene_dir, f"{video_id}_scene_{i:04d}.mp4")
        audio_path = os.path.join(out_audio_dir, f"{video_id}_scene_{i:04d}.wav")

        subprocess.run(
            [
                FFMPEG, "-y", "-ss", f"{start:.3f}", "-i", video_path,
                "-t", f"{duration:.3f}", "-c:v", "libx264", "-preset", "veryfast",
                "-c:a", "aac", "-movflags", "+faststart", scene_path,
            ],
            check=True, capture_output=True,
        )
        # 音声トラックが無い動画でも失敗しないよう -an フォールバック
        try:
            subprocess.run(
                [
                    FFMPEG, "-y", "-ss", f"{start:.3f}", "-i", video_path,
                    "-t", f"{duration:.3f}", "-vn", "-ac", "1", "-ar", "16000",
                    "-c:a", "pcm_s16le", audio_path,
                ],
                check=True, capture_output=True,
            )
        except subprocess.CalledProcessError:
            # 空の wav を作成 (1 秒の無音)
            subprocess.run(
                [
                    FFMPEG, "-y", "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono",
                    "-t", "1.0", "-c:a", "pcm_s16le", audio_path,
                ],
                check=True, capture_output=True,
            )
        result.append(Scene(i, start, end, scene_path, audio_path))
    return result


def extract_frames(scene_path: str, num_frames: int = 4) -> List[bytes]:
    """シーンから等間隔にフレームを抽出 (JPEG bytes). OpenCV のみ使用."""
    cap = cv2.VideoCapture(scene_path)
    try:
        n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if n <= 0:
            return []
        targets = [int(n * (i + 0.5) / num_frames) for i in range(num_frames)]
        frames: List[bytes] = []
        for t in targets:
            cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, min(t, n - 1)))
            ok, frame = cap.read()
            if not ok or frame is None:
                continue
            ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
            if ok:
                frames.append(buf.tobytes())
        return frames
    finally:
        cap.release()
