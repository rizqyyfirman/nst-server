const express  = require('express');
const cors     = require('cors');
const path     = require('path');
const fs       = require('fs');
const multer   = require('multer');
const Database = require('better-sqlite3');
const bcrypt   = require('bcryptjs');
const jwt      = require('jsonwebtoken');

const app = express();
const PORT = process.env.PORT || 3000;
const JWT_SECRET = process.env.JWT_SECRET || 'nst-secret-key-2024';

// ─── Folder & Database ───────────────────────────────
const UPLOADS_DIR = path.join(__dirname, 'uploads');
if (!fs.existsSync(UPLOADS_DIR)) fs.mkdirSync(UPLOADS_DIR, { recursive: true });

const db = new Database(path.join(__dirname, 'nst.db'));

// ─── Inisialisasi Database ───────────────────────────
db.exec(`
  CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password TEXT NOT NULL
  );

  CREATE TABLE IF NOT EXISTS pemeriksaan (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    id_pasien TEXT NOT NULL,
    nama TEXT NOT NULL,
    usia TEXT NOT NULL,
    usia_kandungan TEXT NOT NULL,
    tanggal TEXT NOT NULL,
    avg_bpm REAL NOT NULL,
    file_path TEXT NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
  );
`);

// Buat admin default jika belum ada
const existingAdmin = db.prepare('SELECT id FROM users WHERE username = ?').get('admin');
if (!existingAdmin) {
  const hash = bcrypt.hashSync('tekmed123', 10);
  db.prepare('INSERT INTO users (username, password) VALUES (?, ?)').run('admin', hash);
  console.log('[DB] Admin default dibuat: id=admin, pw=tekmed123');
}

// ─── Middleware ──────────────────────────────────────
app.use(cors());
app.use(express.json());
app.use(express.static(path.join(__dirname, 'public')));
// Sajikan file audio dengan header yang benar
app.use('/uploads', (req, res, next) => {
  res.setHeader('Accept-Ranges', 'bytes');
  next();
}, express.static(UPLOADS_DIR));

// ─── Multer (upload WAV) ──────────────────────────────
const storage = multer.diskStorage({
  destination: (req, file, cb) => cb(null, UPLOADS_DIR),
  filename: (req, file, cb) => {
    const safeName = file.originalname.replace(/[^a-zA-Z0-9_\-\.]/g, '_');
    // Hindari nama duplikat
    let finalName = safeName;
    let counter = 1;
    while (fs.existsSync(path.join(UPLOADS_DIR, finalName))) {
      const ext = path.extname(safeName);
      const base = path.basename(safeName, ext);
      finalName = `${base}_${counter}${ext}`;
      counter++;
    }
    cb(null, finalName);
  }
});
const upload = multer({ storage, limits: { fileSize: 10 * 1024 * 1024 } }); // max 10MB

// ─── Middleware Auth ──────────────────────────────────
function authMiddleware(req, res, next) {
  const token = req.headers['authorization']?.split(' ')[1];
  if (!token) return res.status(401).json({ error: 'Token tidak ada' });
  try {
    req.user = jwt.verify(token, JWT_SECRET);
    next();
  } catch {
    res.status(401).json({ error: 'Token tidak valid' });
  }
}

// ─── Route: Login ─────────────────────────────────────
app.post('/api/login', (req, res) => {
  const { username, password } = req.body;
  const user = db.prepare('SELECT * FROM users WHERE username = ?').get(username);
  if (!user || !bcrypt.compareSync(password, user.password)) {
    return res.status(401).json({ error: 'ID atau password salah' });
  }
  const token = jwt.sign({ id: user.id, username: user.username }, JWT_SECRET, { expiresIn: '8h' });
  res.json({ token });
});

// ─── Route: Upload dari ESP32 ────────────────────────
app.post('/api/upload', upload.single('file'), (req, res) => {
  try {
    const { nama, usia, idPasien, usiaKandungan, tanggal, avgBpm } = req.body;
    if (!req.file || !nama || !idPasien) {
      return res.status(400).json({ error: 'Data tidak lengkap' });
    }

    db.prepare(`
      INSERT INTO pemeriksaan (id_pasien, nama, usia, usia_kandungan, tanggal, avg_bpm, file_path)
      VALUES (?, ?, ?, ?, ?, ?, ?)
    `).run(idPasien, nama, usia, usiaKandungan, tanggal, parseFloat(avgBpm), req.file.filename);

    console.log(`[Upload] Tersimpan: ${nama} (${idPasien}) - BPM: ${avgBpm}`);
    res.json({ success: true, message: 'Data berhasil disimpan' });
  } catch (err) {
    console.error('[Upload Error]', err);
    res.status(500).json({ error: err.message });
  }
});

// ─── Route: Daftar Pasien (unik) ─────────────────────
app.get('/api/pasien', authMiddleware, (req, res) => {
  const rows = db.prepare(`
    SELECT id_pasien, nama, COUNT(*) as jumlah_pemeriksaan
    FROM pemeriksaan
    GROUP BY id_pasien, nama
    ORDER BY nama
  `).all();
  res.json(rows);
});

// ─── Route: Semua Pemeriksaan 1 Pasien ───────────────
app.get('/api/pasien/:id_pasien', authMiddleware, (req, res) => {
  const rows = db.prepare(`
    SELECT * FROM pemeriksaan
    WHERE id_pasien = ?
    ORDER BY created_at DESC
  `).all(req.params.id_pasien);
  res.json(rows);
});

// ─── Fallback ke index.html (SPA) ────────────────────
app.get('*', (req, res) => {
  res.sendFile(path.join(__dirname, 'public', 'index.html'));
});

app.listen(PORT, () => {
  console.log(`[Server] Berjalan di port ${PORT}`);
});