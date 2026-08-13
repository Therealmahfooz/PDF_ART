import io
import os
import re
import sqlite3
import tempfile
import time
import uuid
import zipfile
from datetime import datetime, timezone
from functools import wraps

import cv2
import numpy as np
from dotenv import load_dotenv
from flask import Flask, render_template, request, send_file, abort, send_from_directory, session, redirect, url_for, g, make_response
from authlib.integrations.flask_client import OAuth
import fitz  # PyMuPDF

# --- Compatibility shim for pdf2docx ---
# pdf2docx (and some of its dependencies) still do `from collections import
# Iterable`, which was removed in Python 3.10 (it now only lives in
# collections.abc). Rather than pinning the whole app to an old, soon-to-be
# unsupported Python version just for this one library, we patch the
# `collections` module here — before pdf2docx is imported — so that old
# import style keeps working no matter which Python version Render uses.
import collections
import collections.abc
for _name in ('Iterable', 'Mapping', 'MutableMapping', 'Sequence', 'Callable'):
    if not hasattr(collections, _name):
        setattr(collections, _name, getattr(collections.abc, _name))

from pdf2docx import Converter  # used by the new "PDF to Word/Text" tool

load_dotenv()  # loads GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / FLASK_SECRET_KEY from a local .env file, if present

app = Flask(__name__)

# Session secret — on Render, set this as an environment variable named
# FLASK_SECRET_KEY (Dashboard -> your service -> Environment). The fallback
# below is only for local testing.
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'dev-only-change-me')

# Max upload size: 50 MB total (adjust if needed)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024

# --- Simple built-in analytics (page views + logins) ---
# Stored in a small SQLite file next to app.py. NOTE: on free hosting tiers
# with an ephemeral filesystem (e.g. Render's free web service plan), this
# file is wiped whenever the app redeploys or restarts — fine for a quick
# look at usage, but not for long-term history. For that, point
# ANALYTICS_DB_PATH at a persistent disk, or swap this for a real database.
DB_PATH = os.environ.get('ANALYTICS_DB_PATH', os.path.join(app.root_path, 'analytics.db'))

# Which pages count as "tool usage" in the admin dashboard, and the friendly
# name shown there. Anything not in this dict is not logged.
TOOL_LABELS = {
    '/': 'Home',
    '/image-to-pdf.html': 'Image to PDF',
    '/pdf-to-jpg.html': 'PDF to JPG',
    '/pdf-text-editor.html': 'Edit PDF Text',
    '/protect-pdf.html': 'Protect / Unlock PDF',
    '/image-compress.html': 'Image Compressor',
    '/passport-photo.html': 'Passport Photo',
    '/image-resizer.html': 'Image Resizer',
    '/pdf/merge': 'Merge PDF',
    '/logo-remover.html': 'Logo Remover',
    '/pdf-to-word.html': 'PDF to Word/Text',
}

# Comma-separated list of Google account emails allowed to see /admin.
# Set this as an environment variable, e.g. ADMIN_EMAILS=you@gmail.com
ADMIN_EMAILS = {
    e.strip().lower() for e in os.environ.get('ADMIN_EMAILS', '').split(',') if e.strip()
}


def get_db():
    db = getattr(g, '_analytics_db', None)
    if db is None:
        db = g._analytics_db = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
    return db


@app.teardown_appcontext
def close_db(exception=None):
    db = getattr(g, '_analytics_db', None)
    if db is not None:
        db.close()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute('''CREATE TABLE IF NOT EXISTS page_views (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        path TEXT NOT NULL,
        tool TEXT NOT NULL,
        user_email TEXT,
        user_name TEXT,
        ts TEXT NOT NULL
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS login_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT NOT NULL,
        name TEXT,
        ts TEXT NOT NULL
    )''')
    conn.commit()
    conn.close()


init_db()


