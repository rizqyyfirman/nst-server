from flask import Flask, request, jsonify, render_template
import os, jwt, bcrypt
from datetime import datetime, timedelta, timezone
from supabase import create_client, Client

app = Flask(__name__, template_folder="templates")

SUPABASE_URL  = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY  = os.environ.get("SUPABASE_KEY", "")
JWT_SECRET    = os.environ.get("JWT_SECRET", "nst-secret-2024")
ADMIN_ID      = os.environ.get("ADMIN_ID", "admin")
ADMIN_PW_HASH = os.environ.get("ADMIN_PW_HASH", "")
BUCKET_NAME   = "wav-files"

def get_supabase() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_KEY)

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

@app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"]  = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response

@app.route("/api/<path:path>", methods=["OPTIONS"])
def options_handler(path):
    return jsonify({}), 200

@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")

@app.route("/<path:path>", methods=["GET"])
def catch_all(path):
    if path.startswith("api/"):
        return jsonify({"error": "Not found"}), 404
    return render_template("index.html")

@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({
        "status":        "ok",
        "supabase_url":  SUPABASE_URL[:30] + "..." if SUPABASE_URL else "NOT SET",
        "supabase_key":  "SET" if SUPABASE_KEY else "NOT SET",
        "admin_pw_hash": "SET" if ADMIN_PW_HASH else "NOT SET"
    })

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

# ─── Upload metadata — tanggal diambil otomatis ───────
@app.route("/api/upload/meta", methods=["POST"])
def upload_meta():
    try:
        body           = request.get_json(force=True)
        nama           = str(body.get("nama",           "")).strip()
        usia           = str(body.get("usia",           "")).strip()
        id_pasien      = str(body.get("idPasien",       "")).strip()
        usia_kandungan = str(body.get("usiaKandungan",  "")).strip()
        avg_bpm        = float(body.get("avgBpm",       0))
        file_name      = str(body.get("fileName",       "")).strip()

        if not nama or not id_pasien or not file_name:
            return jsonify({"error": "nama, idPasien, fileName wajib diisi"}), 400

        # Tanggal diambil otomatis dari waktu server (WIB = UTC+7)
        tanggal = datetime.now(timezone(timedelta(hours=7))).strftime("%d-%m-%Y")

        sb = get_supabase()
        sb.table("pemeriksaan").insert({
            "id_pasien":      id_pasien,
            "nama":           nama,
            "usia":           usia,
            "usia_kandungan": usia_kandungan,
            "tanggal":        tanggal,
            "avg_bpm":        avg_bpm,
            "file_name":      file_name,
        }).execute()

        print(f"[Meta] OK: {nama} ({id_pasien}) tgl={tanggal}")
        return jsonify({"success": True, "tanggal": tanggal})

    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"error": str(e)}), 500

# ─── Upload file WAV binary ───────────────────────────
@app.route("/api/upload/wav", methods=["POST"])
def upload_wav():
    try:
        file_name  = request.args.get("file", "").strip()
        if not file_name:
            return jsonify({"error": "Parameter ?file= wajib diisi"}), 400

        file_bytes = request.get_data()
        if len(file_bytes) == 0:
            return jsonify({"error": "File kosong"}), 400

        print(f"[WAV] Upload: {file_name} — {len(file_bytes)} bytes")

        sb = get_supabase()
        sb.storage.from_(BUCKET_NAME).upload(
            path=file_name,
            file=file_bytes,
            file_options={"content-type": "audio/wav", "upsert": "true"}
        )

        print(f"[WAV] OK: {file_name}")
        return jsonify({"success": True, "file": file_name})

    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"error": str(e)}), 500

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
                seen[pid] = {"id_pasien": pid, "nama": row["nama"],
                             "jumlah_pemeriksaan": 0}
            seen[pid]["jumlah_pemeriksaan"] += 1
        return jsonify(sorted(seen.values(), key=lambda x: x["nama"]))
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify([])

@app.route("/api/pasien/<id_pasien>", methods=["GET"])
def detail_pasien(id_pasien):
    if not verify_token(request):
        return jsonify({"error": "Unauthorized"}), 401
    try:
        sb  = get_supabase()
        res = sb.table("pemeriksaan").select("*")\
                .eq("id_pasien", id_pasien)\
                .order("created_at", desc=True).execute()
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
        import traceback; traceback.print_exc()
        return jsonify([])