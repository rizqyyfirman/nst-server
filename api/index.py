from flask import Flask, request, jsonify, render_template, Response
import os, jwt, bcrypt, struct, audioop, io, wave
from datetime import datetime, timedelta, timezone
from supabase import create_client, Client
import requests as req_lib

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

# ─── Upload metadata — tanggal & waktu otomatis WIB ──
@app.route("/api/upload/meta", methods=["POST"])
def upload_meta():
    try:
        body           = request.get_json(force=True)
        nama           = str(body.get("nama",          "")).strip()
        usia           = str(body.get("usia",          "")).strip()
        id_pasien      = str(body.get("idPasien",      "")).strip()
        usia_kandungan = str(body.get("usiaKandungan", "")).strip()
        avg_bpm        = float(body.get("avgBpm",      0))
        file_name      = str(body.get("fileName",      "")).strip()

        if not nama or not id_pasien or not file_name:
            return jsonify({"error": "nama, idPasien, fileName wajib diisi"}), 400

        now_wib = datetime.now(timezone(timedelta(hours=7)))
        tanggal = now_wib.strftime("%d-%m-%Y")
        waktu   = now_wib.strftime("%H:%M")

        sb = get_supabase()
        sb.table("pemeriksaan").insert({
            "id_pasien":      id_pasien,
            "nama":           nama,
            "usia":           usia,
            "usia_kandungan": usia_kandungan,
            "tanggal":        tanggal,
            "waktu":          waktu,
            "avg_bpm":        avg_bpm,
            "file_name":      file_name,
        }).execute()

        print(f"[Meta] OK: {nama} ({id_pasien}) {tanggal} {waktu}")
        return jsonify({"success": True, "tanggal": tanggal, "waktu": waktu})

    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"error": str(e)}), 500

# ─── Upload WAV binary ───────────────────────────────
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

# ─── Resampling WAV menggunakan audioop (built-in Python) ─
def resample_wav_audioop(wav_bytes: bytes, target_sr: int) -> bytes:
    """
    Menggunakan audioop.ratecv() — built-in Python standard library.
    Ini adalah algoritma resampling yang sama dipakai oleh banyak
    audio software profesional. Tidak butuh ffmpeg atau library eksternal.

    Hasil: suara identik dengan SD card, durasi tetap 40 detik.
    """
    if len(wav_bytes) < 44:
        return wav_bytes

    # Parse WAV header
    num_channels     = struct.unpack_from('<H', wav_bytes, 22)[0]
    original_sr      = struct.unpack_from('<I', wav_bytes, 24)[0]
    bits_per_sample  = struct.unpack_from('<H', wav_bytes, 34)[0]
    data_size        = struct.unpack_from('<I', wav_bytes, 40)[0]
    audio_data       = wav_bytes[44:44 + data_size]

    bytes_per_sample = bits_per_sample // 8

    print(f"[Resample] {original_sr}Hz -> {target_sr}Hz, "
          f"{num_channels}ch, {bits_per_sample}bit, "
          f"{len(audio_data)} bytes data")

    # audioop.ratecv melakukan resampling dengan anti-aliasing filter
    # Parameter: (data, width, nchannels, inrate, outrate, state, weightA, weightB)
    # weightA=1, weightB=0 = filter standar (flat response)
    resampled_data, _ = audioop.ratecv(
        audio_data,        # raw PCM data
        bytes_per_sample,  # bytes per sample (2 untuk 16-bit)
        num_channels,      # jumlah channel
        original_sr,       # sample rate asal (800)
        target_sr,         # sample rate target (8000)
        None,              # state (None = mulai baru)
        1,                 # weightA
        0                  # weightB
    )

    print(f"[Resample] Selesai: {len(resampled_data)} bytes")

    # Buat WAV output baru
    out_buf = io.BytesIO()
    with wave.open(out_buf, 'wb') as wf:
        wf.setnchannels(num_channels)
        wf.setsampwidth(bytes_per_sample)
        wf.setframerate(target_sr)
        wf.writeframes(resampled_data)

    return out_buf.getvalue()

# ─── Proxy audio dengan resampling audioop ───────────
@app.route("/api/audio/<filename>", methods=["GET"])
def stream_audio(filename):
    """
    Ambil WAV dari Supabase.
    Jika sample rate tidak standar (800Hz):
    - Gunakan audioop.ratecv() untuk resampling ke 8000Hz
    - Algoritma sama dengan software audio profesional
    - Durasi tetap 40 detik, suara identik dengan SD card
    """
    try:
        supabase_host = SUPABASE_URL.rstrip('/')
        url = f"{supabase_host}/storage/v1/object/public/{BUCKET_NAME}/{filename}"

        r = req_lib.get(url, timeout=30)
        if r.status_code != 200:
            return jsonify({"error": f"File tidak ditemukan: {filename}"}), 404

        wav_bytes = r.content
        print(f"[Audio] {filename}: {len(wav_bytes)} bytes")

        if len(wav_bytes) >= 28:
            original_sr = struct.unpack_from('<I', wav_bytes, 24)[0]
            print(f"[Audio] Sample rate asli: {original_sr} Hz")

            browser_supported = {8000, 11025, 16000, 22050, 44100, 48000}

            if original_sr not in browser_supported:
                # Tentukan target yang paling dekat dan standar
                # Untuk 800Hz -> 8000Hz (faktor 10, bersih)
                if original_sr <= 8000:
                    target_sr = 8000
                elif original_sr <= 16000:
                    target_sr = 16000
                elif original_sr <= 22050:
                    target_sr = 22050
                else:
                    target_sr = 44100

                print(f"[Audio] Resampling {original_sr}Hz -> {target_sr}Hz menggunakan audioop")
                wav_bytes = resample_wav_audioop(wav_bytes, target_sr)
                print(f"[Audio] Output: {len(wav_bytes)} bytes")
            else:
                print(f"[Audio] Sample rate {original_sr}Hz sudah didukung browser")

        response = Response(wav_bytes, status=200, content_type="audio/wav")
        response.headers["Accept-Ranges"]               = "bytes"
        response.headers["Cache-Control"]               = "public, max-age=3600"
        response.headers["Content-Length"]              = str(len(wav_bytes))
        response.headers["Access-Control-Allow-Origin"] = "*"
        return response

    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"error": str(e)}), 500

# ─── Daftar pasien ───────────────────────────────────
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
        import traceback; traceback.print_exc()
        return jsonify([])

# ─── Detail pemeriksaan satu pasien ─────────────────
@app.route("/api/pasien/<id_pasien>", methods=["GET"])
def detail_pasien(id_pasien):
    if not verify_token(request):
        return jsonify({"error": "Unauthorized"}), 401
    try:
        sb  = get_supabase()
        res = sb.table("pemeriksaan")\
                .select("*")\
                .eq("id_pasien", id_pasien)\
                .order("created_at", desc=True).execute()
        if not res.data or not isinstance(res.data, list):
            return jsonify([])
        rows = []
        for row in res.data:
            fn = row.get("file_name", "")
            if fn:
                row["file_url"] = f"/api/audio/{fn}"
            rows.append(row)
        return jsonify(rows)
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify([])