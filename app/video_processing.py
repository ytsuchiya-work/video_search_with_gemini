"""シーン分割 (PySceneDetect) と音声抽出 (ffmpeg) ヘルパ."""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from typing import List

from scenedetect import detect, ContentDetector


@dataclass
class Scene:
    index: int
    start_sec: float
    end_sec: float
    scene_path: str
    audio_path: str


def detect_scenes(video_path: str, threshold: float = 30.0, min_scene_len_sec: float = 4.0) -> List[tuple]:
    """シーン境界を検出。短すぎる分割を避けるため min_scene_len_sec を 4 秒に。

    Returns list of (start_sec, end_sec) tuples.
    """
    detector = ContentDetector(threshold=threshold, min_scene_len=int(min_scene_len_sec * 30))
    scenes = detect(video_path, detector)
    if not scenes:
        return []
    return [(s[0].get_seconds(), s[1].get_seconds()) for s in scenes]


def split_video(
    video_path: str,
    scenes: List[tuple],
    out_scene_dir: str,
    out_audio_dir: str,
    video_id: str,
) -> List[Scene]:
    """各シーンを mp4 と wav に分割。ffmpeg を直接呼ぶ (高速&依存最小)."""
    os.makedirs(out_scene_dir, exist_ok=True)
    os.makedirs(out_audio_dir, exist_ok=True)

    result: List[Scene] = []
    for i, (start, end) in enumerate(scenes):
        duration = end - start
        scene_path = os.path.join(out_scene_dir, f"{video_id}_scene_{i:04d}.mp4")
        audio_path = os.path.join(out_audio_dir, f"{video_id}_scene_{i:04d}.wav")

        subprocess.run(
            [
                "ffmpeg", "-y", "-ss", f"{start:.3f}", "-i", video_path,
                "-t", f"{duration:.3f}", "-c:v", "libx264", "-preset", "veryfast",
                "-c:a", "aac", "-movflags", "+faststart", scene_path,
            ],
            check=True, capture_output=True,
        )
        subprocess.run(
            [
                "ffmpeg", "-y", "-ss", f"{start:.3f}", "-i", video_path,
                "-t", f"{duration:.3f}", "-vn", "-ac", "1", "-ar", "16000",
                "-c:a", "pcm_s16le", audio_path,
            ],
            check=True, capture_output=True,
        )
        result.append(Scene(i, start, end, scene_path, audio_path))
    return result


def extract_frames(scene_path: str, num_frames: int = 4) -> List[bytes]:
    """シーンから等間隔にフレームを抽出 (JPEG bytes)."""
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", scene_path],
        capture_output=True, text=True, check=True,
    )
    duration = float(probe.stdout.strip())
    if duration <= 0:
        return []

    frames: List[bytes] = []
    for i in range(num_frames):
        t = duration * (i + 0.5) / num_frames
        res = subprocess.run(
            ["ffmpeg", "-y", "-ss", f"{t:.3f}", "-i", scene_path,
             "-frames:v", "1", "-q:v", "5", "-f", "image2pipe", "-vcodec", "mjpeg", "-"],
            capture_output=True, check=True,
        )
        if res.stdout:
            frames.append(res.stdout)
    return frames


def get_video_duration(path: str) -> float:
    res = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True, check=True,
    )
    return float(res.stdout.strip())
