# Local Instagram acquisition pipeline

Download one selected Instagram Reel, photo or carousel into a validated local reference library. This is a CLI that an agent can invoke; it does not require an MCP server. It never generates or publishes content.

## What is proven

On 10 November 2026, the Reel route downloaded a live public Instagram Reel with an explicitly authorized local Chrome session. The 28.534-second 720×1280 H.264 video contained AAC audio and passed a complete FFmpeg decode. Sixteen regression tests pass, including real media decoding, cookie-reader compatibility, carousel ordering and partial-download rejection.

**Carousel acquisition has implementation and fixture tests, but no successful live test yet.** Tests are not evidence of universal Instagram access. Downloaded content must still be reviewed against the source before production.

## Install

Use Python 3.10 or newer, with FFmpeg/ffprobe on PATH. The live test used macOS. Unix process-group handling and file locking are used; Windows is not supported by this wrapper yet.

From the repository root:

```sh
python3 -m venv .venv-instagram
.venv-instagram/bin/python -m pip install -r instagram_pipeline/requirements.txt
.venv-instagram/bin/python instagram_pipeline/scripts/instagram_media.py doctor
```

Pinned downloader versions: yt-dlp 2026.10.26, gallery-dl 1.32.19, Pillow 13.0.0. The requirements file also pins their installed dependencies. Native-wheel availability can vary with Python/platform.

The wrapper deliberately uses these released packages. It does not install the copy of upstream yt-dlp in this fork. This keeps the tested pipeline version separate from future upstream merges.

## Download

Replace `SHORTCODE` with a real post identifier:

```sh
# Public-only Reel attempt
.venv-instagram/bin/python instagram_pipeline/scripts/instagram_media.py fetch \
  'https://www.instagram.com/reel/SHORTCODE/' --kind reel

# Explicitly authorized local Chrome session
.venv-instagram/bin/python instagram_pipeline/scripts/instagram_media.py fetch \
  'https://www.instagram.com/reel/SHORTCODE/' --kind reel --browser chrome

# Photo or carousel (including mixed image/video slides)
.venv-instagram/bin/python instagram_pipeline/scripts/instagram_media.py fetch \
  'https://www.instagram.com/p/SHORTCODE/' --kind carousel --browser chrome
```

`--library /absolute/path` chooses a different archive directory. The default is `instagram_pipeline/reference-library/instagram-media/`. `--profile 'Profile 1'` selects a specifically authorized Chrome profile. Only individual HTTPS Instagram post/Reel URLs are accepted; profile, feed and arbitrary URLs are rejected.

## Chrome and macOS Keychain

Sign into the intended account in regular Chrome yourself. This tool does not inherit the Codex in-app browser session. Local session use is opt-in and requires the account holder's authorization.

```sh
.venv-instagram/bin/python instagram_pipeline/scripts/instagram_media.py doctor --browser chrome
```

The doctor verifies that a local session cookie is available; it does not verify server acceptance. macOS may show a **Chrome Safe Storage** permission prompt. Review and handle that prompt yourself. A one-time approval may need to be repeated on a later invocation. Never paste passwords, cookies or Keychain secrets into a chat or issue.

The tool reads Chrome cookies through yt-dlp's supported reader, filters to unexpired Instagram cookies, preserves yt-dlp's required cookie-jar type and keeps them in memory. It exports no cookie file and makes no password-login attempt. Do not share the browser profile or use a hosted downloader with those credentials.

## Outputs and production handoff

A successful exit code `0` prints absolute paths to files and `manifest.json`. Each post has its own shortcode directory. Slides are named `001`, `002`, etc. in original order. The manifest includes:

- Canonical original URL, acquisition time and method.
- Extractor-reported item count and collected count.
- Caption/date metadata when available.
- Per-file order, type, dimensions, byte size and SHA-256 hash.
- Video duration, audio presence and full-decode result.

The status is `ready_for_content_review`, with `source_identity_verified: false`. All extractor-reported items must pass checks. This does **not** prove that Instagram exposed every original slide, that audio is complete, that quality is original, or that the post is a performance outlier. Review the original and downloaded assets before generating or publishing anything.

A cached manifest triggers decoding/hash checks before reuse. Changed assets or incomplete archives are rejected. Mixed carousel video files keep their downloaded container; check downstream codec compatibility. Separately supplied Instagram carousel music is not claimed to be downloaded.

## Blocked results

Exit code `2` returns a sanitized reason. Examples:

| Reason | Meaning / next step |
| --- | --- |
| `browser_session_locked` | The Chrome session could not be decrypted; resolve normal Keychain access locally. |
| `instagram_session_missing` | No unexpired Instagram session found in the selected Chrome profile. |
| `verification_required` | Stop; complete Instagram's normal verification yourself. |
| `rate_limited` | Stop; wait rather than switching accounts or proxies. |
| `access_restricted` | Instagram did not grant access. |
| `incomplete_carousel` | Not all expected slides were acquired. No ready manifest is issued. |
| `download_failed` | Generic upstream failure; success must not be inferred. |
| `operation_timeout` | The complete worker exceeded its time limit and its process group was stopped. |

Network acquisition failures have a 24-hour per-post cooldown. Local session preflight failures permit a retry after fixing access because they occur before contacting Instagram. No automatic provider/account rotation or challenge bypass exists. Raw provider output and signed CDN URLs are not stored. Temporary partial files are removed.

Limits: at most 20 carousel items; JPG/JPEG/PNG/WebP images and common video containers; 1 GiB total carousel payload; 30-minute video duration; 90-second doctor and 300-second acquisition worker timeouts. A library path should be local and trusted.

## Import a user-saved video

The preserved legacy helper can validate and import a phone download or screen recording:

```sh
.venv-instagram/bin/python instagram_pipeline/scripts/instagram_download.py import \
  'https://www.instagram.com/p/SHORTCODE/' \
  --file '/absolute/path/reference.mp4' --method screen-recording
```

Its default library is `instagram_pipeline/reference-library/instagram/`. It supports a legacy `--job` JSON handoff for an unsubmitted matching-source job; it does not support carousels or Chrome sessions. Use `instagram_media.py` for new network acquisition.

## Tests

Run outside the repository root so the fork's upstream source tree does not shadow the pinned yt-dlp package:

```sh
REPO_PATH="$PWD"
cd /tmp
"$REPO_PATH/.venv-instagram/bin/python" -m unittest discover \
  -s "$REPO_PATH/instagram_pipeline/tests" -p 'test_instagram*py' -v
```

The tests use synthetic media and mocked provider responses. No real cookies, passwords or competitor media are fixtures. Live Reel validation is a separate manual result; live carousel validation remains outstanding.

## Maintenance and attribution

- Keep upstream [yt-dlp](https://github.com/yt-dlp/yt-dlp) changes separate from `instagram_pipeline/` changes.
- Update dependencies deliberately, rerun tests and repeat both live acceptance cases before promoting a new version.
- [gallery-dl](https://github.com/mikf/gallery-dl) supplies carousel extraction; [Pillow](https://python-pillow.org/) validates images; [FFmpeg](https://ffmpeg.org/) validates video/audio.
- The wrapper follows this repository's [LICENSE](../LICENSE). Dependencies retain their own licenses; their implementations are installed as dependencies rather than copied here.
- Do not file Instagram session details or downloaded media in GitHub issues. This repository contains code and documentation only.
