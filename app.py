import base64
import io
import os
import re
import tempfile
import zipfile
from functools import wraps

from dotenv import load_dotenv
from flask import Flask, render_template, request, send_file, abort, send_from_directory, session, redirect, url_for
from authlib.integrations.flask_client import OAuth
from PIL import Image, ImageOps
from fpdf import FPDF
import fitz  # PyMuPDF

load_dotenv()  # loads GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / FLASK_SECRET_KEY from a local .env file, if present

app = Flask(__name__)

# Session secret — on Render, set this as an environment variable named
# FLASK_SECRET_KEY (Dashboard -> your service -> Environment). The fallback
# below is only for local testing.
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'dev-only-change-me')

# Max upload size: 50 MB total (adjust if needed)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024

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


@app.context_processor
def inject_user():
    return dict(current_user=session.get('user'))


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
    next_url = session.pop('next_url', '/')
    return redirect(next_url)


@app.route('/logout')
def logout():
    session.pop('user', None)
    return redirect(url_for('index'))

ALLOWED_TYPES = {'image/jpeg', 'image/png', 'image/webp'}
MAX_DIM = 1800  # downscale very large phone photos, same idea as the PHP version

PAGE_SIZES = {
    'A4': (210.0, 297.0),      # width, height in mm (portrait baseline)
    'Letter': (215.9, 279.4),
}


