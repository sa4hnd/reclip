import os
import uuid
import glob
import json
import base64
import tempfile
import subprocess
import threading
from flask import Flask, request, jsonify, send_file, render_template

app = Flask(__name__)
DOWNLOAD_DIR = os.path.join(os.path.dirname(__file__), "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

jobs = {}
COOKIES_FILE = os.path.join(os.path.dirname(__file__), "cookies.txt")

# On startup: if COOKIES_B64 env var is set, decode it to cookies.txt
cookies_b64 = os.environ.get("COOKIES_B64", "")
if cookies_b64:
    try:
        with open(COOKIES_FILE, "wb") as f:
            f.write(base64.b64decode(cookies_b64))
        print(f"[ReClip] Loaded cookies from COOKIES_B64 env var")
    except Exception as e:
        print(f"[ReClip] Failed to decode COOKIES_B64: {e}")


def yt_dlp_cmd(cookies_text=None):
    """Build base yt-dlp command with cookies if available."""
    cmd = ["yt-dlp", "--no-warnings", "--no-check-formats",
           "--extractor-args", "youtube:player_client=ios,web"]
    # Per-request cookies take priority over global cookies file
    if cookies_text:
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False,
                                          dir=DOWNLOAD_DIR, prefix="cookies_")
        tmp.write(cookies_text)
        tmp.close()
        cmd += ["--cookies", tmp.name]
    elif os.path.exists(COOKIES_FILE):
        cmd += ["--cookies", COOKIES_FILE]
    return cmd


def cleanup_temp_cookies(cmd):
    """Remove temp cookie file created by yt_dlp_cmd if any."""
    for i, arg in enumerate(cmd):
        if arg == "--cookies" and i + 1 < len(cmd):
            path = cmd[i + 1]
            if path.startswith(DOWNLOAD_DIR) and "cookies_" in path:
                try:
                    os.remove(path)
                except OSError:
                    pass
            break


@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response


# ──────────────────────────────────────────────
#  Search  (flat playlist — no video access needed)
# ──────────────────────────────────────────────

