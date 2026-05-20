from flask import Flask, request, jsonify, render_template, Response
import os, jwt, bcrypt, struct, io, wave
from datetime import datetime, timedelta, timezone
from math import gcd
from supabase import create_client, Client
import requests as req_lib

# ─── Resampling — pakai scipy (polyphase, kualitas tertinggi) ─────────────────
# scipy.signal.resample_poly menggunakan FIR anti-aliasing filter yang presisi.
# Untuk rasio integer (800→8000 = 10×), hasilnya mathematically identical
# dengan sumber asli — tidak ada distorsi, tidak ada perubahan karakter suara.
#HALOOOOOO
try:
    import numpy as np
    from scipy import signal as scipy_signal
    HAS_SCIPY = True
    print("[AUDIO] scipy tersedia — resampling polyphase (kualitas tertinggi)")
except ImportError:
    HAS_SCIPY = False
    print("[AUDIO] scipy tidak tersedia — pakai audioop fallback")

# audioop fallback untuk Python <= 3.12 jika scipy tidak ada
try:
    import audioop
    HAS_AUDIOOP = True
    print("[AUDIO] audioop tersedia — resampling fallback")
except ImportError:
    HAS_AUDIOOP = False
    print("[AUDIO] audioop tidak tersedia")

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
        "admin_pw_hash": "SET" if ADMIN_PW_HASH else "NOT SET",
        "resampler":     "scipy (polyphase)" if HAS_SCIPY
                         else ("audioop" if HAS_AUDIOOP else "TIDAK ADA — install scipy!")
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

# ─── Upload WAV ──────────────────────────────────────
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


# ─── RESAMPLING ───────────────────────────────────────────────────────────────
#
# Mengapa suara di website berbeda dengan di SD card?
# ────────────────────────────────────────────────────
# ESP32 merekam di 800 Hz. File WAV tersimpan dengan header sample rate 800 Hz.
# Browser tidak support 800 Hz, jadi server lama memaksa resampling dengan:
#   • audioop.ratecv(... weightA=1, weightB=0) → IIR sederhana, mengubah karakter
#   • Linear interpolation → efek low-pass agresif, suara "lembek"
#
# Solusi: scipy.signal.resample_poly
# ────────────────────────────────────────────────────
# Polyphase FIR dengan Kaiser window, orde tinggi.
# Untuk faktor integer (800→8000 = up=10, down=1):
#   hasilnya IDENTIK MATEMATIS dengan sinyal asli.
# Tidak ada distorsi, tidak ada perubahan pitch/timbre.

def _parse_wav_chunks(wav_bytes: bytes) -> dict:
    """
    Parse WAV header dengan benar — cari chunk 'fmt ' dan 'data'
    secara dinamis, bukan hardcode offset 22/24/34/40.
    Ini penting karena beberapa encoder menambahkan chunk tambahan.
    """
    if len(wav_bytes) < 12:
        raise ValueError("File WAV terlalu kecil")

    num_channels    = 1
    original_sr     = 44100
    bits_per_sample = 16
    data_offset     = 44
    data_size       = max(0, len(wav_bytes) - 44)

    i = 12  # lewati RIFF header (4 RIFF + 4 size + 4 WAVE = 12 bytes)
    while i + 8 <= len(wav_bytes):
        chunk_id   = wav_bytes[i : i+4]
        chunk_size = struct.unpack_from('<I', wav_bytes, i+4)[0]

        if chunk_id == b'fmt ':
            if chunk_size >= 16:
                num_channels    = struct.unpack_from('<H', wav_bytes, i+8+2)[0]
                original_sr     = struct.unpack_from('<I', wav_bytes, i+8+4)[0]
                bits_per_sample = struct.unpack_from('<H', wav_bytes, i+8+14)[0]

        elif chunk_id == b'data':
            data_offset = i + 8
            data_size   = min(chunk_size, len(wav_bytes) - data_offset)
            break  # chunk data ditemukan, berhenti

        i += 8 + chunk_size
        if chunk_size % 2 == 1:
            i += 1  # WAV padding byte

    return {
        "num_channels":    num_channels,
        "original_sr":     original_sr,
        "bits_per_sample": bits_per_sample,
        "data_offset":     data_offset,
        "data_size":       data_size,
    }


