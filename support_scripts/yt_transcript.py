#!/usr/bin/env python3
"""yt_transcript.py - pull YouTube-provided transcripts as timestamped JSON.

Fetches the caption tracks YouTube itself serves to its player for a video - the auto-generated
(speech-recognition / "ASR") track and/or a creator-uploaded ("manual") track - and writes each as
timestamped JSON. English only, for now.

When BOTH an English manual transcript and an English auto-generated transcript exist for the same
video (uncommon), it downloads both and also writes a short comparison report, so you can see how the
human-authored and machine-generated versions differ and which to trust.

How it works (no official API key, no third-party packages):
  1. GET the watch page and read the page's InnerTube API key out of the HTML.
  2. POST to YouTube's InnerTube `player` endpoint (mobile client context) to get the caption-track list.
  3. GET each caption track's timedtext URL (XML) and parse it into {text, start, duration} snippets.

This is an original, clean-room implementation written from a description of YouTube's public player
endpoints. It contains no third-party code and depends only on the Python standard library (>= 3.8).

Usage:
  python yt_transcript.py <video-id-or-URL> [--list] [--out-dir DIR] [--print]

Output defaults to <repo>/AI/support_scripts_work/transcripts/ (gitignored): one
`<video_id>.<manual|generated>.json` per track found, plus `<video_id>.comparison.md` when both exist.
Pulled transcripts are third-party copyrighted content - keep them in the gitignored output area.
"""
from __future__ import annotations

import argparse
import difflib
import http.cookiejar
import json
import re
import sys
import textwrap
import urllib.error
import urllib.parse
import urllib.request
from html import unescape
from pathlib import Path
from xml.etree import ElementTree

WATCH_URL = "https://www.youtube.com/watch?v={video_id}"
PLAYER_URL = "https://www.youtube.com/youtubei/v1/player?key={api_key}"
# The mobile client context is what YouTube hands caption tracks to without an extra token.
INNERTUBE_CONTEXT = {"client": {"clientName": "ANDROID", "clientVersion": "20.10.38"}}
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US",
}

_API_KEY_RE = re.compile(r'"INNERTUBE_API_KEY":\s*"([A-Za-z0-9_-]+)"')
_CONSENT_V_RE = re.compile(r'name="v" value="(.*?)"')
_TAG_RE = re.compile(r"<[^>]*>")
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


class TranscriptError(Exception):
    """A user-facing failure (bad input, video unavailable, disabled, IP-blocked, etc.)."""


# --------------------------------------------------------------------------- input

def extract_video_id(value: str) -> str:
    """Accept a bare 11-char video ID or any common YouTube URL form and return the ID."""
    value = value.strip()
    if _ID_RE.match(value):
        return value
    parsed = urllib.parse.urlparse(value)
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    candidate = ""
    if host == "youtu.be":
        candidate = parsed.path.lstrip("/").split("/")[0]
    elif host in ("youtube.com", "m.youtube.com", "music.youtube.com"):
        if parsed.path == "/watch":
            candidate = urllib.parse.parse_qs(parsed.query).get("v", [""])[0]
        else:
            parts = [p for p in parsed.path.split("/") if p]
            if len(parts) >= 2 and parts[0] in ("embed", "shorts", "live", "v"):
                candidate = parts[1]
    if _ID_RE.match(candidate):
        return candidate
    raise TranscriptError("Could not read an 11-character video ID from: {0!r}".format(value))


# --------------------------------------------------------------------------- http

def _build_opener():
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    return opener, jar


def _set_cookie(jar: http.cookiejar.CookieJar, name: str, value: str) -> None:
    jar.set_cookie(http.cookiejar.Cookie(
        version=0, name=name, value=value, port=None, port_specified=False,
        domain=".youtube.com", domain_specified=True, domain_initial_dot=True,
        path="/", path_specified=True, secure=False, expires=None, discard=False,
        comment=None, comment_url=None, rest={},
    ))


def _request(opener, url: str, data: bytes = None, content_type: str = None) -> str:
    headers = dict(HEADERS)
    if content_type:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST" if data is not None else "GET")
    try:
        with opener.open(req, timeout=30) as resp:
            return resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as err:
        if err.code == 429:
            raise TranscriptError(
                "YouTube is rate-limiting or blocking this IP (HTTP 429). "
                "Try again later or from a different network."
            )
        raise TranscriptError("YouTube request failed (HTTP {0}).".format(err.code))
    except urllib.error.URLError as err:
        raise TranscriptError("Network error: {0}".format(err.reason))


def _get(opener, url: str) -> str:
    return _request(opener, url)


def _post_json(opener, url: str, payload: dict) -> dict:
    body = json.dumps(payload).encode("utf-8")
    text = _request(opener, url, data=body, content_type="application/json")
    try:
        return json.loads(text)
    except json.JSONDecodeError as err:
        raise TranscriptError("YouTube returned an unparsable player response: {0}".format(err))


# --------------------------------------------------------------------------- fetch flow

