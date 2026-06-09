# DocVec

Local semantic file search untuk Windows: scan file penting, index konten ke SQLite FTS5 + vector backend lokal, lalu cari lewat GUI Explorer-like, CLI debug, atau MCP.

## Quick Start

1. Pastikan dependency Python sudah terpasang dan package `docvec` tersedia di environment saat ini.
2. Pastikan dependency UI sudah terpasang di `src-ui` (`npm install` sudah pernah dijalankan).
3. Double-click `scripts\Start-DocVec-Native.cmd` untuk membuka native GUI.
4. Double-click `scripts\Stop-DocVec.cmd` untuk menghentikan API dan GUI.

Native launcher membuka `src-ui\src-tauri\target\release\docvec-desktop.exe` dan API lokal di `http://127.0.0.1:8765`.
Launcher browser lama tetap tersedia lewat `scripts\Start-DocVec.cmd`; itu membuka UI dalam mode app browser di `http://127.0.0.1:4173`.

## Index Refresh

- Manual `Refresh Index` di GUI menjalankan incremental scan. File unchanged di-skip memakai fingerprint `path + size + mtime_ns`, file baru di-index, file berubah di-index ulang, dan file yang hilang ditandai deleted.
- Scan besar memakai bounded extractor pipeline: beberapa file diekstrak paralel, chunk dikumpulkan, embedding dijalankan batch, lalu SQLite/turbovec ditulis oleh writer tunggal.
- Vector index di-flush berkala, bukan setiap file, supaya disk write `vectors.tvim` tidak menjadi bottleneck. Jika flush gagal, source ditandai `vector_pending` untuk retry.
- Batas ukuran file bersifat per tipe: `.docx`, `.pptx`, dan `.xlsx` sampai `500MB`; `.pdf` sampai `150MB`; text/code umum tetap `20MB`. Office besar tetap aman karena DocVec hanya mengekstrak teks XML, bukan menyimpan gambar/media di dalam dokumen.
- API production menjalankan auto refresh tiap 1 jam selama DocVec API/native app hidup.
- Auto refresh hanya mulai saat Windows melaporkan laptop sedang charging/AC power dan idle minimal 10 menit.
- Auto refresh memakai root `C:\`, `D:\`, `E:\`; policy C: tetap hanya memasukkan AI memory/session penting.

## Production Mode

Default launcher memakai runtime production:

- SQLite DB: `data\docvec.sqlite`
- Vector index: `data\vectors.tvim`
- Embedder: Ollama model `qwen3-embedding:0.6b`
- Ollama API: `http://127.0.0.1:11434`
- Embedding batch size: `32` by default
- Vector backend: turbovec

Untuk semantic search production, jalankan Ollama dan pastikan model embedding tersedia:

```powershell
ollama pull qwen3-embedding:0.6b
```

Untuk throughput lebih tinggi, restart Ollama dengan env ini sebelum scan besar:

```powershell
$env:OLLAMA_NUM_PARALLEL="4"
$env:OLLAMA_MAX_LOADED_MODELS="1"
$env:OLLAMA_FLASH_ATTENTION="1"
ollama serve
```

Kalau Ollama sudah berjalan dari tray/service, restart proses Ollama dulu supaya env di atas benar-benar berlaku. DocVec sendiri memakai batch `32` secara default; kalau VRAM masih longgar, bisa coba naikkan batch:

```powershell
$env:DOCVEC_OLLAMA_BATCH_SIZE="64"
```

Jika terjadi OOM atau Ollama melambat, turunkan ke `32` atau `16`. `DOCVEC_OLLAMA_BATCH_SIZE` mengatur ukuran batch teks yang DocVec kirim ke Ollama; `OLLAMA_NUM_PARALLEL` dan `OLLAMA_FLASH_ATTENTION` mengatur proses Ollama.

Kalau ingin mencoba model lain, override via env:

```powershell
$env:DOCVEC_OLLAMA_MODEL="bge-m3"
$env:DOCVEC_OLLAMA_DIM="1024"
```

Mode fake hanya untuk smoke/dev:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\Start-DocVec.ps1 -Fake
```

## CLI Debug

```powershell
docvec scan --root D:\hermes\brain
docvec search "AI project"
docvec serve
```

## MCP

Entrypoint MCP:

```powershell
docvec-mcp
```

Tools MCP yang tersedia: `search_all`, `search_files`, `search_sessions`, `get_context`, `open_result`, `find_secret_or_config`, `summarize_project`, `saved_searches`, `list_sources`, dan `index_status`.

## Native Tauri

Build native `.exe` butuh Visual Studio Build Tools dengan MSVC + Windows SDK. Cek:

```powershell
cd src-ui
npm run tauri -- info
```

Setelah MSVC Build Tools tersedia:

```powershell
cd src-ui
npm run tauri:build
```

Output build native:

- `src-ui\src-tauri\target\release\docvec-desktop.exe`
- `src-ui\src-tauri\target\release\bundle\nsis\DocVec_0.1.0_x64-setup.exe`
- `src-ui\src-tauri\target\release\bundle\msi\DocVec_0.1.0_x64_en-US.msi`
