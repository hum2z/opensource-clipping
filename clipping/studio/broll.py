import html
import importlib.util
import json
import math
import os
import random
import re
import shutil
import string
import subprocess
import textwrap
import time
import urllib.parse
import urllib.request

import cv2
import mediapipe as mp
import numpy as np
import requests
from mediapipe.tasks import python as mp_python
from mediapipe.tasks.python import vision as mp_vision
from PIL import Image, ImageDraw, ImageFont
from yt_dlp import YoutubeDL

def _load_studio_internal_module(file_name: str, module_alias: str):
    module_path = os.path.join(os.path.dirname(__file__), file_name)
    spec = importlib.util.spec_from_file_location(module_alias, module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

utils = _load_studio_internal_module("utils.py", "clipping_studio_utils")
_resize_frame = utils._resize_frame
_is_vertical_ratio = utils._is_vertical_ratio

FIREFOX_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:148.0) Gecko/20100101 Firefox/148.0"


_helpers = _load_studio_internal_module("helpers.py", "clipping_studio_helpers")
_ffmpeg_utils = _load_studio_internal_module("ffmpeg_utils.py", "clipping_studio_ffmpeg_utils")
format_seconds = _helpers.format_seconds
escape_ffmpeg_filter_value = _helpers.escape_ffmpeg_filter_value
detect_video_encoder = _ffmpeg_utils.detect_video_encoder
get_ts_encode_args = _ffmpeg_utils.get_ts_encode_args
get_mp4_encode_args = _ffmpeg_utils.get_mp4_encode_args
open_ffmpeg_video_writer = _ffmpeg_utils.open_ffmpeg_video_writer
build_ffmpeg_progress_cmd = _ffmpeg_utils.build_ffmpeg_progress_cmd
run_ffmpeg_with_progress = _ffmpeg_utils.run_ffmpeg_with_progress

USED_PEXELS_IDS = set()


class _PoolSize:
    """Holds the b-roll relevance-pool size (see ``--broll-pool``)."""
    value = 5


cfg_pool = _PoolSize()


def download_pexels_broll(query, rasio, output_filename, pexels_api_key):
    """
    Search and download one Pexels B-roll video clip matching the query and aspect ratio.

    Args:
        query (str): Search query term (e.g., 'nature', 'technology').
        rasio (str): Target aspect ratio string (`9:16` for portrait or `16:9` for landscape).
        output_filename (str): Local file path where the downloaded MP4 will be saved.
        pexels_api_key (str): Valid Pexels API key for authorization.

    Returns:
        bool: True if the video was successfully downloaded and saved, False otherwise.

    Side Effects:
        Makes HTTP GET requests to the Pexels API and video CDN.
        Mutates the global `USED_PEXELS_IDS` set to prevent duplicate downloads.
        Writes a temporary file (`.part`) and renames it upon successful download.
        Prints status and error messages to stdout.

    Raises:
        None explicitly. Exceptions during download or API calls are caught and return False.
    """
    global USED_PEXELS_IDS

    if not pexels_api_key:
        print("   ⚠️ PEXELS_API_KEY tidak ditemukan. B-roll dilewati.")
        return False

    orientation = "portrait" if _is_vertical_ratio(rasio) else "landscape"

    # Deliberately no "size"/"resolution_name" filter. Both prune the result
    # set before relevance is applied — for "courthouse building exterior" they
    # cut 8000 matches down to 384 and pushed a football stadium and an
    # Istanbul mosque into the top five. Resolution is already handled properly
    # below, where the best rendition of the chosen video is selected from its
    # video_files, so filtering here only costs relevance.
    params = urllib.parse.urlencode(
        {
            "query": query,
            "orientation": orientation,
            "per_page": 30,
        }
    )
    search_url = f"https://api.pexels.com/videos/search?{params}"

    req = urllib.request.Request(
        search_url,
        headers={
            "Authorization": pexels_api_key,
            "User-Agent": "Mozilla/5.0",
        },
    )

    try:
        with urllib.request.urlopen(req) as response:
            data = json.load(response)
    except Exception as e:
        print(f"   ⚠️ Error API Pexels saat mencari '{query}': {e}")
        return False

    if not data.get("videos"):
        print(f"   ⚠️ Pexels tidak menemukan video untuk '{query}'.")
        return False

    # Pexels returns results in relevance order. Sampling uniformly across all
    # 30 throws that ranking away: for "police officer uniform closeup" the top
    # hit is a police officer, while results far down the list are only loosely
    # tagged and can be wildly off-topic. Unrelated b-roll under a specific
    # claim is worse than no b-roll, so restrict the draw to the most relevant
    # few and keep just enough randomness to avoid repeating shots across clips.
    pool_size = max(1, int(getattr(cfg_pool, "value", 5)))
    ranked = data["videos"][:pool_size]
    available_videos = [v for v in ranked if v["id"] not in USED_PEXELS_IDS]
    if not available_videos:
        # Widen to the full result set before giving up on novelty.
        available_videos = [v for v in data["videos"] if v["id"] not in USED_PEXELS_IDS]
    if not available_videos:
        print(f"   🔄 B-roll pool untuk '{query}' habis, me-reset.")
        available_videos = ranked or data["videos"]

    video_data = random.choice(available_videos)
    USED_PEXELS_IDS.add(video_data["id"])

    video_files = [
        vf
        for vf in video_data.get("video_files", [])
        if vf.get("file_type") == "video/mp4"
    ]
    if not video_files:
        print(f"   ⚠️ Tidak ada file MP4 di dalam data video '{query}'.")
        return False

    # Prefer the smallest rendition that still covers 1080p, falling back to
    # the largest available. Grabbing the biggest file wastes bandwidth on 4K
    # sources that get scaled down to the render height anyway.
    def _rendition_rank(vf):
        h = vf.get("height") or 0
        return (0, h) if h >= 1080 else (1, -h)

    video_files.sort(key=_rendition_rank)

    download_url = video_files[0]["link"]
    download_req = urllib.request.Request(
        download_url, headers={"User-Agent": "Mozilla/5.0"}
    )

    try:
        temp_path = output_filename + ".part"
        with (
            urllib.request.urlopen(download_req) as response,
            open(temp_path, "wb") as f,
        ):
            shutil.copyfileobj(response, f)
        os.replace(temp_path, output_filename)
        return True
    except Exception as e:
        print(f"   ⚠️ Error saat mengunduh B-roll '{query}': {e}")
        return False


def crop_center_broll(img, target_w, target_h):
    """
    Center-crop an image frame to the exact target aspect ratio, then resize it.

    Args:
        img (np.ndarray): Input image frame array (from OpenCV).
        target_w (int): Desired output width in pixels.
        target_h (int): Desired output height in pixels.

    Returns:
        np.ndarray: The cropped and resized frame.

    Side Effects:
        None.

    Raises:
        cv2.error: If the input image format is invalid or resizing fails.
    """
    h, w = img.shape[:2]
    target_ratio = target_w / target_h
    img_ratio = w / h

    if img_ratio > target_ratio:
        new_w = int(h * target_ratio)
        x = (w - new_w) // 2
        img = img[:, x : x + new_w]
    elif img_ratio < target_ratio:
        new_h = int(w / target_ratio)
        y = (h - new_h) // 2
        img = img[y : y + new_h, :]

    return _resize_frame(img, (target_w, target_h))


