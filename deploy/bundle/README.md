# aitmation – first steps

**Start:** double-click or run `start.cmd` (Windows) or `start.sh`
(macOS/Linux). At the first start

1. the launcher starts the core with the Python that ships in this folder
   (nothing is downloaded, no internet connection is needed),
2. it creates a household with one private mandant — your user name is
   your login name on this computer, the **password is shown once in the
   window** (write it down),
3. it opens the interface in your browser: `http://127.0.0.1:8400/app/`.

Add further mandants (a company, say) under **Administration → Setup**
and further people under **Administration → Users**. **Master data** holds
the profile per mandant: country, name, identifiers (UID, VAT number,
commercial register) — the basis for templates and for the data
protection rules that govern model access.

**Hand in documents:** put files into the inbox folder
(`~/.aitmation/mailroom/inbox` or `%USERPROFILE%\.aitmation\mailroom\inbox`),
or use "Hand in a document" in the interface (file, photo, text,
dictation) — they appear under *To decide*. Mailbox (IMAP), web form token
and the addressees of the entrance are set up under **Administration →
Channels**.

**Models:** under **Administration → Models** you enter the API key (it
is never displayed), approve models, or connect a local model through
Ollama (`https://ollama.com`, then for example `ollama pull llama3.1:8b`).
The table "Allowed up to" shows which data class may go where — depending
on the country in the profile.

**Reading scans and photos (OCR):** install Tesseract — Windows: the
installer from https://github.com/UB-Mannheim/tesseract/wiki (tick the
languages you need); macOS: `brew install tesseract tesseract-lang`;
Linux: `sudo apt install tesseract-ocr tesseract-ocr-deu tesseract-ocr-fra
tesseract-ocr-ita`. If `tesseract` is not on the PATH, set
`AITMATION_TESSERACT_PATH`. Without Tesseract scans stay unread, unless an
approved model may see images — and then only for the data classes it is
allowed to see.

**Voice notes and videos:** are transcribed on your computer, never on
the internet. This needs ffmpeg (Windows: https://www.gyan.dev/ffmpeg/builds/,
macOS `brew install ffmpeg`, Linux `sudo apt install ffmpeg`) and a local
Whisper: either whisper.cpp (`whisper-cli` from
https://github.com/ggml-org/whisper.cpp plus a model file such as
`ggml-small.bin`, path as `whisper_cpp_model` in `config.toml` or in the
environment variable `AITMATION_WHISPER_CPP_MODEL`) or the Python extra
`transcription`. Administration → Status shows what was found. Without
Whisper recordings can be played but are not read.

**Stop:** close the window or press Ctrl+C. **Data:** everything lives in
the data directory (`~/.aitmation`); a backup is a copy of it.

**Forgot the password:** in this folder run
`aitmation-launcher user passwd -name <user>`.

What the system is and is not: https://aitmation.com
