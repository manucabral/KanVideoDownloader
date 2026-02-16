"""
KanDownloader – CLI tool to download episodes from kan.org.il.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from typing import Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from curl_cffi import requests

LOG_FILE = "KanVideoDownloader.log"
LOG_FORMAT = "%(asctime)s  %(levelname)-8s  %(message)s"
DEFAULT_OUTPUT_DIR = "episodes"
PROGRESS_BAR_WIDTH = 30
HTTP_TIMEOUT = 30

M3U8_PATTERNS: list[str] = [
    r'"contentUrl"\s*:\s*"([^"]+\.m3u8[^"]*)"',
    r'https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*',
]

TRAILER_RE = re.compile(r"טריילר|trailer", re.IGNORECASE)
PROGRAM_ID_RE = re.compile(r"/(p-\d+)/")
UNSAFE_CHARS_RE = re.compile(r'[\\/*?:"<>|]')

PROJECT_FFMPEG = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "ffmpeg",
    "ffmpeg-master-latest-win64-gpl",
    "bin",
    "ffmpeg.exe",
)

logger = logging.getLogger("KanVideoDownloader")
logger.setLevel(logging.DEBUG)

_formatter = logging.Formatter(LOG_FORMAT)

_console = logging.StreamHandler()
_console.setFormatter(_formatter)
logger.addHandler(_console)

_logfile = logging.FileHandler(LOG_FILE, "a", "utf-8")
_logfile.setFormatter(_formatter)
logger.addHandler(_logfile)

_session = requests.Session(impersonate="chrome")


def _parse_args() -> argparse.Namespace:
    """Parse command-line arguments and return the namespace."""
    p = argparse.ArgumentParser(
        prog="KanVideoDownloader",
        description="Download episodes from Kan.",
    )
    p.add_argument("url", help="URL of the show or episode")
    p.add_argument(
        "-o",
        "--output",
        default=DEFAULT_OUTPUT_DIR,
        help=f"Output directory (default: {DEFAULT_OUTPUT_DIR})",
    )
    p.add_argument(
        "-v", "--verbose", action="store_true", help="Enable debug-level console output"
    )
    p.add_argument(
        "-fp", "--ffmpeg-path", default=None, help="Explicit path to the ffmpeg binary"
    )
    return p.parse_args()


def _resolve_ffmpeg(explicit: Optional[str]) -> str:
    """Determine the ffmpeg binary to use."""
    if explicit:
        logger.info("Using explicit ffmpeg path: %s", explicit)
        if not os.path.isfile(explicit):
            logger.error("Path does not exist: %s", explicit)
            sys.exit(1)
        return explicit

    try:
        subprocess.run(
            ["ffmpeg", "-version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        logger.info("ffmpeg found in system PATH")
        return "ffmpeg"
    except FileNotFoundError:
        logger.warning("ffmpeg not in system PATH")

    if os.path.isfile(PROJECT_FFMPEG):
        logger.info("Using bundled ffmpeg: %s", PROJECT_FFMPEG)
        return PROJECT_FFMPEG

    logger.error(
        "ffmpeg binary not found. Please install ffmpeg or provide its path with --ffmpeg-path."
    )
    sys.exit(1)


def _resolve_ffprobe(ffmpeg_bin: str) -> Optional[str]:
    """Derive ffprobe path from the ffmpeg binary."""
    if ffmpeg_bin == "ffmpeg":
        return "ffprobe" if shutil.which("ffprobe") else None
    d = os.path.dirname(ffmpeg_bin)
    name = "ffprobe.exe" if sys.platform == "win32" else "ffprobe"
    path = os.path.join(d, name)
    return path if os.path.isfile(path) else None


def _fetch(url: str, timeout: int = HTTP_TIMEOUT) -> str:
    """Fetch the page content."""
    logger.info("GET %s", url)
    r = _session.get(url, timeout=timeout)
    r.raise_for_status()
    logger.debug("Res headers: %s (%dbytes)", r.headers, len(r.content))
    return r.text


def _sanitize(name: str) -> str:
    """Make a string safe for use as a filename."""
    return UNSAFE_CHARS_RE.sub("_", name).strip()


def _extract_m3u8(html: str) -> Optional[str]:
    """Extract the m3u8 URL from the page HTML using regex patterns."""
    logger.info("Searching for m3u8 URL...")
    for pat in M3U8_PATTERNS:
        m = re.search(pat, html)
        if m:
            url = m.group(1) if m.lastindex else m.group(0)
            logger.info("Found m3u8: %s", url)
            return url
    logger.warning("m3u8 URL not found")
    return None


def _extract_episode_links(html: str, page_url: str) -> list[dict]:
    """Extract episode links and titles from a show page."""
    logger.info("Looking for episodes...")
    soup = BeautifulSoup(html, "html.parser")
    base = f"{urlparse(page_url).scheme}://{urlparse(page_url).netloc}"
    pid_match = PROGRAM_ID_RE.search(page_url)
    pid = pid_match.group(1) if pid_match else None
    seen: set[str] = set()
    episodes: list[dict] = []
    for a in soup.select("a.card-link[href]"):
        t = a.select_one(".card-title")
        if not t:
            continue
        title = t.get_text(strip=True)
        href = urljoin(base, a["href"])

        if pid and pid not in href:
            logger.debug("Skiping unrelated link: %s", href)
            continue
        if TRAILER_RE.search(title):
            logger.debug("Skiping trailer: %s", title)
            continue
        if href in seen:
            logger.debug("Skiping duplicate link: %s", href)
            continue

        seen.add(href)
        episodes.append({"title": title, "url": href})

    logger.info("Found %d episode/s for %s", len(episodes), pid)
    return episodes


def _m3u8_duration(m3u8_url: str) -> Optional[float]:
    """Parse the m3u8 manifest to estimate total duration."""
    try:
        text = _session.get(m3u8_url, timeout=15).text
    except Exception as exc:
        logger.debug("m3u8 fetch failed: %s", exc)
        return None
    if "#EXT-X-STREAM-INF" in text:
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                try:
                    text = _session.get(urljoin(m3u8_url, line), timeout=15).text
                except Exception as exc:
                    logger.debug("Variant fetch failed: %s", exc)
                    return None
                break
    total = sum(float(m.group(1)) for m in re.finditer(r"#EXTINF:\s*([\d.]+)", text))
    if total > 0:
        logger.debug("m3u8 duration: %.2f seconds", total)
        return total
    return None


def _get_duration(ffmpeg_bin: str, m3u8_url: str) -> Optional[float]:
    """Try to get the total duration of the stream via ffprobe, falling back to m3u8 parsing."""
    probe = _resolve_ffprobe(ffmpeg_bin)
    if probe:
        try:
            r = subprocess.run(
                [
                    probe,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    m3u8_url,
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            d = float(r.stdout.strip())
            if d > 0:
                logger.debug("Duration (ffprobe): %.2f seconds", d)
                return d
        except Exception as exc:
            logger.debug("ffprobe failed: %s", exc)

    _duration = _m3u8_duration(m3u8_url)
    if _duration:
        return _duration
    logger.debug("Duration unknown, progress bar will show elapsed time")
    return None


def _fmt_time(seconds: float) -> str:
    """Format seconds as MM:SS or HH:MM:SS."""
    seconds = int(seconds)
    if seconds >= 3600:
        h, seconds = divmod(seconds, 3600)
        m, seconds = divmod(seconds, 60)
        return f"{h}:{m:02d}:{seconds:02d}"
    m, seconds = divmod(seconds, 60)
    return f"{m:02d}:{seconds:02d}"


def _bar(
    p_of_prog: float,
    elapsed: float,
    total_duration: Optional[float],
    w: int = PROGRESS_BAR_WIDTH,
) -> str:
    filled = int(w * p_of_prog / 100)
    bar = f"[{'█' * filled}{'░' * (w - filled)}] {p_of_prog:5.1f}%"
    elapsed_str = _fmt_time(elapsed)
    if total_duration and total_duration > 0 and p_of_prog > 0:
        total_str = _fmt_time(total_duration)
        remaining = max((total_duration - elapsed), 0)
        eta_str = _fmt_time(remaining)
        return f"{bar}  {elapsed_str} / {total_str}  ETA {eta_str}"
    return f"{bar}  {elapsed_str}"


def _download(ffmpeg_bin: str, m3u8_url: str, dest: str) -> None:
    """Download the stream using ffmpeg"""
    logger.info("Downloading to %s", dest)
    duration = _get_duration(ffmpeg_bin, m3u8_url)
    cmd = [
        ffmpeg_bin,
        "-y",
        "-loglevel",
        "error",
        "-progress",
        "pipe:1",
        "-i",
        m3u8_url,
        "-c",
        "copy",
        "-bsf:a",
        "aac_adtstoasc",
        dest,
    ]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    last_t = 0.0
    start = time.monotonic()
    try:
        for line in proc.stdout:  # type: ignore[union-attr]
            if not line.startswith("out_time_us="):
                continue
            try:
                elapsed = int(line.split("=", 1)[1]) / 1_000_000
            except ValueError:
                continue

            now = time.monotonic()
            wall = now - start
            if duration and duration > 0:
                p_of_prog = min(elapsed / duration * 100, 100.0)
                if now - last_t >= 0.25 or p_of_prog >= 100:
                    print(
                        f"\r  {_bar(p_of_prog, elapsed, duration)}", end="", flush=True
                    )
                    last_t = now
            elif now - last_t >= 1.0:
                m, s = divmod(int(wall), 60)
                print(f"\rDownloading… {m:02d}:{s:02d} elapsed", end="", flush=True)
                last_t = now
        proc.wait()
        if proc.returncode != 0:
            err = proc.stderr.read() if proc.stderr else ""
            print()
            logger.error("ffmpeg: %s", err.strip())
            raise subprocess.CalledProcessError(proc.returncode, cmd)
        wall = time.monotonic() - start
        if duration:
            print(f"\r{_bar(100.0, duration, duration)}")
        else:
            m, s = divmod(int(wall), 60)
            print(f"\rDone in {m:02d}:{s:02d}")
    except Exception:
        proc.kill()
        proc.wait()
        raise

    logger.info("Saved %s", dest)


def _parse_selection(raw: str, total: int) -> list[int]:
    """Parse a user selection string into a list of episode indices."""
    raw = raw.strip().lower()
    if raw in ("all", "a", "*", ""):
        return list(range(total))
    idx: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if "-" in part:
            try:
                lo, hi = part.split("-", 1)
                for i in range(int(lo), int(hi) + 1):
                    if 1 <= i <= total:
                        idx.add(i - 1)
            except ValueError:
                continue
        else:
            try:
                n = int(part)
                if 1 <= n <= total:
                    idx.add(n - 1)
            except ValueError:
                continue
    return sorted(idx)


def _choose_episodes(episodes: list[dict]) -> list[dict]:
    """Display a list of episodes and prompt the user to select which ones to download."""
    separator = "-" * 40
    print(f"{separator}\nEpisodes found:\n{separator}")
    for i, ep in enumerate(episodes, 1):
        print(f"{i:>3}.   {ep['title']}")
    print(f"{separator}\nEnter episode/s to download:")
    print(f"Examples:  all | 1 | 1,3,5 | 2-8 | 1,4-6,10\n{separator}")

    while True:
        try:
            choice = input("\n>> Selection: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            logger.info("Cancelled.")
            sys.exit(0)

        sel = _parse_selection(choice, len(episodes))
        if sel:
            nums = ", ".join(str(i + 1) for i in sel)
            print(f"{len(sel)} episode/s selected: {nums}\n")
            return [episodes[i] for i in sel]
        print("Invalid selection, try again.")


def _process_episode(
    url: str, ffmpeg_bin: str, out_dir: str, title: Optional[str] = None
) -> None:
    """Fetch a single episode page, find its m3u8, and download it."""
    html = _fetch(url)
    m3u8 = _extract_m3u8(html)
    if not m3u8:
        logger.error("Skipping (no m3u8): %s", url)
        return
    if not title:
        soup = BeautifulSoup(html, "html.parser")
        og = soup.find("meta", property="og:title")
        title = og["content"] if og else urlparse(url).path.rstrip("/").split("/")[-1]
    dest = os.path.join(out_dir, f"{_sanitize(title)}.mp4")
    try:
        _download(ffmpeg_bin, m3u8, dest)
    except subprocess.CalledProcessError as exc:
        logger.error("ffmpeg failed for %s: %s", title, exc)


def main() -> None:
    args = _parse_args()
    _console.setLevel(logging.DEBUG if args.verbose else logging.INFO)

    ffmpeg = _resolve_ffmpeg(args.ffmpeg_path)
    out_dir: str = args.output
    os.makedirs(out_dir, exist_ok=True)

    html = _fetch(args.url)
    episodes = _extract_episode_links(html, args.url)

    if episodes:
        chosen = _choose_episodes(episodes)
        logger.info("Downloading %d episode/s", len(chosen))
        for i, ep in enumerate(chosen, 1):
            logger.info("[%d/%d] %s", i, len(chosen), ep["title"])
            _process_episode(ep["url"], ffmpeg, out_dir, title=ep["title"])
    else:
        logger.info("Single episode, downloading...")
        _process_episode(args.url, ffmpeg, out_dir)
    logger.info("Done.")


if __name__ == "__main__":
    main()