def fetch_watch_html(opener, jar, video_id: str) -> str:
    url = WATCH_URL.format(video_id=video_id)
    html = unescape(_get(opener, url))
    if 'action="https://consent.youtube.com/s"' in html:
        match = _CONSENT_V_RE.search(html)
        if not match:
            raise TranscriptError("Hit YouTube's consent page and could not build a consent cookie.")
        _set_cookie(jar, "CONSENT", "YES+" + match.group(1))
        html = unescape(_get(opener, url))
        if 'action="https://consent.youtube.com/s"' in html:
            raise TranscriptError("Could not get past YouTube's consent page.")
    return html


def extract_api_key(html: str) -> str:
    match = _API_KEY_RE.search(html)
    if match:
        return match.group(1)
    if 'class="g-recaptcha"' in html:
        raise TranscriptError("YouTube served a reCAPTCHA - this IP is being blocked.")
    raise TranscriptError("Could not find the InnerTube API key (YouTube markup may have changed).")


def fetch_player(opener, video_id: str, api_key: str) -> dict:
    return _post_json(opener, PLAYER_URL.format(api_key=api_key),
                      {"context": INNERTUBE_CONTEXT, "videoId": video_id})


def extract_caption_tracks(player: dict, video_id: str) -> list:
    status = player.get("playabilityStatus") or {}
    state = status.get("status")
    if state and state != "OK":
        reason = status.get("reason") or state
        lowered = reason.lower()
        if "bot" in lowered or (state == "LOGIN_REQUIRED" and "sign in" in lowered):
            raise TranscriptError("YouTube requires sign-in (bot detection) for this video.")
        if "age" in lowered or state == "AGE_CHECK_REQUIRED":
            raise TranscriptError("Video is age-restricted: {0}".format(reason))
        raise TranscriptError("Video is not playable: {0}".format(reason))
    renderer = (player.get("captions") or {}).get("playerCaptionsTracklistRenderer") or {}
    tracks = renderer.get("captionTracks")
    if not tracks:
        raise TranscriptError("This video has no transcripts/captions available (or they are disabled).")
    return tracks


def _track_name(track: dict) -> str:
    try:
        return track["name"]["runs"][0]["text"]
    except (KeyError, IndexError, TypeError):
        return track.get("languageCode", "")


def _english(tracks: list, generated: bool) -> dict:
    """Pick the best English track of the requested kind (exact 'en' preferred over 'en-US' etc.)."""
    matches = [
        t for t in tracks
        if t.get("languageCode", "").lower().startswith("en")
        and (t.get("kind") == "asr") == generated
    ]
    if not matches:
        return None
    for track in matches:
        if track.get("languageCode", "").lower() == "en":
            return track
    return matches[0]


def fetch_snippets(opener, track: dict) -> list:
    url = track["baseUrl"].replace("&fmt=srv3", "")
    if "&exp=xpe" in url:
        raise TranscriptError("This transcript requires a PoToken and cannot be fetched without one.")
    # The timedtext payload is YouTube's own small XML document (no DTD); parse with the stdlib.
    xml = _get(opener, url)
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as err:
        raise TranscriptError("Could not parse the transcript XML: {0}".format(err))
    snippets = []
    for element in root:
        if element.text is None:
            continue
        snippets.append({
            "text": _TAG_RE.sub("", unescape(element.text)),
            "start": round(float(element.attrib.get("start", "0.0")), 3),
            "duration": round(float(element.attrib.get("dur", "0.0")), 3),
        })
    return snippets


def build_transcript(track: dict, snippets: list, video_id: str, source: str) -> dict:
    return {
        "video_id": video_id,
        "video_url": "https://www.youtube.com/watch?v={0}".format(video_id),
        "source": source,                       # "manual" (creator-uploaded) or "generated" (ASR)
        "is_generated": track.get("kind") == "asr",
        "language": _track_name(track),
        "language_code": track.get("languageCode", ""),
        "snippet_count": len(snippets),
        "snippets": snippets,
    }


# --------------------------------------------------------------------------- comparison

def _plain_text(transcript: dict) -> str:
    return " ".join(s["text"].strip() for s in transcript["snippets"] if s["text"].strip())


def _span(transcript: dict):
    snips = transcript["snippets"]
    if not snips:
        return 0.0, 0.0
    last = snips[-1]
    return snips[0]["start"], round(last["start"] + last["duration"], 3)