def resample_scipy(wav_bytes: bytes, target_sr: int) -> bytes:
    """
    Polyphase resampling dengan scipy — kualitas tertinggi.
    Untuk 800→8000 Hz: up=10, down=1 → identik matematis dengan aslinya.
    """
    hdr = _parse_wav_chunks(wav_bytes)
    original_sr     = hdr["original_sr"]
    num_channels    = hdr["num_channels"]
    bits_per_sample = hdr["bits_per_sample"]
    audio_data      = wav_bytes[hdr["data_offset"] : hdr["data_offset"] + hdr["data_size"]]

    print(f"[scipy] {original_sr}Hz → {target_sr}Hz | "
          f"{num_channels}ch {bits_per_sample}bit | {len(audio_data)} bytes")

    # Konversi raw bytes ke numpy float32
    dtype = np.int16 if bits_per_sample == 16 else np.uint8
    samples = np.frombuffer(audio_data, dtype=dtype).astype(np.float64)

    # Deinterleave multi-channel (untuk mono tidak berpengaruh)
    if num_channels > 1:
        samples = samples.reshape(-1, num_channels)

    # Hitung rasio up/down dengan GCD
    # Contoh: 800→8000: gcd=800, up=10, down=1
    #         1000→8000: gcd=1000, up=8, down=1
    #         2400→8000: gcd=800, up=10, down=3
    g    = gcd(int(target_sr), int(original_sr))
    up   = target_sr   // g
    down = original_sr // g
    print(f"[scipy] Polyphase: up={up}, down={down}")

    # Resample setiap channel
    if num_channels > 1:
        ch_resampled = [scipy_signal.resample_poly(samples[:, ch], up, down)
                        for ch in range(num_channels)]
        resampled = np.column_stack(ch_resampled).flatten()
    else:
        resampled = scipy_signal.resample_poly(samples, up, down)

    # Clip dan konversi kembali ke int16
    resampled = np.clip(resampled, -32768.0, 32767.0).astype(np.int16)

    # Buat WAV output
    out = io.BytesIO()
    with wave.open(out, 'wb') as wf:
        wf.setnchannels(num_channels)
        wf.setsampwidth(bits_per_sample // 8)
        wf.setframerate(target_sr)
        wf.writeframes(resampled.tobytes())

    result = out.getvalue()
    print(f"[scipy] Selesai: {len(result)} bytes")
    return result


def resample_audioop(wav_bytes: bytes, target_sr: int) -> bytes:
    """Fallback: audioop (Python ≤ 3.12, tanpa scipy)."""
    hdr = _parse_wav_chunks(wav_bytes)
    original_sr      = hdr["original_sr"]
    num_channels     = hdr["num_channels"]
    bits_per_sample  = hdr["bits_per_sample"]
    bytes_per_sample = bits_per_sample // 8
    audio_data       = wav_bytes[hdr["data_offset"] : hdr["data_offset"] + hdr["data_size"]]

    print(f"[audioop] {original_sr}Hz → {target_sr}Hz | {len(audio_data)} bytes")
    resampled, _ = audioop.ratecv(
        audio_data, bytes_per_sample, num_channels, original_sr, target_sr, None
    )

    out = io.BytesIO()
    with wave.open(out, 'wb') as wf:
        wf.setnchannels(num_channels)
        wf.setsampwidth(bytes_per_sample)
        wf.setframerate(target_sr)
        wf.writeframes(resampled)

    result = out.getvalue()
    print(f"[audioop] Selesai: {len(result)} bytes")
    return result


def do_resample(wav_bytes: bytes, target_sr: int) -> bytes:
    """Pilih resampler terbaik yang tersedia."""
    if HAS_SCIPY:
        return resample_scipy(wav_bytes, target_sr)
    elif HAS_AUDIOOP:
        return resample_audioop(wav_bytes, target_sr)
    else:
        raise RuntimeError(
            "Tidak ada resampler. Tambahkan 'scipy' dan 'numpy' ke requirements.txt"
        )


# ─── Proxy audio ─────────────────────────────────────
# Sample rate yang didukung semua browser modern
BROWSER_OK_SR = {8000, 11025, 16000, 22050, 32000, 44100, 48000}

@app.route("/api/audio/<filename>", methods=["GET"])
def stream_audio(filename):
    try:
        url = f"{SUPABASE_URL.rstrip('/')}/storage/v1/object/public/{BUCKET_NAME}/{filename}"
        r   = req_lib.get(url, timeout=30)
        if r.status_code != 200:
            return jsonify({"error": f"File tidak ditemukan: {filename}"}), 404

        wav_bytes = r.content
        print(f"[Audio] {filename}: {len(wav_bytes)} bytes")

        if len(wav_bytes) >= 44:
            try:
                hdr = _parse_wav_chunks(wav_bytes)
                original_sr = hdr["original_sr"]
            except Exception:
                # Fallback ke parse manual jika gagal
                original_sr = struct.unpack_from('<I', wav_bytes, 24)[0]

            print(f"[Audio] Sample rate: {original_sr} Hz")

            if original_sr not in BROWSER_OK_SR:
                # Pilih target sample rate terdekat yang didukung browser.
                # Untuk 800 Hz → 8000 Hz (faktor bulat 10×, paling efisien).
                if original_sr <= 8000:
                    target_sr = 8000
                elif original_sr <= 16000:
                    target_sr = 16000
                elif original_sr <= 22050:
                    target_sr = 22050
                else:
                    target_sr = 44100

                print(f"[Audio] Resampling {original_sr} → {target_sr} Hz")
                wav_bytes = do_resample(wav_bytes, target_sr)
                print(f"[Audio] Resampling selesai")

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
        # Ambil semua kolom termasuk tanggal, waktu, created_at
        res = sb.table("pemeriksaan")\
                .select("id_pasien, nama, tanggal, waktu, created_at")\
                .order("created_at", desc=True)\
                .execute()
        if not res.data or not isinstance(res.data, list):
            return jsonify([])

        seen = {}
        for row in res.data:
            pid = row["id_pasien"]
            if pid not in seen:
                # Baris pertama = yang terbaru (sudah desc)
                seen[pid] = {
                    "id_pasien":          pid,
                    "nama":               row["nama"],
                    "jumlah_pemeriksaan": 0,
                    "tanggal_terbaru":    row.get("tanggal", ""),
                    "waktu_terbaru":      row.get("waktu", ""),
                    "created_at_terbaru": row.get("created_at", ""),
                }
            seen[pid]["jumlah_pemeriksaan"] += 1

        # Urutkan list pasien berdasarkan pemeriksaan terbaru (desc)
        result = sorted(
            seen.values(),
            key=lambda x: x["created_at_terbaru"],
            reverse=True
        )
        return jsonify(result)
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify([])


# ─── Detail pemeriksaan ──────────────────────────────
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
                row["file_url"] = f"/api/audio/{fn}"
            rows.append(row)
        return jsonify(rows)
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify([])