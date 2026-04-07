from flask import Flask, request, jsonify
import os, jwt, bcrypt
from datetime import datetime, timedelta, timezone
from supabase import create_client, Client

app = Flask(__name__)

# ─── Config dari Environment Variable ───────────────
SUPABASE_URL  = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY  = os.environ.get("SUPABASE_KEY", "")
JWT_SECRET    = os.environ.get("JWT_SECRET", "nst-secret-2024")
ADMIN_ID      = os.environ.get("ADMIN_ID", "admin")
ADMIN_PW_HASH = os.environ.get("ADMIN_PW_HASH", "")
BUCKET_NAME   = "wav-files"

# ─── Inisialisasi Supabase ───────────────────────────
def get_supabase() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_KEY)

# ─── Helper JWT ──────────────────────────────────────
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
    token = auth.split(" ", 1)[1]
    try:
        jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        return True
    except Exception:
        return False

# ─── CORS helper ────────────────────────────────────
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"]  = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response

@app.after_request
def after_request(response):
    return add_cors(response)

# ─── OPTIONS handler untuk preflight ────────────────
@app.route("/api/<path:path>", methods=["OPTIONS"])
def options_handler(path):
    return jsonify({}), 200

# ─── Health check ────────────────────────────────────
@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "supabase_url": SUPABASE_URL[:30] + "..." if SUPABASE_URL else "NOT SET",
        "supabase_key": "SET" if SUPABASE_KEY else "NOT SET",
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
            return jsonify({"error": "ADMIN_PW_HASH belum diset di environment"}), 500

        if not bcrypt.checkpw(password.encode(), ADMIN_PW_HASH.encode()):
            return jsonify({"error": "ID atau password salah"}), 401

        return jsonify({"token": make_token(username)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ─── Upload dari ESP32 ──────────────────────────────
@app.route("/api/upload", methods=["POST"])
def upload():
    try:
        # Ambil field form
        nama           = request.form.get("nama", "").strip()
        usia           = request.form.get("usia", "").strip()
        id_pasien      = request.form.get("idPasien", "").strip()
        usia_kandungan = request.form.get("usiaKandungan", "").strip()
        tanggal        = request.form.get("tanggal", "").strip()
        avg_bpm        = float(request.form.get("avgBpm", 0))

        if not nama or not id_pasien:
            return jsonify({"error": "Field nama dan idPasien wajib diisi"}), 400

        wav_file = request.files.get("file")
        if not wav_file:
            return jsonify({"error": "File WAV tidak ada"}), 400

        # Nama file aman
        safe_nama    = nama.replace(" ", "_")
        safe_tanggal = tanggal.replace("/", "-").replace(" ", "_")
        file_name    = f"{safe_nama}_{safe_tanggal}.wav"

        # Baca isi file
        file_bytes = wav_file.read()
        print(f"[Upload] File: {file_name}, size: {len(file_bytes)} bytes")

        # Upload ke Supabase Storage
        sb = get_supabase()
        sb.storage.from_(BUCKET_NAME).upload(
            path=file_name,
            file=file_bytes,
            file_options={
                "content-type": "audio/wav",
                "upsert":       "true"
            }
        )

        # Simpan metadata ke database
        sb.table("pemeriksaan").insert({
            "id_pasien":      id_pasien,
            "nama":           nama,
            "usia":           usia,
            "usia_kandungan": usia_kandungan,
            "tanggal":        tanggal,
            "avg_bpm":        avg_bpm,
            "file_name":      file_name,
        }).execute()

        print(f"[Upload] Berhasil: {nama} ({id_pasien})")
        return jsonify({"success": True, "file": file_name})

    except Exception as e:
        print(f"[Upload Error] {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

# ─── Daftar Pasien ───────────────────────────────────
@app.route("/api/pasien", methods=["GET"])
def list_pasien():
    if not verify_token(request):
        return jsonify({"error": "Unauthorized"}), 401
    try:
        sb  = get_supabase()
        res = sb.table("pemeriksaan").select("id_pasien, nama").execute()

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
        return jsonify({"error": str(e)}), 500

# ─── Detail Pemeriksaan 1 Pasien ─────────────────────
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

        rows = []
        for row in res.data:
            fn = row.get("file_name", "")
            if fn:
                row["file_url"] = sb.storage.from_(BUCKET_NAME).get_public_url(fn)
            rows.append(row)

        return jsonify(rows)
    except Exception as e:
        return jsonify({"error": str(e)}), 500