def build_comparison(manual: dict, generated: dict, video_id: str) -> str:
    man_text, gen_text = _plain_text(manual), _plain_text(generated)
    man_words, gen_words = man_text.split(), gen_text.split()
    ratio = difflib.SequenceMatcher(None, man_words, gen_words).ratio()
    man_start, man_end = _span(manual)
    gen_start, gen_end = _span(generated)

    lines = [
        "# Transcript comparison - {0}".format(video_id),
        "",
        "This video has **both** an English creator-uploaded (manual) transcript and an English "
        "auto-generated (ASR) transcript. That is uncommon; here is how they compare so you can decide "
        "which to use.",
        "",
        "| Metric | Manual (uploaded) | Generated (ASR) |",
        "| --- | --- | --- |",
        "| Language | {0} | {1} |".format(manual["language"], generated["language"]),
        "| Language code | {0} | {1} |".format(manual["language_code"], generated["language_code"]),
        "| Snippets | {0} | {1} |".format(manual["snippet_count"], generated["snippet_count"]),
        "| Words | {0} | {1} |".format(len(man_words), len(gen_words)),
        "| Time span (s) | {0} -> {1} | {2} -> {3} |".format(man_start, man_end, gen_start, gen_end),
        "",
        "**Word-level similarity:** {0:.1f}% "
        "(difflib SequenceMatcher ratio over whitespace tokens)".format(ratio * 100),
        "",
        "> Manual transcripts are human-authored (usually punctuated and cleaned up); auto-generated "
        "transcripts come from speech recognition and can mis-hear words. Where they disagree, the "
        "manual track is normally the more reliable source.",
        "",
        "## Sample differences",
        "",
        "Unified diff of the two transcripts as wrapped plain text (manual = `-`, generated = `+`), "
        "first 60 changed lines:",
        "",
        "```diff",
    ]
    man_wrapped = textwrap.wrap(man_text, width=90) or [""]
    gen_wrapped = textwrap.wrap(gen_text, width=90) or [""]
    diff = difflib.unified_diff(man_wrapped, gen_wrapped, fromfile="manual", tofile="generated", lineterm="")
    changed = [ln for ln in diff if ln and ln[0] in "+-" and not ln.startswith(("+++", "---"))]
    lines.extend(changed[:60] if changed else ["(no line-level differences in the wrapped text)"])
    lines.append("```")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- output / cli

def default_out_dir() -> Path:
    # This script lives at <repo>/support_scripts/yt_transcript.py.
    return Path(__file__).resolve().parent.parent / "AI" / "support_scripts_work" / "transcripts"


def print_languages(tracks: list) -> None:
    print("Available transcript tracks:")
    for track in tracks:
        kind = "generated (ASR)" if track.get("kind") == "asr" else "manual"
        translatable = " [translatable]" if track.get("isTranslatable") else ""
        print("  - {0:<8} {1:<16} {2}{3}".format(
            track.get("languageCode", "?"), kind, _track_name(track), translatable))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Pull YouTube-provided English transcripts as timestamped JSON.")
    parser.add_argument("video", help="YouTube video ID or full URL")
    parser.add_argument("--list", action="store_true",
                        help="list available transcript languages and exit")
    parser.add_argument("--out-dir", default=None,
                        help="output directory (default: <repo>/AI/support_scripts_work/transcripts)")
    parser.add_argument("--print", dest="to_stdout", action="store_true",
                        help="also print each transcript JSON to stdout")
    args = parser.parse_args(argv)

    try:
        video_id = extract_video_id(args.video)
        opener, jar = _build_opener()
        html = fetch_watch_html(opener, jar, video_id)
        api_key = extract_api_key(html)
        player = fetch_player(opener, video_id, api_key)
        tracks = extract_caption_tracks(player, video_id)

        if args.list:
            print_languages(tracks)
            return 0

        candidates = [("manual", _english(tracks, generated=False)),
                      ("generated", _english(tracks, generated=True))]
        found = [(label, track) for label, track in candidates if track]
        if not found:
            raise TranscriptError(
                "No English transcript found for this video. Run with --list to see what is available.")

        out_dir = Path(args.out_dir) if args.out_dir else default_out_dir()
        out_dir.mkdir(parents=True, exist_ok=True)

        results, written = {}, []
        for label, track in found:
            transcript = build_transcript(track, fetch_snippets(opener, track), video_id, label)
            results[label] = transcript
            path = out_dir / "{0}.{1}.json".format(video_id, label)
            path.write_text(json.dumps(transcript, ensure_ascii=False, indent=2), encoding="utf-8")
            written.append(path)
            if args.to_stdout:
                print(json.dumps(transcript, ensure_ascii=False, indent=2))

        if "manual" in results and "generated" in results:
            report = build_comparison(results["manual"], results["generated"], video_id)
            path = out_dir / "{0}.comparison.md".format(video_id)
            path.write_text(report, encoding="utf-8")
            written.append(path)

        print("Fetched {0} English transcript(s) for {1}:".format(len(found), video_id),
              file=sys.stderr)
        for label, track in found:
            print("  - {0:<10} {1} snippets ({2})".format(
                label, results[label]["snippet_count"], track.get("languageCode", "")), file=sys.stderr)
        if "manual" in results and "generated" in results:
            print("  - both present -> wrote a comparison report", file=sys.stderr)
        print("Output:", file=sys.stderr)
        for path in written:
            print("  {0}".format(path), file=sys.stderr)
        return 0

    except TranscriptError as err:
        print("error: {0}".format(err), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
