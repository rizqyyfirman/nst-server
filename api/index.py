from flask import Flask, request, jsonify, render_template
import os, jwt, bcrypt
from datetime import datetime, timedelta, timezone
from supabase import create_client, Client

app = Flask(__name__, template_folder="templates")

# ─── Config ──────────────────────────────────────────
SUPABASE_URL  = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY  = os.environ.get("SUPABASE_KEY", "")
JWT_SECRET    = os.environ.get("JWT_SECRET", "nst-secret-2024")
ADMIN_ID      = os.environ.get("ADMIN_ID", "admin")
ADMIN_PW_HASH = os.environ.get("ADMIN_PW_HASH", "")
BUCKET_NAME   = "wav-files"

# ─── Supabase client ─────────────────────────────────
def get_supabase() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_KEY)

# ─── JWT helper ──────────────────────────────────────
def make_token(username: str) -> str:
    payload = {
        "sub": username,
        "exp": datetime.now(timezone.utc) + timedelta(hours=8)
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")

def verify_token(req) -> bool:
    auth = req.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return False
    try:
        jwt.decode(auth.split(" ", 1)[1], JWT_SECRET, algorithms=["HS256"])
        return True
    except Exception:
        return False

# ─── CORS ────────────────────────────────────────────
@app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"]  = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response

@app.route("/api/<path:path>", methods=["OPTIONS"])
def options_handler(path):
    return jsonify({}), 200

# ─── Halaman utama ───────────────────────────────────
@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")

@app.route("/<path:path>", methods=["GET"])
def catch_all(path):
    if path.startswith("api/"):
        return jsonify({"error": "Not found"}), 404
    return render_template("index.html")

# ─── Health check ────────────────────────────────────
@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({
        "status":        "ok",
        "supabase_url":  SUPABASE_URL[:30] + "..." if SUPABASE_URL else "NOT SET",
        "supabase_key":  "SET" if SUPABASE_KEY else "NOT SET",
        "admin_pw_hash": "SET" if ADMIN_PW_HASH else "NOT SET"
    })

# ─── Login ───────────────────────────────────────────
@app.route("/api/login", methods=["POST"])
def login():
    try:
        body     = request.get_json(force=True)
        username = body.get("username", "")
        password = body.get("password", "")

        if username != ADMIN_ID:
            return jsonify({"error": "ID atau password salah"}), 401
        if not ADMIN_PW_HASH:
            return jsonify({"error": "ADMIN_PW_HASH belum diset"}), 500
        if not bcrypt.checkpw(password.encode(), ADMIN_PW_HASH.encode()):
            return jsonify({"error": "ID atau password salah"}), 401

        return jsonify({"token": make_token(username)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ─── Upload dari ESP32 ───────────────────────────────
@app.route("/api/upload", methods=["POST"])
def upload():
    try:
        nama           = request.form.get("nama",          "").strip()
        usia           = request.form.get("usia",          "").strip()
        id_pasien      = request.form.get("idPasien",      "").strip()
        usia_kandungan = request.form.get("usiaKandungan", "").strip()
        tanggal        = request.form.get("tanggal",       "").strip()
        avg_bpm        = float(request.form.get("avgBpm",  0))

        if not nama or not id_pasien:
            return jsonify({"error": "Field nama dan idPasien wajib diisi"}), 400

        wav_file = request.files.get("file")
        if not wav_file:
            return jsonify({"error": "File WAV tidak ada"}), 400

        safe_nama    = nama.replace(" ", "_")
        safe_tanggal = tanggal.replace("/", "-").replace(" ", "_")
        file_name    = f"{safe_nama}_{safe_tanggal}.wav"

        file_bytes = wav_file.read()
        print(f"[Upload] {file_name} — {len(file_bytes)} bytes")

        sb = get_supabase()

        sb.storage.from_(BUCKET_NAME).upload(
            path=file_name,
            file=file_bytes,
            file_options={"content-type": "audio/wav", "upsert": "true"}
        )

        sb.table("pemeriksaan").insert({
            "id_pasien":      id_pasien,
            "nama":           nama,
            "usia":           usia,
            "usia_kandungan": usia_kandungan,
            "tanggal":        tanggal,
            "avg_bpm":        avg_bpm,
            "file_name":      file_name,
        }).execute()

        print(f"[Upload] OK: {nama} ({id_pasien})")
        return jsonify({"success": True, "file": file_name})

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

# ─── Daftar semua pasien ─────────────────────────────
@app.route("/api/pasien", methods=["GET"])
def list_pasien():
    if not verify_token(request):
        return jsonify({"error": "Unauthorized"}), 401
    try:
        sb  = get_supabase()
        res = sb.table("pemeriksaan").select("id_pasien, nama").execute()

        if not res.data or not isinstance(res.data, list):
            return jsonify([])

        seen = {}
        for row in res.data:
            pid = row["id_pasien"]
            if pid not in seen:
                seen[pid] = {
                    "id_pasien":          pid,
                    "nama":               row["nama"],
                    "jumlah_pemeriksaan": 0
                }
            seen[pid]["jumlah_pemeriksaan"] += 1

        return jsonify(sorted(seen.values(), key=lambda x: x["nama"]))

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify([])

# ─── Detail pemeriksaan satu pasien ──────────────────
@app.route("/api/pasien/<id_pasien>", methods=["GET"])
def detail_pasien(id_pasien):
    if not verify_token(request):
        return jsonify({"error": "Unauthorized"}), 401
    try:
        sb  = get_supabase()
        res = sb.table("pemeriksaan")\
                .select("*")\
                .eq("id_pasien", id_pasien)\
                .order("created_at", desc=True)\
                .execute()

        if not res.data or not isinstance(res.data, list):
            return jsonify([])

        rows = []
        for row in res.data:
            fn = row.get("file_name", "")
            if fn:
                row["file_url"] = sb.storage.from_(BUCKET_NAME).get_public_url(fn)
            rows.append(row)

        return jsonify(rows)

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify([])