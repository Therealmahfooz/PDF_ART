import io
import os
import re
import tempfile
import zipfile

from flask import Flask, render_template, request, send_file, abort
from PIL import Image, ImageOps
from fpdf import FPDF
import fitz  # PyMuPDF

app = Flask(__name__)

# Max upload size: 50 MB total (adjust if needed)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024

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


@app.route('/')
def index():
    return render_template('index.html')


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


@app.errorhandler(400)
def bad_request(e):
    return (e.description or 'Something went wrong.'), 400


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=False, host='0.0.0.0', port=port)