import os
import uuid
import tempfile

from flask import Flask, render_template, request, jsonify, send_file, after_this_request
import yt_dlp

app = Flask(__name__)

DOWNLOAD_DIR = tempfile.gettempdir()
COOKIES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cookies.txt')


def base_ydl_opts(use_cookies=False):
    """Common yt-dlp options. Cookies are only attached when use_cookies=True,
    so callers can first try a plain (cookie-less) request and only fall back
    to cookies if YouTube blocks the plain request."""
    opts = {
        'quiet': True,
        'no_warnings': True,
        'extractor_args': {
            'youtube': {
                'player_client': ['android', 'web'],
            }
        },
    }
    if use_cookies and os.path.exists(COOKIES_FILE):
        opts['cookiefile'] = COOKIES_FILE
    return opts


def is_bot_check_error(exc):
    """True if the yt-dlp exception looks like YouTube's bot-detection block,
    in which case retrying with cookies is worth attempting."""
    msg = str(exc).lower()
    return 'sign in to confirm' in msg or 'not a bot' in msg


def run_with_fallback(build_opts_fn, action):
    """If a cookies file is present, use it right away (cookies also unlock
    higher-resolution formats, not just bypass the bot check). Only skip
    cookies entirely if no cookies file exists, in which case we still try
    a plain request and surface whatever error comes back."""
    if os.path.exists(COOKIES_FILE):
        try:
            return action(build_opts_fn(use_cookies=True))
        except Exception as e:
            if is_bot_check_error(e):
                raise
            # non bot-check error: try once more without cookies as a fallback
            return action(build_opts_fn(use_cookies=False))
    return action(build_opts_fn(use_cookies=False))


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/debug-cookies')
def debug_cookies():
    """Temporary diagnostic route - tells us whether cookies.txt exists on the
    server and how many cookie lines it has. Safe to remove later."""
    exists = os.path.exists(COOKIES_FILE)
    line_count = None
    if exists:
        with open(COOKIES_FILE, 'r', errors='ignore') as fh:
            line_count = sum(1 for line in fh if line.strip() and not line.startswith('#'))
    return jsonify({
        'cookies_path': COOKIES_FILE,
        'exists': exists,
        'cookie_line_count': line_count,
    })


@app.route('/formats', methods=['POST'])
def get_formats():
    """Given a YouTube URL, return the video's title/thumbnail and a
    de-duplicated list of downloadable resolutions."""
    data = request.get_json(silent=True) or {}
    url = (data.get('url') or '').strip()
    if not url:
        return jsonify({'error': 'Link nahi mila'}), 400

    def do_extract(ydl_opts):
        ydl_opts = dict(ydl_opts)
        ydl_opts['skip_download'] = True
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=False)

    try:
        info = run_with_fallback(base_ydl_opts, do_extract)
    except Exception as e:
        return jsonify({'error': f'Video info nahi mil paayi: {e}'}), 400

    seen = set()
    formats = []
    for f in info.get('formats', []):
        # only care about entries that actually contain a video stream
        if f.get('vcodec') and f.get('vcodec') != 'none':
            height = f.get('height')
            if not height:
                continue
            key = height
            if key in seen:
                continue
            seen.add(key)
            formats.append({
                'format_id': f.get('format_id'),
                'resolution': f'{height}p',
                'height': height,
                'ext': 'mp4',
                'filesize': f.get('filesize') or f.get('filesize_approx'),
            })

    formats.sort(key=lambda x: x['height'], reverse=True)

    # Always offer an audio-only (mp3) option
    formats.append({
        'format_id': 'bestaudio',
        'resolution': 'Audio only (MP3)',
        'height': 0,
        'ext': 'mp3',
        'filesize': None,
    })

    return jsonify({
        'title': info.get('title'),
        'thumbnail': info.get('thumbnail'),
        'duration': info.get('duration'),
        'formats': formats,
    })


@app.route('/download', methods=['POST'])
def download():
    data = request.get_json(silent=True) or {}
    url = (data.get('url') or '').strip()
    format_id = (data.get('format_id') or 'best').strip()

    if not url:
        return jsonify({'error': 'Link nahi mila'}), 400

    unique_id = str(uuid.uuid4())
    out_template = os.path.join(DOWNLOAD_DIR, f'{unique_id}.%(ext)s')
    is_audio = format_id == 'bestaudio'

    def build_opts(use_cookies):
        opts = base_ydl_opts(use_cookies=use_cookies)
        opts['outtmpl'] = out_template
        opts['noplaylist'] = True
        if is_audio:
            opts['format'] = 'bestaudio/best'
            opts['postprocessors'] = [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }]
        else:
            opts['format'] = f'bestvideo[format_id={format_id}]+bestaudio/best'
            opts['merge_output_format'] = 'mp4'
        return opts

    def do_download(ydl_opts):
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            if is_audio:
                filename = os.path.splitext(filename)[0] + '.mp3'
            elif ydl_opts.get('merge_output_format'):
                filename = os.path.splitext(filename)[0] + '.mp4'
            return filename

    try:
        filename = run_with_fallback(build_opts, do_download)
    except Exception as e:
        return jsonify({'error': f'Download fail hua: {e}'}), 400

    if not os.path.exists(filename):
        return jsonify({'error': 'File nahi bani, dobara try karein'}), 500

    download_name = os.path.basename(filename)

    @after_this_request
    def cleanup(response):
        try:
            os.remove(filename)
        except Exception:
            pass
        return response

    return send_file(filename, as_attachment=True, download_name=download_name)


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)