def prepare_image(file_storage):
    """Load, auto-orient, downscale, and flatten an uploaded image.
    Returns a Pillow Image in RGB mode, or None if it can't be read."""
    try:
        img = Image.open(file_storage.stream)
        img = ImageOps.exif_transpose(img)  # respect phone camera orientation
    except Exception:
        return None

    # Flatten any transparency onto white (PDF pages are opaque)
    if img.mode in ('RGBA', 'LA') or (img.mode == 'P' and 'transparency' in img.info):
        img = img.convert('RGBA')
        bg = Image.new('RGB', img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        img = bg
    else:
        img = img.convert('RGB')

    w, h = img.size
    if max(w, h) > MAX_DIM:
        ratio = MAX_DIM / max(w, h)
        img = img.resize((max(1, int(w * ratio)), max(1, int(h * ratio))), Image.LANCZOS)

    return img


def safe_filename(raw, fallback):
    name = re.sub(r'[^A-Za-z0-9 _\-]', '', (raw or '').strip())
    name = name.strip()
    return (name or fallback) + '.pdf'


def resolve_font(doc, page_num, font_label, tmp_files):
    """Try to reuse the PDF's own embedded font so edited text matches the
    original as closely as possible. Falls back to the closest standard
    PDF font (Helvetica / Times / Courier, with bold/italic detected from
    the font's name) when the original isn't embedded or can't be reused."""
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
                    return 'customfont', tmp.name
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
    return mapping.get((base, bold, italic), 'helv'), None


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/robots.txt')
def robots_txt():
    return send_from_directory(os.path.dirname(__file__), 'robots.txt', mimetype='text/plain')


@app.route('/sitemap.xml')
def sitemap_xml():
    return send_from_directory(os.path.dirname(__file__), 'sitemap.xml', mimetype='application/xml')


@app.route('/image-compress.html')
def image_compress():
    return render_template('image_compress.html')


@app.route('/passport-photo.html')
def passport_photo():
    return render_template('passport_photo.html')


@app.route('/image-resizer.html')
def image_resizer():
    return render_template('image_resizer.html')


@app.route('/pdf-to-jpg.html')
def pdf_to_jpg_page():
    return render_template('pdf_to_jpg.html')


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


@app.route('/convert', methods=['POST'])
def convert():
    files = request.files.getlist('images[]')
    if not files or all(f.filename == '' for f in files):
        abort(400, description='No images were received. Please go back and select at least one photo.')

    page_size = request.form.get('page_size', 'A4')
    orientation = request.form.get('orientation', 'P')  # 'P' or 'L'

    pdf = FPDF(orientation=orientation, unit='mm',
               format='A4' if page_size == 'Fit' else page_size)
    pdf.set_auto_page_break(False)

    processed = 0

    for f in files:
        if f.mimetype not in ALLOWED_TYPES:
            continue

        img = prepare_image(f)
        if img is None:
            continue

        px_w, px_h = img.size
        img_w_mm = px_w * 25.4 / 96
        img_h_mm = px_h * 25.4 / 96

        # Write to a temp JPEG so FPDF can embed it
        tmp = tempfile.NamedTemporaryFile(suffix='.jpg', delete=False)
        img.save(tmp.name, 'JPEG', quality=85)
        tmp.close()

        try:
            if page_size == 'Fit':
                max_dim = 1000  # mm cap, mirrors the PHP version
                pw = min(img_w_mm, max_dim)
                ph = min(img_h_mm, max_dim)
                page_orientation = 'L' if img_w_mm >= img_h_mm else 'P'
                pdf.add_page(orientation=page_orientation, format=(pw, ph))
                pdf.image(tmp.name, 0, 0, pw, ph)
            else:
                pdf.add_page(orientation=orientation)
                page_w = pdf.w
                page_h = pdf.h
                ratio = min(page_w / img_w_mm, page_h / img_h_mm)
                draw_w = img_w_mm * ratio
                draw_h = img_h_mm * ratio
                x = (page_w - draw_w) / 2
                y = (page_h - draw_h) / 2
                pdf.image(tmp.name, x, y, draw_w, draw_h)

            processed += 1
        finally:
            os.unlink(tmp.name)

    if processed == 0:
        abort(400, description='None of the uploaded files could be processed. Please try JPG, PNG, or WEBP images.')

    output_name = safe_filename(request.form.get('filename'), 'PDF-ART-Document')
    pdf_bytes = bytes(pdf.output())

    return send_file(
        io.BytesIO(pdf_bytes),
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
                blocks.append({
                    'id': idx,
                    'page': page_num,
                    'text': text,
                    'x0': round(bbox[0], 2), 'y0': round(bbox[1], 2),
                    'x1': round(bbox[2], 2), 'y1': round(bbox[3], 2),
                    'size': round(first.get('size', 11), 2),
                    'font': first.get('font', 'helv'),
                    'color': f"{r},{g},{b}",
                })
                idx += 1

    doc.close()

    if not blocks:
        abort(400, description='No editable text was found in this PDF — it may be a scanned or image-only document.')

    pdf_b64 = base64.b64encode(pdf_bytes).decode('ascii')
    return render_template(
        'pdf_text_edit_result.html',
        blocks=blocks,
        pdf_b64=pdf_b64,
        page_count=page_count,
        truncated=page_count > max_pages,
    )


@app.route('/apply-pdf-edits', methods=['POST'])
@login_required
def apply_pdf_edits():
    pdf_b64 = request.form.get('pdf_data', '')
    try:
        pdf_bytes = base64.b64decode(pdf_b64)
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
                })
            except (ValueError, TypeError):
                continue

    tmp_font_files = []
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

                fontname, fontfile = resolve_font(doc, page_num, e['font'], tmp_font_files)
                baseline_y = e['y1'] - (e['size'] * 0.15)

                try:
                    page.insert_text(
                        fitz.Point(e['x0'], baseline_y),
                        e['text'],
                        fontsize=e['size'],
                        fontname=fontname,
                        fontfile=fontfile,
                        color=(r, g, b),
                    )
                except Exception:
                    page.insert_text(
                        fitz.Point(e['x0'], baseline_y),
                        e['text'],
                        fontsize=e['size'],
                        fontname='helv',
                        color=(r, g, b),
                    )

        out_bytes = doc.tobytes()
    finally:
        doc.close()
        for f in tmp_font_files:
            try:
                os.unlink(f)
            except OSError:
                pass

    output_name = safe_filename(request.form.get('filename'), 'PDF-ART-Edited')
    return send_file(
        io.BytesIO(out_bytes),
        mimetype='application/pdf',
        as_attachment=True,
        download_name=output_name,
    )


@app.errorhandler(400)
def bad_request(e):
    return (e.description or 'Something went wrong.'), 400


@app.errorhandler(503)
def service_unavailable(e):
    return (e.description or 'This feature is temporarily unavailable.'), 503


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=False, host='0.0.0.0', port=port)