@app.route("/api/search", methods=["POST"])
def search():
    data = request.json
    query = data.get("query", "").strip()
    limit = data.get("limit", 20)
    if not query:
        return jsonify({"error": "No query provided"}), 400

    cmd = yt_dlp_cmd() + [f"ytsearch{limit}:{query}", "--flat-playlist",
           "-j", "--extractor-args", "youtube:player_skip=webpage"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        items = []
        for line in result.stdout.strip().split("\n"):
            if not line:
                continue
            try:
                entry = json.loads(line)
                items.append({
                    "id": entry.get("id", ""),
                    "title": entry.get("title", ""),
                    "uploader": entry.get("uploader") or entry.get("channel") or "",
                    "duration": entry.get("duration") or 0,
                    "thumbnail": entry.get("thumbnails", [{}])[-1].get("url", "") if entry.get("thumbnails") else "",
                    "viewCount": entry.get("view_count"),
                })
            except json.JSONDecodeError:
                continue

        return jsonify({"items": items})
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Search timed out"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400


# ──────────────────────────────────────────────
#  Stream URL  (bestaudio + metadata in one call)
# ──────────────────────────────────────────────

@app.route("/api/stream/<video_id>", methods=["GET", "POST"])
def get_stream(video_id):
    url = f"https://www.youtube.com/watch?v={video_id}"
    cookies_text = None
    if request.method == "POST" and request.json:
        cookies_text = request.json.get("cookies")
    cmd = yt_dlp_cmd(cookies_text) + ["-f", "bestaudio/best", "-j", "--no-playlist", url]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        cleanup_temp_cookies(cmd)
        if result.returncode != 0:
            return jsonify({"error": result.stderr.strip().split("\n")[-1]}), 400

        info = json.loads(result.stdout)
        audio_url = info.get("url", "")
        if not audio_url:
            return jsonify({"error": "No audio stream found"}), 400

        return jsonify({
            "audioUrl": audio_url,
            "title": info.get("title", ""),
            "uploader": info.get("uploader", ""),
            "duration": info.get("duration") or 0,
            "thumbnail": info.get("thumbnail", ""),
        })
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Timed out getting stream"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400


# ──────────────────────────────────────────────
#  Info  (metadata only — uses --skip-download)
# ──────────────────────────────────────────────

@app.route("/api/info", methods=["POST"])
def get_info():
    data = request.json
    url = data.get("url", "").strip()
    cookies_text = data.get("cookies")
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    # Use --print to get just metadata without format resolution
    cmd = yt_dlp_cmd(cookies_text) + [
        "--no-playlist", "--skip-download",
        "--print", "%(title)s\n%(thumbnail)s\n%(duration)s\n%(uploader)s",
        url
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        cleanup_temp_cookies(cmd)
        if result.returncode != 0:
            return jsonify({"error": result.stderr.strip().split("\n")[-1]}), 400

        lines = result.stdout.strip().split("\n")
        title = lines[0] if len(lines) > 0 else ""
        thumbnail = lines[1] if len(lines) > 1 else ""
        duration_str = lines[2] if len(lines) > 2 else "0"
        uploader = lines[3] if len(lines) > 3 else ""

        try:
            duration = int(float(duration_str)) if duration_str and duration_str != "NA" else 0
        except (ValueError, TypeError):
            duration = 0

        return jsonify({
            "title": title,
            "thumbnail": thumbnail,
            "duration": duration,
            "uploader": uploader,
            "formats": [],
        })
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Timed out fetching video info"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400


# ──────────────────────────────────────────────
#  Download  (async job-based)
# ──────────────────────────────────────────────

def run_download(job_id, url, format_choice, format_id, cookies_text=None):
    job = jobs[job_id]
    out_template = os.path.join(DOWNLOAD_DIR, f"{job_id}.%(ext)s")

    cmd = yt_dlp_cmd(cookies_text) + ["--no-playlist", "-o", out_template]

    if format_choice == "audio":
        cmd += ["-f", "bestaudio/best/worst", "-x", "--audio-format", "mp3"]
    elif format_id:
        cmd += ["-f", f"{format_id}+bestaudio/best", "--merge-output-format", "mp4"]
    else:
        cmd += ["-f", "bestvideo+bestaudio/best", "--merge-output-format", "mp4"]

    cmd.append(url)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        cleanup_temp_cookies(cmd)
        if result.returncode != 0:
            job["status"] = "error"
            job["error"] = result.stderr.strip().split("\n")[-1]
            return

        files = glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}.*"))
        if not files:
            job["status"] = "error"
            job["error"] = "Download completed but no file was found"
            return

        if format_choice == "audio":
            target = [f for f in files if f.endswith(".mp3")]
            chosen = target[0] if target else files[0]
        else:
            target = [f for f in files if f.endswith(".mp4")]
            chosen = target[0] if target else files[0]

        for f in files:
            if f != chosen:
                try:
                    os.remove(f)
                except OSError:
                    pass

        job["status"] = "done"
        job["file"] = chosen
        ext = os.path.splitext(chosen)[1]
        title = job.get("title", "").strip()
        if title:
            safe_title = "".join(c for c in title if c not in r'\/:*?"<>|').strip()[:20].strip()
            job["filename"] = f"{safe_title}{ext}" if safe_title else os.path.basename(chosen)
        else:
            job["filename"] = os.path.basename(chosen)
    except subprocess.TimeoutExpired:
        job["status"] = "error"
        job["error"] = "Download timed out (5 min limit)"
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)


@app.route("/api/download", methods=["POST"])
def start_download():
    data = request.json
    url = data.get("url", "").strip()
    format_choice = data.get("format", "video")
    format_id = data.get("format_id")
    title = data.get("title", "")
    cookies_text = data.get("cookies")

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    job_id = uuid.uuid4().hex[:10]
    jobs[job_id] = {"status": "downloading", "url": url, "title": title}

    thread = threading.Thread(target=run_download, args=(job_id, url, format_choice, format_id, cookies_text))
    thread.daemon = True
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/api/status/<job_id>")
def check_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({
        "status": job["status"],
        "error": job.get("error"),
        "filename": job.get("filename"),
    })


@app.route("/api/file/<job_id>")
def download_file(job_id):
    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "File not ready"}), 404
    return send_file(job["file"], as_attachment=True, download_name=job["filename"])


@app.route("/")
def index():
    return render_template("index.html")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8899))
    host = os.environ.get("HOST", "127.0.0.1")
    app.run(host=host, port=port)