@app.before_request
def log_page_view():
    if request.method != 'GET':
        return
    label = TOOL_LABELS.get(request.path)
    if not label:
        return
    user = session.get('user')
    try:
        db = get_db()
        db.execute(
            'INSERT INTO page_views (path, tool, user_email, user_name, ts) VALUES (?, ?, ?, ?, ?)',
            (
                request.path,
                label,
                user.get('email') if user else None,
                user.get('name') if user else None,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        db.commit()
    except Exception:
        pass  # analytics should never break the actual page


# --- Google Sign-In ---
oauth = OAuth(app)
google = oauth.register(
    name='google',
    client_id=os.environ.get('GOOGLE_CLIENT_ID'),
    client_secret=os.environ.get('GOOGLE_CLIENT_SECRET'),
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={'scope': 'openid email profile'},
)


def login_required(view_func):
    """Redirects to Google sign-in if nobody is logged in yet, then sends
    the person back to the page they originally wanted."""
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if not session.get('user'):
            return redirect(url_for('login', next=request.path))
        return view_func(*args, **kwargs)
    return wrapped


def admin_required(view_func):
    """Like login_required, but also checks the signed-in email is in
    ADMIN_EMAILS. Anyone else gets a 403."""
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        user = session.get('user')
        if not user:
            return redirect(url_for('login', next=request.path))
        if not ADMIN_EMAILS or (user.get('email') or '').lower() not in ADMIN_EMAILS:
            abort(403, description="You don't have access to this page.")
        return view_func(*args, **kwargs)
    return wrapped


@app.context_processor
def inject_user():
    user = session.get('user')
    is_admin = bool(user and (user.get('email') or '').lower() in ADMIN_EMAILS)
    return dict(current_user=user, is_admin=is_admin)


@app.route('/login')
def login():
    if not os.environ.get('GOOGLE_CLIENT_ID') or not os.environ.get('GOOGLE_CLIENT_SECRET'):
        abort(503, description='Google Sign-In isn\'t configured on this server yet. Add GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET as environment variables and restart the app.')
    session['next_url'] = request.args.get('next', '/')
    redirect_uri = url_for('auth_callback', _external=True)
    return google.authorize_redirect(redirect_uri)


@app.route('/auth/callback')
def auth_callback():
    token = google.authorize_access_token()
    user_info = token.get('userinfo')
    if user_info:
        session['user'] = {
            'name': user_info.get('name'),
            'email': user_info.get('email'),
            'picture': user_info.get('picture'),
        }
        try:
            db = get_db()
            db.execute(
                'INSERT INTO login_events (email, name, ts) VALUES (?, ?, ?)',
                (user_info.get('email'), user_info.get('name'), datetime.now(timezone.utc).isoformat()),
            )
            db.commit()
        except Exception:
            pass
    next_url = session.pop('next_url', '/')
    return redirect(next_url)


@app.route('/logout')
def logout():
    session.pop('user', None)
    return redirect(url_for('home'))


# --- Temp storage for the PDF while the user edits its text ---
# The uploaded PDF is written here once, and the edit form only carries a
# short token referencing it — NOT the whole file re-encoded as base64.
# Sending the full PDF back and forth as a base64 form field was the cause
# of "Request Entity Too Large": base64 is ~33% bigger than the original,
# and x-www-form-urlencoded encoding of the base64 alphabet (+, /, =) adds
# another ~30-40% on top, so even a modest PDF could blow past the upload
# size limit once it made the round trip.
PDF_EDIT_TMP_DIR = os.path.join(tempfile.gettempdir(), 'pdfart_edits')
os.makedirs(PDF_EDIT_TMP_DIR, exist_ok=True)
PDF_EDIT_TOKEN_RE = re.compile(r'^[a-f0-9]{32}$')
PDF_EDIT_MAX_AGE_SECONDS = 3600  # 1 hour — plenty for someone to finish editing


def _cleanup_old_edit_temp_files():
    try:
        now = time.time()
        for fname in os.listdir(PDF_EDIT_TMP_DIR):
            fpath = os.path.join(PDF_EDIT_TMP_DIR, fname)
            try:
                if now - os.path.getmtime(fpath) > PDF_EDIT_MAX_AGE_SECONDS:
                    os.unlink(fpath)
            except OSError:
                pass
    except OSError:
        pass


def store_pdf_for_editing(pdf_bytes):
    """Writes the uploaded PDF to a temp file and returns a token for it."""
    _cleanup_old_edit_temp_files()
    token = uuid.uuid4().hex
    with open(os.path.join(PDF_EDIT_TMP_DIR, token + '.pdf'), 'wb') as f:
        f.write(pdf_bytes)
    return token


def load_pdf_for_editing(token):
    """Reads back the PDF bytes for a token created by store_pdf_for_editing.
    Returns None if the token is invalid, expired, or unknown."""
    if not token or not PDF_EDIT_TOKEN_RE.match(token):
        return None
    fpath = os.path.join(PDF_EDIT_TMP_DIR, token + '.pdf')
    if not os.path.isfile(fpath):
        return None
    try:
        with open(fpath, 'rb') as f:
            return f.read()
    except OSError:
        return None


def discard_pdf_edit_token(token):
    if not token or not PDF_EDIT_TOKEN_RE.match(token):
        return
    fpath = os.path.join(PDF_EDIT_TMP_DIR, token + '.pdf')
    try:
        os.unlink(fpath)
    except OSError:
        pass


def safe_filename(raw, fallback):
    name = re.sub(r'[^A-Za-z0-9 _\-]', '', (raw or '').strip())
    name = name.strip()
    return (name or fallback) + '.pdf'


def safe_image_filename(raw, fallback, ext='.png'):
    name = re.sub(r'[^A-Za-z0-9 _\-]', '', (raw or '').strip())
    name = name.strip()
    return (name or fallback) + ext


def safe_doc_basename(raw, fallback):
    """Like safe_filename/safe_image_filename, but returns just the
    sanitized base name (no extension) so the caller can append .docx
    or .txt depending on what the user chose."""
    name = re.sub(r'[^A-Za-z0-9 _\-]', '', (raw or '').strip())
    name = name.strip()
    return name or fallback


def resolve_font(doc, page_num, font_label, tmp_files, font_cache=None):
    """Try to reuse the PDF's own embedded font so edited text matches the
    original as closely as possible. Falls back to the closest standard
    PDF font (Helvetica / Times / Courier, with bold/italic detected from
    the font's name) when the original isn't embedded or can't be reused.

    font_cache (optional dict) should be shared across all calls made while
    editing a single document. Each distinct embedded font gets its own
    alias (customfont0, customfont1, ...) so that PyMuPDF doesn't reuse the
    bytes of a previously-registered font when a *different* font is asked
    for under the same name."""
    cache_key = (page_num, font_label)
    if font_cache is not None and cache_key in font_cache:
        return font_cache[cache_key]

    try:
        page = doc[page_num]
        for f in page.get_fonts(full=True):
            xref, basefont = f[0], f[3]
            if basefont == font_label or (font_label and font_label in basefont):
                extracted = doc.extract_font(xref)
                buffer = extracted[3] if len(extracted) > 3 else None
                ext = extracted[1] if len(extracted) > 1 else 'ttf'
                if buffer:
                    tmp = tempfile.NamedTemporaryFile(suffix='.' + (ext or 'ttf'), delete=False)
                    tmp.write(buffer)
                    tmp.close()
                    tmp_files.append(tmp.name)
                    # Unique alias per distinct embedded font so multiple
                    # different fonts on the same page/doc don't collide.
                    alias = f'customfont{len(tmp_files) - 1}'
                    result = (alias, tmp.name)
                    if font_cache is not None:
                        font_cache[cache_key] = result
                    return result
    except Exception:
        pass

    lower = (font_label or '').lower()
    bold = 'bold' in lower
    italic = 'italic' in lower or 'oblique' in lower
    if 'times' in lower or 'serif' in lower or 'georgia' in lower or 'garamond' in lower:
        base = 'times'
    elif 'courier' in lower or 'mono' in lower or 'consol' in lower:
        base = 'courier'
    else:
        base = 'helv'

    mapping = {
        ('helv', False, False): 'helv', ('helv', True, False): 'hebo',
        ('helv', False, True): 'heit', ('helv', True, True): 'hebi',
        ('times', False, False): 'tiro', ('times', True, False): 'tibo',
        ('times', False, True): 'tiit', ('times', True, True): 'tibi',
        ('courier', False, False): 'cour', ('courier', True, False): 'cobo',
        ('courier', False, True): 'coit', ('courier', True, True): 'cobi',
    }
    result = (mapping.get((base, bold, italic), 'helv'), None)
    if font_cache is not None:
        font_cache[cache_key] = result
    return result


@app.route('/')
def home():
    return render_template('home.html')


@app.route('/image-to-pdf.html')
def index():
    return render_template('index.html')


@app.route('/robots.txt')
def robots_txt():
    return send_from_directory(os.path.dirname(__file__), 'robots.txt', mimetype='text/plain')


@app.route('/favicon.ico')
def favicon():
    return send_from_directory(
        os.path.join(app.root_path, 'static', 'icons'),
        'favicon.ico',
        mimetype='image/vnd.microsoft.icon',
    )


@app.route('/sitemap.xml')
def sitemap_xml():
    return send_from_directory(os.path.dirname(__file__), 'sitemap.xml', mimetype='application/xml')


@app.route('/image-compress.html')
def image_compress():
    return render_template('image_compress.html')


@app.route('/passport-photo.html')
def passport_photo():
    resp = make_response(render_template('passport_photo.html'))
    # These headers make the page "cross-origin isolated", which lets the
    # in-browser background-removal model (onnxruntime-web) use
    # multi-threaded WASM and WebGPU instead of a single slow CPU thread.
    # 'credentialless' (rather than 'require-corp') is used for the embedder
    # policy so that third-party assets (Google Fonts, jsDelivr, esm.sh)
    # keep loading normally without needing their own CORP headers.
    resp.headers['Cross-Origin-Opener-Policy'] = 'same-origin'
    resp.headers['Cross-Origin-Embedder-Policy'] = 'credentialless'
    return resp


@app.route('/image-resizer.html')
def image_resizer():
    return render_template('image_resizer.html')


@app.route('/pdf-to-jpg.html')
def pdf_to_jpg_page():
    return render_template('pdf_to_jpg.html')


@app.route('/pdf/merge')
def pdf_merge_page():
    return render_template('pdf_merge.html')


@app.route('/merge-pdfs', methods=['POST'])
def merge_pdfs():
    files = [f for f in request.files.getlist('pdfs') if f and f.filename]
    if len(files) < 2:
        abort(400, description='Please add at least two PDF files to merge.')

    merged = fitz.open()
    try:
        for f in files:
            is_pdf = (f.mimetype == 'application/pdf') or f.filename.lower().endswith('.pdf')
            if not is_pdf:
                abort(400, description=f'"{f.filename}" is not a PDF file. Please remove it and try again.')

            try:
                sub_doc = fitz.open(stream=f.read(), filetype='pdf')
            except Exception:
                abort(400, description=f'"{f.filename}" could not be read. It may be corrupted — please remove it and try again.')

            if sub_doc.needs_pass:
                sub_doc.close()
                abort(400, description=f'"{f.filename}" is password-protected. Please unlock it first using the Protect/Unlock PDF tool, then try merging again.')

            if sub_doc.page_count == 0:
                sub_doc.close()
                abort(400, description=f'"{f.filename}" has no pages.')

            merged.insert_pdf(sub_doc)
            sub_doc.close()

        if merged.page_count == 0:
            abort(400, description='No valid pages were found to merge.')

        out_bytes = merged.tobytes()
    finally:
        merged.close()

    output_name = safe_filename(request.form.get('filename'), 'PDF-ART-Merged')
    return send_file(
        io.BytesIO(out_bytes),
        mimetype='application/pdf',
        as_attachment=True,
        download_name=output_name,
    )


@app.route('/pdf-to-images', methods=['POST'])
def pdf_to_images():
    f = request.files.get('pdf')
    if not f or f.filename == '':
        abort(400, description='No PDF file was received. Please go back and select a PDF.')

    is_pdf = (f.mimetype == 'application/pdf') or f.filename.lower().endswith('.pdf')
    if not is_pdf:
        abort(400, description='Please upload a valid PDF file.')

    fmt = request.form.get('format', 'jpg').lower()
    if fmt not in ('jpg', 'png'):
        fmt = 'jpg'

    try:
        dpi = int(request.form.get('dpi', 150))
    except ValueError:
        dpi = 150
    dpi = max(72, min(dpi, 300))

    try:
        doc = fitz.open(stream=f.read(), filetype='pdf')
    except Exception:
        abort(400, description='This PDF could not be read. It may be corrupted or password-protected.')

    if doc.page_count == 0:
        abort(400, description='This PDF has no pages.')

    base_name = safe_filename(request.form.get('filename'), 'PDF-ART-Pages')[:-4]  # strip the .pdf we added
    ext = 'jpg' if fmt == 'jpg' else 'png'
    pixmap_fmt = 'jpeg' if fmt == 'jpg' else 'png'
    mimetype = 'image/jpeg' if fmt == 'jpg' else 'image/png'

    if doc.page_count == 1:
        pix = doc[0].get_pixmap(dpi=dpi)
        img_bytes = pix.tobytes(pixmap_fmt)
        return send_file(
            io.BytesIO(img_bytes),
            mimetype=mimetype,
            as_attachment=True,
            download_name=f'{base_name}.{ext}',
        )

    mem_zip = io.BytesIO()
    with zipfile.ZipFile(mem_zip, 'w', zipfile.ZIP_DEFLATED) as zf:
        for i, page in enumerate(doc):
            pix = page.get_pixmap(dpi=dpi)
            img_bytes = pix.tobytes(pixmap_fmt)
            zf.writestr(f'{base_name}-page-{i + 1}.{ext}', img_bytes)
    mem_zip.seek(0)

    return send_file(
        mem_zip,
        mimetype='application/zip',
        as_attachment=True,
        download_name=f'{base_name}.zip',
    )


@app.route('/privacy-policy.html')
def privacy_policy():
    return render_template('privacy_policy.html')


@app.route('/terms-of-service.html')
def terms_of_service():
    return render_template('terms_of_service.html')


@app.route('/protect-pdf.html')
@login_required
def protect_pdf_page():
    return render_template('protect_pdf.html')


@app.route('/protect-pdf', methods=['POST'])
@login_required
def protect_pdf():
    f = request.files.get('pdf')
    if not f or f.filename == '':
        abort(400, description='No PDF file was received. Please go back and select a PDF.')

    is_pdf = (f.mimetype == 'application/pdf') or f.filename.lower().endswith('.pdf')
    if not is_pdf:
        abort(400, description='Please upload a valid PDF file.')

    user_pw = request.form.get('user_password', '').strip()
    owner_pw = request.form.get('owner_password', '').strip()
    if not user_pw:
        abort(400, description='Please enter a password to protect this PDF with.')
    if len(user_pw) < 4:
        abort(400, description='Please choose a password with at least 4 characters.')

    try:
        doc = fitz.open(stream=f.read(), filetype='pdf')
    except Exception:
        abort(400, description='This PDF could not be read. It may be corrupted or already password-protected.')

    if doc.page_count == 0:
        doc.close()
        abort(400, description='This PDF has no pages.')

    try:
        out_bytes = doc.tobytes(
            encryption=fitz.PDF_ENCRYPT_AES_256,
            owner_pw=owner_pw or user_pw,
            user_pw=user_pw,
            permissions=int(
                fitz.PDF_PERM_PRINT | fitz.PDF_PERM_COPY
                | fitz.PDF_PERM_ANNOTATE | fitz.PDF_PERM_FORM
            ),
        )
    finally:
        doc.close()

    output_name = safe_filename(request.form.get('filename'), 'PDF-ART-Protected')
    return send_file(
        io.BytesIO(out_bytes),
        mimetype='application/pdf',
        as_attachment=True,
        download_name=output_name,
    )


@app.route('/unlock-pdf', methods=['POST'])
@login_required
def unlock_pdf():
    f = request.files.get('pdf')
    if not f or f.filename == '':
        abort(400, description='No PDF file was received. Please go back and select a PDF.')

    is_pdf = (f.mimetype == 'application/pdf') or f.filename.lower().endswith('.pdf')
    if not is_pdf:
        abort(400, description='Please upload a valid PDF file.')

    password = request.form.get('password', '').strip()

    try:
        doc = fitz.open(stream=f.read(), filetype='pdf')
    except Exception:
        abort(400, description='This PDF could not be read. It may be corrupted.')

    if doc.needs_pass:
        if not password:
            doc.close()
            abort(400, description='This PDF is password-protected. Please enter its password.')
        ok = doc.authenticate(password)
        if not ok:
            doc.close()
            abort(400, description='That password is incorrect. Please try again.')

    try:
        out_bytes = doc.tobytes(encryption=fitz.PDF_ENCRYPT_NONE)
    finally:
        doc.close()

    output_name = safe_filename(request.form.get('filename'), 'PDF-ART-Unlocked')
    return send_file(
        io.BytesIO(out_bytes),
        mimetype='application/pdf',
        as_attachment=True,
        download_name=output_name,
    )


@app.route('/pdf-text-editor.html')
@login_required
def pdf_text_editor_page():
    return render_template('pdf_text_editor.html')


@app.route('/extract-pdf-text', methods=['POST'])
@login_required
def extract_pdf_text():
    file = request.files.get('pdf')
    if not file or file.filename == '':
        abort(400, description='No PDF was received. Please go back and choose a file.')

    pdf_bytes = file.read()
    try:
        doc = fitz.open(stream=pdf_bytes, filetype='pdf')
    except Exception:
        abort(400, description='This file could not be read as a PDF.')

    page_count = len(doc)
    max_pages = min(page_count, 20)  # keep this quick and light on the free-tier server

    blocks = []
    idx = 0
    for page_num in range(max_pages):
        page = doc[page_num]
        text_dict = page.get_text('dict')
        for block in text_dict.get('blocks', []):
            for line in block.get('lines', []):
                spans = line.get('spans', [])
                if not spans:
                    continue
                text = ''.join(s.get('text', '') for s in spans).strip()
                if not text:
                    continue
                first = spans[0]
                bbox = line.get('bbox', first.get('bbox', [0, 0, 0, 0]))
                color_int = first.get('color', 0)
                r = (color_int >> 16) & 255
                g = (color_int >> 8) & 255
                b = color_int & 255

                # Some PDFs (e.g. ID-card / form layouts) print certain
                # fields sideways along a margin. `line['dir']` is the
                # writing-direction unit vector: (1,0) horizontal,
                # (0,-1)/(0,1) vertical, (-1,0) upside-down. We snap it to
                # the nearest 90° so re-inserted text keeps the original
                # line's orientation instead of always being horizontal.
                dx, dy = line.get('dir', (1.0, 0.0))
                if abs(dy) > abs(dx):
                    rotate = 90 if dy < 0 else 270
                else:
                    rotate = 180 if dx < 0 else 0

                blocks.append({
                    'id': idx,
                    'page': page_num,
                    'text': text,
                    'x0': round(bbox[0], 2), 'y0': round(bbox[1], 2),
                    'x1': round(bbox[2], 2), 'y1': round(bbox[3], 2),
                    'size': round(first.get('size', 11), 2),
                    'font': first.get('font', 'helv'),
                    'color': f"{r},{g},{b}",
                    'rotate': rotate,
                })
                idx += 1

    doc.close()

    if not blocks:
        abort(400, description='No editable text was found in this PDF — it may be a scanned or image-only document.')

    pdf_token = store_pdf_for_editing(pdf_bytes)
    return render_template(
        'pdf_text_edit_result.html',
        blocks=blocks,
        pdf_token=pdf_token,
        page_count=page_count,
        truncated=page_count > max_pages,
    )


@app.route('/apply-pdf-edits', methods=['POST'])
@login_required
def apply_pdf_edits():
    pdf_token = request.form.get('pdf_token', '')
    pdf_bytes = load_pdf_for_editing(pdf_token)
    if pdf_bytes is None:
        abort(400, description='The original PDF could not be found — it may have expired (edits must be saved within an hour). Please upload the file again.')
    try:
        doc = fitz.open(stream=pdf_bytes, filetype='pdf')
    except Exception:
        abort(400, description='The original PDF data was lost — please upload the file again.')

    edits_by_page = {}
    for key in request.form:
        if key.startswith('text_'):
            bid = key.split('_', 1)[1]
            try:
                page_num = int(request.form.get(f'page_{bid}', '0'))
                edits_by_page.setdefault(page_num, []).append({
                    'text': request.form.get(key, ''),
                    'x0': float(request.form.get(f'x0_{bid}', '0')),
                    'y0': float(request.form.get(f'y0_{bid}', '0')),
                    'x1': float(request.form.get(f'x1_{bid}', '0')),
                    'y1': float(request.form.get(f'y1_{bid}', '0')),
                    'size': float(request.form.get(f'size_{bid}', '11')),
                    'font': request.form.get(f'font_{bid}', 'helv'),
                    'color': request.form.get(f'color_{bid}', '0,0,0'),
                    'rotate': int(request.form.get(f'rotate_{bid}', '0')) if request.form.get(f'rotate_{bid}', '0') in ('0', '90', '180', '270') else 0,
                })
            except (ValueError, TypeError):
                continue

    tmp_font_files = []
    font_cache = {}
    try:
        for page_num, edits in edits_by_page.items():
            if page_num >= len(doc):
                continue
            page = doc[page_num]

            for e in edits:
                rect = fitz.Rect(e['x0'], e['y0'], e['x1'], e['y1'])
                page.add_redact_annot(rect, fill=(1, 1, 1))
            page.apply_redactions()

            for e in edits:
                if not e['text'].strip():
                    continue
                try:
                    r, g, b = [max(0, min(255, int(c))) / 255 for c in e['color'].split(',')]
                except Exception:
                    r, g, b = 0, 0, 0

                fontname, fontfile = resolve_font(doc, page_num, e['font'], tmp_font_files, font_cache)
                rotate = e.get('rotate', 0)

                # For sideways lines (rotate 90/270) text runs along the
                # box's HEIGHT, not its width — so that's the dimension to
                # fit the text into.
                if rotate in (90, 270):
                    box_span = max(e['y1'] - e['y0'], 1)
                else:
                    box_span = max(e['x1'] - e['x0'], 1)

                # If the new text is longer than the original line's box,
                # shrink the font size just enough to fit (down to a floor)
                # instead of letting it spill over neighbouring text.
                fontsize = e['size']
                min_fontsize = max(e['size'] * 0.5, 5)
                try:
                    for _ in range(12):
                        text_width = fitz.get_text_length(
                            e['text'], fontname=fontname, fontsize=fontsize, fontfile=fontfile
                        )
                        if text_width <= box_span or fontsize <= min_fontsize:
                            break
                        fontsize = max(fontsize * (box_span / text_width) * 0.98, min_fontsize)
                except Exception:
                    fontsize = e['size']

                # The insertion point's meaning depends on rotation: it
                # always sits on the "far" edge of the box in the direction
                # text is written FROM, offset by the font's descent along
                # the perpendicular axis (same offset PyMuPDF itself applies
                # when it writes rotated text).
                offset = fontsize * 0.15
                if rotate == 90:
                    point = fitz.Point(e['x1'] - offset, e['y1'])
                elif rotate == 180:
                    point = fitz.Point(e['x1'], e['y0'] + offset)
                elif rotate == 270:
                    point = fitz.Point(e['x0'] + offset, e['y0'])
                else:
                    point = fitz.Point(e['x0'], e['y1'] - offset)

                try:
                    page.insert_text(
                        point,
                        e['text'],
                        fontsize=fontsize,
                        fontname=fontname,
                        fontfile=fontfile,
                        color=(r, g, b),
                        rotate=rotate,
                    )
                except Exception:
                    page.insert_text(
                        point,
                        e['text'],
                        fontsize=fontsize,
                        fontname='helv',
                        color=(r, g, b),
                        rotate=rotate,
                    )

        out_bytes = doc.tobytes()
    finally:
        doc.close()
        for f in tmp_font_files:
            try:
                os.unlink(f)
            except OSError:
                pass
        discard_pdf_edit_token(pdf_token)

    output_name = safe_filename(request.form.get('filename'), 'PDF-ART-Edited')
    return send_file(
        io.BytesIO(out_bytes),
        mimetype='application/pdf',
        as_attachment=True,
        download_name=output_name,
    )


@app.route('/logo-remover.html')
def logo_remover_page():
    return render_template('logo_remover.html')


@app.route('/remove-logo', methods=['POST'])
def remove_logo():
    img_file = request.files.get('image')
    mask_file = request.files.get('mask')

    if not img_file or img_file.filename == '':
        abort(400, description='No photo was received. Please go back and choose an image.')
    if not mask_file or mask_file.filename == '':
        abort(400, description='No logo area was marked. Please paint over the logo before removing it.')

    is_image = (img_file.mimetype or '').startswith('image/') or re.search(
        r'\.(jpe?g|png|webp|bmp)$', img_file.filename, re.IGNORECASE
    )
    if not is_image:
        abort(400, description='Please upload a valid photo (JPG, PNG, WEBP, or BMP).')

    img_bytes = img_file.read()
    mask_bytes = mask_file.read()

    img_arr = np.frombuffer(img_bytes, np.uint8)
    img = cv2.imdecode(img_arr, cv2.IMREAD_COLOR)
    if img is None:
        abort(400, description='This file could not be read as an image. It may be corrupted or an unsupported format.')

    mask_arr = np.frombuffer(mask_bytes, np.uint8)
    mask = cv2.imdecode(mask_arr, cv2.IMREAD_GRAYSCALE)
    if mask is None:
        abort(400, description='The marked logo area could not be read. Please try marking it again.')

    # The mask is painted on a canvas that may be a scaled-down preview of
    # the photo, so stretch it back up to the photo's real resolution
    # before using it — nearest-neighbour keeps the painted edges crisp
    # instead of blurring them into semi-transparent grey.
    if mask.shape[:2] != img.shape[:2]:
        mask = cv2.resize(mask, (img.shape[1], img.shape[0]), interpolation=cv2.INTER_NEAREST)

    # Anything painted (even lightly, from anti-aliased brush edges) counts
    # as "remove this", then grow the marked area slightly so the very
    # edge of the logo — which the user may not have painted perfectly —
    # gets covered too.
    _, mask_bin = cv2.threshold(mask, 20, 255, cv2.THRESH_BINARY)
    if not np.any(mask_bin):
        abort(400, description='No logo area was marked. Please paint over the logo before removing it.')
    kernel = np.ones((5, 5), np.uint8)
    mask_bin = cv2.dilate(mask_bin, kernel, iterations=2)

    result = cv2.inpaint(img, mask_bin, inpaintRadius=7, flags=cv2.INPAINT_TELEA)

    ok, buffer = cv2.imencode('.png', result)
    if not ok:
        abort(400, description='Something went wrong producing the result image. Please try again.')

    output_name = safe_image_filename(request.form.get('filename'), 'PDF-ART-Logo-Removed', '.png')
    return send_file(
        io.BytesIO(buffer.tobytes()),
        mimetype='image/png',
        as_attachment=True,
        download_name=output_name,
    )


@app.route('/pdf-to-word.html')
def pdf_to_word_page():
    return render_template('pdf_to_word.html')


@app.route('/convert-pdf-to-word', methods=['POST'])
def convert_pdf_to_word():
    f = request.files.get('pdf')
    if not f or f.filename == '':
        abort(400, description='No PDF file was received. Please go back and select a PDF.')

    is_pdf = (f.mimetype == 'application/pdf') or f.filename.lower().endswith('.pdf')
    if not is_pdf:
        abort(400, description='Please upload a valid PDF file.')

    out_format = request.form.get('format', 'docx').lower()
    if out_format not in ('docx', 'txt'):
        out_format = 'docx'

    pdf_bytes = f.read()

    try:
        check_doc = fitz.open(stream=pdf_bytes, filetype='pdf')
    except Exception:
        abort(400, description='This PDF could not be read. It may be corrupted or password-protected.')

    if check_doc.needs_pass:
        check_doc.close()
        abort(400, description='This PDF is password-protected. Please unlock it first using the Protect/Unlock PDF tool, then try again.')

    if check_doc.page_count == 0:
        check_doc.close()
        abort(400, description='This PDF has no pages.')

    base_name = safe_doc_basename(request.form.get('filename'), 'PDF-ART-Converted')

    if out_format == 'txt':
        text_parts = [page.get_text() for page in check_doc]
        check_doc.close()
        text_content = '\n\f\n'.join(text_parts)  # form-feed marks page breaks
        return send_file(
            io.BytesIO(text_content.encode('utf-8')),
            mimetype='text/plain; charset=utf-8',
            as_attachment=True,
            download_name=f'{base_name}.txt',
        )

    check_doc.close()

    # DOCX path: pdf2docx works off real file paths, so the upload is
    # written to a temp .pdf and converted to a temp .docx alongside it.
    tmp_pdf = tempfile.NamedTemporaryFile(suffix='.pdf', delete=False, dir=PDF_EDIT_TMP_DIR)
    tmp_pdf.write(pdf_bytes)
    tmp_pdf.close()
    tmp_docx_path = tmp_pdf.name[:-4] + '.docx'

    try:
        cv = Converter(tmp_pdf.name)
        try:
            cv.convert(tmp_docx_path)
        finally:
            cv.close()
        with open(tmp_docx_path, 'rb') as out_f:
            docx_bytes = out_f.read()
    except Exception:
        abort(400, description='This PDF could not be converted to Word. It may be a scanned/image-only PDF or have a very complex layout — try "Extract as Text" instead.')
    finally:
        for p in (tmp_pdf.name, tmp_docx_path):
            try:
                os.unlink(p)
            except OSError:
                pass

    return send_file(
        io.BytesIO(docx_bytes),
        mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        as_attachment=True,
        download_name=f'{base_name}.docx',
    )


@app.route('/admin')
@admin_required
def admin_dashboard():
    db = get_db()

    total_views = db.execute('SELECT COUNT(*) c FROM page_views').fetchone()['c']
    views_today = db.execute(
        "SELECT COUNT(*) c FROM page_views WHERE date(ts) = date('now')"
    ).fetchone()['c']
    unique_users = db.execute(
        'SELECT COUNT(DISTINCT user_email) c FROM page_views WHERE user_email IS NOT NULL'
    ).fetchone()['c']
    total_logins = db.execute('SELECT COUNT(*) c FROM login_events').fetchone()['c']

    tool_counts = db.execute(
        'SELECT tool, COUNT(*) c FROM page_views GROUP BY tool ORDER BY c DESC'
    ).fetchall()
    max_count = tool_counts[0]['c'] if tool_counts else 1

    recent_logins = db.execute(
        'SELECT email, name, ts FROM login_events ORDER BY ts DESC LIMIT 25'
    ).fetchall()

    recent_views = db.execute(
        'SELECT path, tool, user_email, user_name, ts FROM page_views ORDER BY ts DESC LIMIT 40'
    ).fetchall()

    return render_template(
        'admin.html',
        total_views=total_views,
        views_today=views_today,
        unique_users=unique_users,
        total_logins=total_logins,
        tool_counts=tool_counts,
        max_count=max_count,
        recent_logins=recent_logins,
        recent_views=recent_views,
        admin_configured=bool(ADMIN_EMAILS),
    )


@app.errorhandler(400)
def bad_request(e):
    return (e.description or 'Something went wrong.'), 400


@app.errorhandler(403)
def forbidden(e):
    return (e.description or "You don't have access to this page."), 403


@app.errorhandler(503)
def service_unavailable(e):
    return (e.description or 'This feature is temporarily unavailable.'), 503


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=False, host='0.0.0.0', port=port)