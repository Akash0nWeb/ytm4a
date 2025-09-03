from flask import Flask, render_template, request, send_file, redirect, url_for, flash, jsonify, Response, abort, after_this_request
import os, yt_dlp, tempfile, uuid, threading, time, json, re

app = Flask(__name__)
app.secret_key = "ytm4a_secret"

JOBS = {}  # job_id -> dict(status, pct, eta, speed, title, filepath, error)

INVALID_FS_CHARS = r'[\\/:*?"<>|]'

def sanitize_title(title: str) -> str:
    safe = re.sub(INVALID_FS_CHARS, "_", title).strip().rstrip(".")
    return (safe[:180] if len(safe) > 180 else safe) or "ytm4a_audio"

def human_speed(bps):
    if not bps:
        return ""
    units = ["B/s", "KB/s", "MB/s", "GB/s"]
    i = 0
    while bps >= 1024 and i < len(units) - 1:
        bps /= 1024.0
        i += 1
    return f"{bps:.1f} {units[i]}"

def download_job(job_id: str, url: str):
    tmpdir = tempfile.gettempdir()
    outtmpl = os.path.join(tmpdir, f"{job_id}_" + "%(title)s.%(ext)s")
    JOBS[job_id] = {"status": "starting", "pct": 0, "eta": None, "speed": "", "title": "", "filepath": "", "error": ""}

    def hook(d):
        try:
            if d["status"] == "downloading":
                downloaded = d.get("downloaded_bytes") or 0
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                pct = int(downloaded * 100 / total) if total else 0
                speed = human_speed(d.get("speed"))
                eta = d.get("eta")
                JOBS[job_id].update({"status": "downloading", "pct": pct, "eta": eta, "speed": speed})
            elif d["status"] == "finished":
                JOBS[job_id].update({"status": "post", "pct": 100})
        except Exception as e:
            JOBS[job_id].update({"status": "error", "error": str(e)})

    ydl_opts = {
        "format": "bestaudio[ext=m4a]/bestaudio",
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "progress_hooks": [hook],
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title = info.get("title") or "ytm4a_audio"
            filename = ydl.prepare_filename(info)

        JOBS[job_id].update({
            "status": "ready",
            "title": title,
            "filepath": filename,
            "pct": 100,
            "eta": 0,
            "speed": ""
        })

    except Exception as e:
        JOBS[job_id].update({"status": "error", "error": str(e)})


@app.route("/", methods=["GET"])
def home():
    return render_template("index.html")


@app.route("/start", methods=["POST"])
def start():
    url = request.form.get("url")
    if not url:
        flash("Please paste a YouTube URL.", "error")
        return redirect(url_for("home"))
    job_id = str(uuid.uuid4())
    t = threading.Thread(target=download_job, args=(job_id, url), daemon=True)
    t.start()
    return jsonify({"job_id": job_id})


@app.route("/events/<job_id>")
def events(job_id):
    def gen():
        last_payload = None
        while True:
            job = JOBS.get(job_id)
            if not job:
                payload = {"status": "unknown"}
            else:
                payload = {
                    "status": job["status"],
                    "pct": job.get("pct", 0),
                    "eta": job.get("eta"),
                    "speed": job.get("speed", ""),
                    "title": job.get("title", ""),
                }
                if job["status"] in ("ready", "error"):
                    yield f"data: {json.dumps(payload)}\n\n"
                    break

            now_payload = json.dumps(payload)
            if now_payload != last_payload:
                yield f"data: {now_payload}\n\n"
                last_payload = now_payload
            time.sleep(0.5)
    return Response(gen(), mimetype="text/event-stream")


@app.route("/file/<job_id>")
def file(job_id):
    job = JOBS.get(job_id)
    if not job or job.get("status") != "ready":
        return abort(404)
    path = job.get("filepath")
    if not path or not os.path.exists(path):
        return abort(404)

    title = sanitize_title(job.get("title") or os.path.splitext(os.path.basename(path))[0])
    ext = os.path.splitext(path)[1] or ".m4a"
    download_name = f"{title}{ext}"

    @after_this_request
    def cleanup(response):
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass
        JOBS.pop(job_id, None)
        return response

    return send_file(path, as_attachment=True, download_name=download_name)


if __name__ == "__main__":
    app.run()
