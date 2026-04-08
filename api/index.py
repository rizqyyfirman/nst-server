from flask import Flask, request, jsonify, render_template, Response, stream_with_context
import os, jwt, bcrypt, struct, io
from datetime import datetime, timedelta, timezone
from supabase import create_client, Client
import requests as req_lib

app = Flask(__name__, template_folder="templates")

# ─── Config ──────────────────────────────────────────
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

# ─── Upload metadata ─────────────────────────────────
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

        # Tanggal otomatis WIB
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

# ─── Konversi WAV: ubah sample rate di header ────────
def resample_wav_header(wav_bytes: bytes,
                        original_sr: int,
                        target_sr: int) -> bytes:
    """
    Konversi WAV dengan cara mengubah header saja +
    duplikasi sampel (nearest-neighbor upsampling).
    Kualitas suara tetap sama, hanya kecepatan putar
    yang disesuaikan agar browser bisa membaca.
    """
    # Parse header WAV (44 byte standar)
    if len(wav_bytes) < 44:
        return wav_bytes

    # Baca info dari header asli
    # Offset 22: numChannels (2 byte)
    # Offset 24: sampleRate  (4 byte)
    # Offset 28: byteRate    (4 byte)
    # Offset 32: blockAlign  (2 byte)
    # Offset 34: bitsPerSample (2 byte)
    # Offset 40: dataSize    (4 byte)
    num_channels   = struct.unpack_from('<H', wav_bytes, 22)[0]
    bits_per_sample = struct.unpack_from('<H', wav_bytes, 34)[0]
    data_size      = struct.unpack_from('<I', wav_bytes, 40)[0]

    # Ambil data audio (setelah 44 byte header)
    audio_data = wav_bytes[44:44 + data_size]

    # Hitung faktor upsampling
    factor = target_sr // original_sr  # 8000 // 800 = 10

    # Upsample dengan duplikasi sampel (nearest-neighbor)
    # Setiap sampel diduplikasi sebanyak 'factor' kali
    bytes_per_sample = bits_per_sample // 8  # 2 byte untuk 16-bit
    frame_size       = bytes_per_sample * num_channels
    new_audio        = bytearray()

    for i in range(0, len(audio_data), frame_size):
        frame = audio_data[i:i + frame_size]
        if len(frame) == frame_size:
            for _ in range(factor):
                new_audio.extend(frame)

    new_data_size = len(new_audio)
    new_byte_rate = target_sr * num_channels * bytes_per_sample
    new_block_align = num_channels * bytes_per_sample
    new_file_size   = new_data_size + 36  # 44 - 8

    # Buat header baru
    header = bytearray(44)
    header[0:4]   = b'RIFF'
    struct.pack_into('<I', header, 4,  new_file_size)
    header[8:12]  = b'WAVE'
    header[12:16] = b'fmt '
    struct.pack_into('<I', header, 16, 16)           # fmtSize
    struct.pack_into('<H', header, 20, 1)            # audioFormat PCM
    struct.pack_into('<H', header, 22, num_channels)
    struct.pack_into('<I', header, 24, target_sr)    # sampleRate baru
    struct.pack_into('<I', header, 28, new_byte_rate)
    struct.pack_into('<H', header, 32, new_block_align)
    struct.pack_into('<H', header, 34, bits_per_sample)
    header[36:40] = b'data'
    struct.pack_into('<I', header, 40, new_data_size)

    return bytes(header) + bytes(new_audio)

# ─── Proxy audio dengan konversi sample rate ─────────
@app.route("/api/audio/<filename>", methods=["GET"])
def stream_audio(filename):
    """
    Ambil WAV dari Supabase, konversi dari 800Hz ke 8000Hz,
    lalu kirim ke browser. Browser bisa memutar dengan normal.
    """
    try:
        supabase_host = SUPABASE_URL.rstrip('/')
        url = f"{supabase_host}/storage/v1/object/public/{BUCKET_NAME}/{filename}"

        print(f"[Audio] Fetch: {url}")

        r = req_lib.get(url, timeout=30)
        if r.status_code != 200:
            print(f"[Audio] Supabase error {r.status_code}: {r.text}")
            return jsonify({"error": f"File tidak ditemukan: {filename}"}), 404

        wav_bytes = r.content
        print(f"[Audio] File asli: {len(wav_bytes)} bytes")

        # Deteksi sample rate dari header WAV
        if len(wav_bytes) >= 28:
            original_sr = struct.unpack_from('<I', wav_bytes, 24)[0]
            print(f"[Audio] Sample rate asli: {original_sr} Hz")

            # Konversi ke 8000Hz jika sample rate bukan standar browser
            if original_sr not in (8000, 11025, 16000, 22050, 44100, 48000):
                target_sr = 8000
                # Cari kelipatan terdekat yang bulat
                # 800 * 10 = 8000, 400 * 20 = 8000, dst
                if 8000 % original_sr == 0:
                    target_sr = 8000
                elif 16000 % original_sr == 0:
                    target_sr = 16000
                else:
                    target_sr = 8000

                print(f"[Audio] Konversi {original_sr}Hz -> {target_sr}Hz")
                wav_bytes = resample_wav_header(wav_bytes, original_sr, target_sr)
                print(f"[Audio] File setelah konversi: {len(wav_bytes)} bytes")
            else:
                print(f"[Audio] Sample rate sudah standar, tidak dikonversi")

        response = Response(
            wav_bytes,
            status=200,
            content_type="audio/wav"
        )
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
                # Gunakan proxy route agar sample rate dikonversi otomatis
                row["file_url"] = f"/api/audio/{fn}"
            rows.append(row)

        return jsonify(rows)

    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify([])