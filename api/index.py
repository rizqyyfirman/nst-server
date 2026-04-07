from flask import Flask, request, jsonify, send_from_directory
import os, jwt, bcrypt, json
from datetime import datetime, timedelta, timezone
from supabase import create_client, Client

app = Flask(__name__)

# ─── Konfigurasi dari environment variable Vercel ────
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")   # service_role key
JWT_SECRET   = os.environ.get("JWT_SECRET", "nst-secret-2024")
ADMIN_ID     = os.environ.get("ADMIN_ID", "admin")
# Hash bcrypt dari "tekmed123" — dibuat sekali, disimpan di env var
ADMIN_PW_HASH = os.environ.get("ADMIN_PW_HASH", "")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

BUCKET_NAME = "wav-files"

# ─── Helper JWT ──────────────────────────────────────
def make_token(username: str) -> str:
    payload = {
        "sub": username,
        "exp": datetime.now(timezone.utc) + timedelta(hours=8)
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")

def verify_token(req) -> str | None:
    auth = req.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    token = auth.split(" ", 1)[1]
    try:
        data = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        return data["sub"]
    except Exception:
        return None

# ─── Route: Login ─────────────────────────────────────
@app.route("/api/login", methods=["POST"])
def login():
    body = request.get_json(force=True)
    username = body.get("username", "")
    password = body.get("password", "")

    if username != ADMIN_ID:
        return jsonify({"error": "ID atau password salah"}), 401

    if not ADMIN_PW_HASH:
        return jsonify({"error": "Server belum dikonfigurasi"}), 500

    pw_bytes   = password.encode("utf-8")
    hash_bytes = ADMIN_PW_HASH.encode("utf-8")
    if not bcrypt.checkpw(pw_bytes, hash_bytes):
        return jsonify({"error": "ID atau password salah"}), 401

    return jsonify({"token": make_token(username)})

# ─── Route: Upload dari ESP32 ────────────────────────
@app.route("/api/upload", methods=["POST"])
def upload():
    try:
        nama           = request.form.get("nama", "")
        usia           = request.form.get("usia", "")
        id_pasien      = request.form.get("idPasien", "")
        usia_kandungan = request.form.get("usiaKandungan", "")
        tanggal        = request.form.get("tanggal", "")
        avg_bpm        = float(request.form.get("avgBpm", 0))

        if not nama or not id_pasien:
            return jsonify({"error": "Data tidak lengkap"}), 400

        wav_file = request.files.get("file")
        if not wav_file:
            return jsonify({"error": "File tidak ada"}), 400

        # Nama file aman untuk Supabase storage
        safe_nama   = nama.replace(" ", "_")
        safe_tanggal = tanggal.replace("/", "-").replace(" ", "_")
        file_name   = f"{safe_nama}_{safe_tanggal}.wav"

        # Upload ke Supabase Storage
        file_bytes = wav_file.read()
        res = supabase.storage.from_(BUCKET_NAME).upload(
            path=file_name,
            file=file_bytes,
            file_options={"content-type": "audio/wav", "upsert": "true"}
        )

        # Simpan metadata ke Supabase Database
        supabase.table("pemeriksaan").insert({
            "id_pasien":       id_pasien,
            "nama":            nama,
            "usia":            usia,
            "usia_kandungan":  usia_kandungan,
            "tanggal":         tanggal,
            "avg_bpm":         avg_bpm,
            "file_name":       file_name,
        }).execute()

        return jsonify({"success": True, "message": "Data berhasil disimpan"})

    except Exception as e:
        print(f"[Upload Error] {e}")
        return jsonify({"error": str(e)}), 500

# ─── Route: Daftar Pasien ─────────────────────────────
@app.route("/api/pasien", methods=["GET"])
def list_pasien():
    if not verify_token(request):
        return jsonify({"error": "Unauthorized"}), 401
    try:
        res = supabase.table("pemeriksaan")\
            .select("id_pasien, nama")\
            .execute()

        # Kelompokkan per id_pasien
        seen = {}
        for row in res.data:
            pid = row["id_pasien"]
            if pid not in seen:
                seen[pid] = {"id_pasien": pid, "nama": row["nama"], "jumlah_pemeriksaan": 0}
            seen[pid]["jumlah_pemeriksaan"] += 1

        result = sorted(seen.values(), key=lambda x: x["nama"])
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ─── Route: Pemeriksaan 1 Pasien ──────────────────────
@app.route("/api/pasien/<id_pasien>", methods=["GET"])
def detail_pasien(id_pasien):
    if not verify_token(request):
        return jsonify({"error": "Unauthorized"}), 401
    try:
        res = supabase.table("pemeriksaan")\
            .select("*")\
            .eq("id_pasien", id_pasien)\
            .order("created_at", desc=True)\
            .execute()

        # Tambahkan URL publik untuk setiap file WAV
        rows = []
        for row in res.data:
            fn = row.get("file_name", "")
            if fn:
                url_res = supabase.storage.from_(BUCKET_NAME)\
                    .get_public_url(fn)
                row["file_url"] = url_res
            rows.append(row)

        return jsonify(rows)
    except Exception as e:
        return jsonify({"error": str(e)}), 500