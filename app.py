import os
import hashlib
import re
import uuid
from datetime import datetime
from functools import wraps
from flask import (Flask, render_template, request, redirect, url_for,
                   session, flash, jsonify, abort, send_from_directory)
from werkzeug.utils import secure_filename
from slugify import slugify
import markdown as md

from database import get_db, init_db, get_setting, get_all_settings

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', os.urandom(32).hex())

@app.before_request
def ensure_session_id():
    if 'session_id' not in session:
        session['session_id'] = str(uuid.uuid4())

UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), 'static', 'uploads')
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp', 'svg'}
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# ── Helpers ────────────────────────────────────────────────────────────────

def hash_password(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def render_markdown(text):
    extensions = ['fenced_code', 'codehilite', 'tables', 'toc', 'nl2br']
    try:
        return md.markdown(text, extensions=extensions)
    except Exception:
        return md.markdown(text)

def auto_excerpt(content, length=200):
    plain = re.sub(r'#+ |[*_`\[\]]', '', content)
    plain = re.sub(r'\n+', ' ', plain).strip()
    return plain[:length] + '…' if len(plain) > length else plain

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return decorated

def get_current_user():
    if 'user_id' not in session:
        return None
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (session['user_id'],)).fetchone()
    conn.close()
    return user

@app.context_processor
def inject_globals():
    settings = get_all_settings()
    conn = get_db()
    cats = conn.execute("""
        SELECT c.*, COUNT(pc.post_id) as post_count
        FROM categories c
        LEFT JOIN post_categories pc ON c.id = pc.category_id
        LEFT JOIN posts p ON pc.post_id = p.id AND p.status = 'published'
        GROUP BY c.id ORDER BY c.name
    """).fetchall()
    conn.close()
    return dict(settings=settings, nav_categories=cats, current_user=get_current_user())

# ── Blog Routes ────────────────────────────────────────────────────────────

@app.route('/')
def index():
    # Show subscribe page unless user is logged in or has subscribed
    if 'user_id' not in session and not session.get('subscribed'):
        return render_template('subscribe.html')

    conn = get_db()
    posts = conn.execute("""
        SELECT p.*, u.full_name as author_name, u.username as author_username,
               u.avatar as author_avatar
        FROM posts p LEFT JOIN users u ON p.author_id = u.id
        WHERE p.status = 'published'
        ORDER BY p.is_featured DESC, p.published_at DESC
    """).fetchall()

    session_id = session.get('session_id', '')
    enriched = []
    for post in posts:
        cats = conn.execute("""
            SELECT c.* FROM categories c
            JOIN post_categories pc ON c.id = pc.category_id
            WHERE pc.post_id = ?
        """, (post['id'],)).fetchall()
        like_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM post_likes WHERE post_id = ?", (post['id'],)
        ).fetchone()['cnt']
        user_liked = bool(conn.execute(
            "SELECT 1 FROM post_likes WHERE post_id = ? AND session_id = ?",
            (post['id'], session_id)
        ).fetchone())
        enriched.append({'post': post, 'categories': cats,
                         'like_count': like_count, 'user_liked': user_liked})

    conn.close()
    return render_template('index.html', posts=enriched)


@app.route('/subscribe', methods=['POST'])
def subscribe():
    email = request.form.get('email', '').strip()
    if email:
        try:
            conn = get_db()
            conn.execute("INSERT OR IGNORE INTO subscribers (email) VALUES (?)", (email,))
            conn.commit()
            conn.close()
        except Exception:
            pass
        session['subscribed'] = True
    return redirect(url_for('index'))


@app.route('/post/<slug>')
def post(slug):
    conn = get_db()
    post = conn.execute("""
        SELECT p.*, u.full_name as author_name, u.username as author_username,
               u.bio as author_bio, u.avatar as author_avatar
        FROM posts p LEFT JOIN users u ON p.author_id = u.id
        WHERE p.slug = ? AND p.status = 'published'
    """, (slug,)).fetchone()
    if not post:
        abort(404)

    conn.execute("UPDATE posts SET views = views + 1 WHERE id = ?", (post['id'],))
    conn.commit()

    cats = conn.execute("""
        SELECT c.* FROM categories c
        JOIN post_categories pc ON c.id = pc.category_id
        WHERE pc.post_id = ?
    """, (post['id'],)).fetchall()

    tags = conn.execute("""
        SELECT t.* FROM tags t
        JOIN post_tags pt ON t.id = pt.tag_id
        WHERE pt.post_id = ?
    """, (post['id'],)).fetchall()

    comments = conn.execute("""
        SELECT * FROM comments WHERE post_id = ? AND status = 'approved'
        ORDER BY created_at ASC
    """, (post['id'],)).fetchall()

    related = conn.execute("""
        SELECT DISTINCT p2.*, u.full_name as author_name
        FROM posts p2
        JOIN post_categories pc ON p2.id = pc.post_id
        JOIN post_categories pc2 ON pc.category_id = pc2.category_id AND pc2.post_id = ?
        LEFT JOIN users u ON p2.author_id = u.id
        WHERE p2.id != ? AND p2.status = 'published'
        ORDER BY p2.published_at DESC LIMIT 3
    """, (post['id'], post['id'])).fetchall()

    conn.close()
    html_content = render_markdown(post['content'])
    return render_template('post.html', post=post, html_content=html_content,
                           categories=cats, tags=tags, comments=comments, related=related)


@app.route('/category/<slug>')
def category(slug):
    page = request.args.get('page', 1, type=int)
    per_page = int(get_setting('posts_per_page', '6'))
    conn = get_db()
    cat = conn.execute("SELECT * FROM categories WHERE slug = ?", (slug,)).fetchone()
    if not cat:
        abort(404)
    total = conn.execute("""
        SELECT COUNT(*) as n FROM posts p
        JOIN post_categories pc ON p.id = pc.post_id
        WHERE pc.category_id = ? AND p.status = 'published'
    """, (cat['id'],)).fetchone()['n']
    posts = conn.execute("""
        SELECT p.*, u.full_name as author_name FROM posts p
        JOIN post_categories pc ON p.id = pc.post_id
        LEFT JOIN users u ON p.author_id = u.id
        WHERE pc.category_id = ? AND p.status = 'published'
        ORDER BY p.published_at DESC LIMIT ? OFFSET ?
    """, (cat['id'], per_page, (page - 1) * per_page)).fetchall()
    conn.close()
    pages = (total + per_page - 1) // per_page
    return render_template('category.html', category=cat, posts=posts,
                           page=page, pages=pages, total=total)


@app.route('/tag/<slug>')
def tag(slug):
    page = request.args.get('page', 1, type=int)
    per_page = int(get_setting('posts_per_page', '6'))
    conn = get_db()
    tag = conn.execute("SELECT * FROM tags WHERE slug = ?", (slug,)).fetchone()
    if not tag:
        abort(404)
    total = conn.execute("""
        SELECT COUNT(*) as n FROM posts p
        JOIN post_tags pt ON p.id = pt.post_id
        WHERE pt.tag_id = ? AND p.status = 'published'
    """, (tag['id'],)).fetchone()['n']
    posts = conn.execute("""
        SELECT p.*, u.full_name as author_name FROM posts p
        JOIN post_tags pt ON p.id = pt.post_id
        LEFT JOIN users u ON p.author_id = u.id
        WHERE pt.tag_id = ? AND p.status = 'published'
        ORDER BY p.published_at DESC LIMIT ? OFFSET ?
    """, (tag['id'], per_page, (page - 1) * per_page)).fetchall()
    conn.close()
    pages = (total + per_page - 1) // per_page
    return render_template('tag.html', tag=tag, posts=posts,
                           page=page, pages=pages, total=total)


@app.route('/search')
def search():
    q = request.args.get('q', '').strip()
    posts = []
    if q:
        conn = get_db()
        posts = conn.execute("""
            SELECT p.*, u.full_name as author_name FROM posts p
            LEFT JOIN users u ON p.author_id = u.id
            WHERE p.status = 'published'
              AND (p.title LIKE ? OR p.content LIKE ? OR p.excerpt LIKE ?)
            ORDER BY p.published_at DESC LIMIT 20
        """, (f'%{q}%', f'%{q}%', f'%{q}%')).fetchall()
        conn.close()
    return render_template('search.html', posts=posts, query=q)


@app.route('/post/<int:post_id>/comment', methods=['POST'])
def add_comment(post_id):
    name = request.form.get('name', '').strip()
    email = request.form.get('email', '').strip()
    content = request.form.get('content', '').strip()
    if name and email and content:
        conn = get_db()
        conn.execute(
            "INSERT INTO comments (post_id, author_name, author_email, content, status) VALUES (?, ?, ?, ?, 'approved')",
            (post_id, name, email, content)
        )
        conn.commit()
        conn.close()
        flash('Comment posted!', 'success')
    else:
        flash('All fields are required.', 'error')
    post = get_db().execute("SELECT slug FROM posts WHERE id = ?", (post_id,)).fetchone()
    return redirect(url_for('post', slug=post['slug']) + '#comments')


@app.route('/like/<int:post_id>', methods=['POST'])
def like_post(post_id):
    session_id = session.get('session_id', str(uuid.uuid4()))
    session['session_id'] = session_id
    conn = get_db()
    existing = conn.execute(
        "SELECT 1 FROM post_likes WHERE post_id = ? AND session_id = ?",
        (post_id, session_id)
    ).fetchone()
    if existing:
        conn.execute("DELETE FROM post_likes WHERE post_id = ? AND session_id = ?",
                     (post_id, session_id))
        liked = False
    else:
        conn.execute("INSERT OR IGNORE INTO post_likes (post_id, session_id) VALUES (?, ?)",
                     (post_id, session_id))
        liked = True
    conn.commit()
    like_count = conn.execute(
        "SELECT COUNT(*) as cnt FROM post_likes WHERE post_id = ?", (post_id,)
    ).fetchone()['cnt']
    conn.close()
    return jsonify({'liked': liked, 'like_count': like_count})


# ── Admin Auth ─────────────────────────────────────────────────────────────

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if 'user_id' in session:
        return redirect(url_for('admin_dashboard'))
    error = None
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        conn = get_db()
        user = conn.execute(
            "SELECT * FROM users WHERE username = ? OR email = ?", (username, username)
        ).fetchone()
        conn.close()
        if user and user['password_hash'] == hash_password(password):
            session['user_id'] = user['id']
            session['username'] = user['username']
            flash(f'Welcome back, {user["full_name"] or user["username"]}!', 'success')
            return redirect(url_for('admin_dashboard'))
        error = 'Invalid username or password.'
    return render_template('admin/login.html', error=error)


@app.route('/admin/logout')
def admin_logout():
    session.clear()
    return redirect(url_for('index'))


# ── Admin Dashboard ────────────────────────────────────────────────────────

@app.route('/admin')
@app.route('/admin/')
@login_required
def admin_dashboard():
    conn = get_db()
    stats = {
        'total_posts': conn.execute("SELECT COUNT(*) as n FROM posts").fetchone()['n'],
        'published_posts': conn.execute("SELECT COUNT(*) as n FROM posts WHERE status='published'").fetchone()['n'],
        'draft_posts': conn.execute("SELECT COUNT(*) as n FROM posts WHERE status='draft'").fetchone()['n'],
        'total_categories': conn.execute("SELECT COUNT(*) as n FROM categories").fetchone()['n'],
        'total_tags': conn.execute("SELECT COUNT(*) as n FROM tags").fetchone()['n'],
        'total_comments': conn.execute("SELECT COUNT(*) as n FROM comments").fetchone()['n'],
        'pending_comments': conn.execute("SELECT COUNT(*) as n FROM comments WHERE status='pending'").fetchone()['n'],
        'total_views': conn.execute("SELECT COALESCE(SUM(views),0) as n FROM posts").fetchone()['n'],
    }
    recent_posts = conn.execute("""
        SELECT p.*, u.full_name as author_name FROM posts p
        LEFT JOIN users u ON p.author_id = u.id
        ORDER BY p.created_at DESC LIMIT 5
    """).fetchall()
    recent_comments = conn.execute("""
        SELECT c.*, p.title as post_title, p.slug as post_slug FROM comments c
        JOIN posts p ON c.post_id = p.id
        ORDER BY c.created_at DESC LIMIT 5
    """).fetchall()
    top_posts = conn.execute("""
        SELECT * FROM posts WHERE status='published'
        ORDER BY views DESC LIMIT 5
    """).fetchall()
    conn.close()
    return render_template('admin/dashboard.html', stats=stats,
                           recent_posts=recent_posts, recent_comments=recent_comments,
                           top_posts=top_posts)


# ── Admin Posts ────────────────────────────────────────────────────────────

@app.route('/admin/posts')
@login_required
def admin_posts():
    status_filter = request.args.get('status', '')
    search = request.args.get('q', '')
    page = request.args.get('page', 1, type=int)
    per_page = 15
    conn = get_db()

    where = []
    params = []
    if status_filter:
        where.append("p.status = ?")
        params.append(status_filter)
    if search:
        where.append("(p.title LIKE ? OR p.excerpt LIKE ?)")
        params.extend([f'%{search}%', f'%{search}%'])

    where_sql = 'WHERE ' + ' AND '.join(where) if where else ''
    total = conn.execute(
        f"SELECT COUNT(*) as n FROM posts p {where_sql}", params
    ).fetchone()['n']
    posts = conn.execute(f"""
        SELECT p.*, u.full_name as author_name FROM posts p
        LEFT JOIN users u ON p.author_id = u.id
        {where_sql}
        ORDER BY p.created_at DESC LIMIT ? OFFSET ?
    """, params + [per_page, (page - 1) * per_page]).fetchall()
    conn.close()
    pages = (total + per_page - 1) // per_page
    return render_template('admin/posts/list.html', posts=posts, page=page, pages=pages,
                           total=total, status_filter=status_filter, search=search)


@app.route('/admin/posts/new', methods=['GET', 'POST'])
@login_required
def admin_post_new():
    conn = get_db()
    categories = conn.execute("SELECT * FROM categories ORDER BY name").fetchall()
    tags = conn.execute("SELECT * FROM tags ORDER BY name").fetchall()
    conn.close()

    if request.method == 'POST':
        return _save_post(None)
    return render_template('admin/posts/edit.html', post=None,
                           categories=categories, tags=tags,
                           selected_cats=[], selected_tags=[])


@app.route('/admin/posts/<int:post_id>/edit', methods=['GET', 'POST'])
@login_required
def admin_post_edit(post_id):
    conn = get_db()
    post = conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
    if not post:
        abort(404)
    categories = conn.execute("SELECT * FROM categories ORDER BY name").fetchall()
    tags = conn.execute("SELECT * FROM tags ORDER BY name").fetchall()
    selected_cats = [r['category_id'] for r in conn.execute(
        "SELECT category_id FROM post_categories WHERE post_id = ?", (post_id,)).fetchall()]
    selected_tags = [r['tag_id'] for r in conn.execute(
        "SELECT tag_id FROM post_tags WHERE post_id = ?", (post_id,)).fetchall()]
    conn.close()

    if request.method == 'POST':
        return _save_post(post_id)
    return render_template('admin/posts/edit.html', post=post,
                           categories=categories, tags=tags,
                           selected_cats=selected_cats, selected_tags=selected_tags)


def _save_post(post_id):
    title = request.form.get('title', '').strip()
    content = request.form.get('content', '').strip()
    excerpt = request.form.get('excerpt', '').strip() or auto_excerpt(content)
    status = request.form.get('status', 'draft')
    custom_slug = request.form.get('slug', '').strip()
    cat_ids = request.form.getlist('categories')
    tag_names = [t.strip() for t in request.form.get('tags_input', '').split(',') if t.strip()]
    featured_image = request.form.get('featured_image', '').strip()

    if not title or not content:
        flash('Title and content are required.', 'error')
        return redirect(request.url)

    slug = custom_slug or slugify(title)
    now = datetime.now().isoformat()
    conn = get_db()

    if post_id:
        conn.execute("""
            UPDATE posts SET title=?, slug=?, content=?, excerpt=?, status=?,
            featured_image=?, updated_at=?, published_at=CASE WHEN status!='published' AND ?='published' THEN ? ELSE published_at END
            WHERE id=?
        """, (title, slug, content, excerpt, status, featured_image or None, now, status, now, post_id))
        conn.execute("DELETE FROM post_categories WHERE post_id=?", (post_id,))
        conn.execute("DELETE FROM post_tags WHERE post_id=?", (post_id,))
    else:
        published_at = now if status == 'published' else None
        conn.execute("""
            INSERT INTO posts (title, slug, content, excerpt, status, featured_image,
                               author_id, published_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (title, slug, content, excerpt, status, featured_image or None,
              session['user_id'], published_at, now, now))
        post_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    for cat_id in cat_ids:
        conn.execute("INSERT OR IGNORE INTO post_categories VALUES (?, ?)", (post_id, cat_id))

    for tag_name in tag_names:
        tag_slug = slugify(tag_name)
        conn.execute("INSERT OR IGNORE INTO tags (name, slug) VALUES (?, ?)", (tag_name, tag_slug))
        tag = conn.execute("SELECT id FROM tags WHERE slug = ?", (tag_slug,)).fetchone()
        conn.execute("INSERT OR IGNORE INTO post_tags VALUES (?, ?)", (post_id, tag['id']))

    conn.commit()
    conn.close()
    flash('Post saved successfully!', 'success')
    return redirect(url_for('admin_posts'))


@app.route('/admin/posts/<int:post_id>/delete', methods=['POST'])
@login_required
def admin_post_delete(post_id):
    conn = get_db()
    conn.execute("DELETE FROM posts WHERE id = ?", (post_id,))
    conn.commit()
    conn.close()
    flash('Post deleted.', 'success')
    return redirect(url_for('admin_posts'))


@app.route('/admin/posts/<int:post_id>/toggle-status', methods=['POST'])
@login_required
def admin_post_toggle(post_id):
    conn = get_db()
    post = conn.execute("SELECT status FROM posts WHERE id = ?", (post_id,)).fetchone()
    new_status = 'draft' if post['status'] == 'published' else 'published'
    published_at = datetime.now().isoformat() if new_status == 'published' else None
    conn.execute("UPDATE posts SET status=?, published_at=COALESCE(published_at, ?) WHERE id=?",
                 (new_status, published_at, post_id))
    conn.commit()
    conn.close()
    return jsonify({'status': new_status})


@app.route('/admin/posts/<int:post_id>/toggle-featured', methods=['POST'])
@login_required
def admin_post_toggle_featured(post_id):
    conn = get_db()
    post = conn.execute("SELECT is_featured FROM posts WHERE id = ?", (post_id,)).fetchone()
    new_featured = 0 if post['is_featured'] else 1
    if new_featured:
        # Only one post can be featured at a time
        conn.execute("UPDATE posts SET is_featured = 0")
    conn.execute("UPDATE posts SET is_featured = ? WHERE id = ?", (new_featured, post_id))
    conn.commit()
    conn.close()
    return jsonify({'is_featured': bool(new_featured)})


# ── Admin Categories ───────────────────────────────────────────────────────

@app.route('/admin/categories', methods=['GET', 'POST'])
@login_required
def admin_categories():
    conn = get_db()
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        description = request.form.get('description', '').strip()
        if name:
            cat_slug = slugify(name)
            try:
                conn.execute("INSERT INTO categories (name, slug, description) VALUES (?, ?, ?)",
                             (name, cat_slug, description))
                conn.commit()
                flash('Category created!', 'success')
            except Exception:
                flash('Category name or slug already exists.', 'error')
        return redirect(url_for('admin_categories'))

    cats = conn.execute("""
        SELECT c.*, COUNT(pc.post_id) as post_count FROM categories c
        LEFT JOIN post_categories pc ON c.id = pc.category_id
        GROUP BY c.id ORDER BY c.name
    """).fetchall()
    conn.close()
    return render_template('admin/categories/list.html', categories=cats)


@app.route('/admin/categories/<int:cat_id>/edit', methods=['POST'])
@login_required
def admin_category_edit(cat_id):
    name = request.form.get('name', '').strip()
    description = request.form.get('description', '').strip()
    if name:
        conn = get_db()
        conn.execute("UPDATE categories SET name=?, description=? WHERE id=?",
                     (name, description, cat_id))
        conn.commit()
        conn.close()
        flash('Category updated.', 'success')
    return redirect(url_for('admin_categories'))


@app.route('/admin/categories/<int:cat_id>/delete', methods=['POST'])
@login_required
def admin_category_delete(cat_id):
    conn = get_db()
    conn.execute("DELETE FROM categories WHERE id = ?", (cat_id,))
    conn.commit()
    conn.close()
    flash('Category deleted.', 'success')
    return redirect(url_for('admin_categories'))


# ── Admin Tags ─────────────────────────────────────────────────────────────

@app.route('/admin/tags')
@login_required
def admin_tags():
    conn = get_db()
    tags = conn.execute("""
        SELECT t.*, COUNT(pt.post_id) as post_count FROM tags t
        LEFT JOIN post_tags pt ON t.id = pt.tag_id
        GROUP BY t.id ORDER BY t.name
    """).fetchall()
    conn.close()
    return render_template('admin/tags/list.html', tags=tags)


@app.route('/admin/tags/<int:tag_id>/delete', methods=['POST'])
@login_required
def admin_tag_delete(tag_id):
    conn = get_db()
    conn.execute("DELETE FROM tags WHERE id = ?", (tag_id,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


# ── Admin Comments ─────────────────────────────────────────────────────────

@app.route('/admin/comments')
@login_required
def admin_comments():
    conn = get_db()
    comments = conn.execute("""
        SELECT c.*, p.title as post_title, p.slug as post_slug FROM comments c
        JOIN posts p ON c.post_id = p.id
        ORDER BY c.created_at DESC
    """).fetchall()
    conn.close()
    return render_template('admin/comments.html', comments=comments)


@app.route('/admin/comments/<int:comment_id>/approve', methods=['POST'])
@login_required
def admin_comment_approve(comment_id):
    conn = get_db()
    conn.execute("UPDATE comments SET status='approved' WHERE id=?", (comment_id,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/admin/comments/<int:comment_id>/delete', methods=['POST'])
@login_required
def admin_comment_delete(comment_id):
    conn = get_db()
    conn.execute("DELETE FROM comments WHERE id=?", (comment_id,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


# ── Admin Media ────────────────────────────────────────────────────────────

@app.route('/admin/media')
@login_required
def admin_media():
    conn = get_db()
    media = conn.execute("""
        SELECT m.*, u.full_name as uploader FROM media m
        LEFT JOIN users u ON m.uploaded_by = u.id
        ORDER BY m.created_at DESC
    """).fetchall()
    conn.close()
    return render_template('admin/media/list.html', media=media)


@app.route('/admin/media/upload', methods=['POST'])
@login_required
def admin_media_upload():
    files = request.files.getlist('files')
    uploaded = []
    for file in files:
        if file and allowed_file(file.filename):
            ext = file.filename.rsplit('.', 1)[1].lower()
            filename = f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{secure_filename(file.filename)}"
            filepath = os.path.join(UPLOAD_FOLDER, filename)
            file.save(filepath)
            size = os.path.getsize(filepath)
            conn = get_db()
            conn.execute("""
                INSERT INTO media (filename, original_name, file_size, mime_type, uploaded_by)
                VALUES (?, ?, ?, ?, ?)
            """, (filename, file.filename, size, f'image/{ext}', session['user_id']))
            conn.commit()
            conn.close()
            uploaded.append({'filename': filename, 'url': f'/static/uploads/{filename}'})
    return jsonify({'uploaded': uploaded})


@app.route('/admin/media/<int:media_id>/delete', methods=['POST'])
@login_required
def admin_media_delete(media_id):
    conn = get_db()
    m = conn.execute("SELECT filename FROM media WHERE id=?", (media_id,)).fetchone()
    if m:
        filepath = os.path.join(UPLOAD_FOLDER, m['filename'])
        if os.path.exists(filepath):
            os.remove(filepath)
        conn.execute("DELETE FROM media WHERE id=?", (media_id,))
        conn.commit()
    conn.close()
    return jsonify({'ok': True})


# ── Admin Settings ─────────────────────────────────────────────────────────

@app.route('/admin/settings', methods=['GET', 'POST'])
@login_required
def admin_settings():
    if request.method == 'POST':
        keys = ['site_name', 'site_description', 'site_tagline', 'posts_per_page',
                'allow_comments', 'footer_text', 'social_twitter', 'social_instagram', 'social_linkedin']
        conn = get_db()
        for key in keys:
            value = request.form.get(key, '')
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))

        # Handle logo upload
        logo_file = request.files.get('site_logo')
        if logo_file and logo_file.filename and allowed_file(logo_file.filename):
            ext = logo_file.filename.rsplit('.', 1)[1].lower()
            filename = f"logo.{ext}"
            logo_file.save(os.path.join(UPLOAD_FOLDER, filename))
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('site_logo', ?)", (filename,))

        conn.commit()
        conn.close()
        flash('Settings saved!', 'success')
        return redirect(url_for('admin_settings'))
    settings = get_all_settings()
    return render_template('admin/settings/index.html', settings=settings)


# ── Admin Profile ──────────────────────────────────────────────────────────

@app.route('/admin/profile', methods=['GET', 'POST'])
@login_required
def admin_profile():
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id=?", (session['user_id'],)).fetchone()
    if request.method == 'POST':
        full_name = request.form.get('full_name', '').strip()
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip()
        bio = request.form.get('bio', '').strip()
        new_pw = request.form.get('new_password', '')
        confirm_pw = request.form.get('confirm_password', '')

        # Check username uniqueness (exclude current user)
        if username:
            taken = conn.execute(
                "SELECT id FROM users WHERE username=? AND id!=?",
                (username, session['user_id'])
            ).fetchone()
            if taken:
                flash('That username is already taken.', 'error')
                conn.close()
                return redirect(url_for('admin_profile'))

        if new_pw:
            if new_pw != confirm_pw:
                flash('Passwords do not match.', 'error')
                conn.close()
                return redirect(url_for('admin_profile'))
            conn.execute("UPDATE users SET password_hash=? WHERE id=?",
                         (hash_password(new_pw), session['user_id']))

        # Handle avatar upload
        new_avatar = user['avatar']
        avatar_file = request.files.get('avatar')
        if avatar_file and avatar_file.filename and allowed_file(avatar_file.filename):
            ext = avatar_file.filename.rsplit('.', 1)[1].lower()
            filename = f"avatar_{session['user_id']}.{ext}"
            avatar_file.save(os.path.join(UPLOAD_FOLDER, filename))
            new_avatar = filename

        conn.execute("UPDATE users SET full_name=?, username=?, email=?, bio=?, avatar=? WHERE id=?",
                     (full_name, username or user['username'], email, bio, new_avatar, session['user_id']))
        if username:
            session['username'] = username
        conn.commit()
        flash('Profile updated!', 'success')
        conn.close()
        return redirect(url_for('admin_profile'))

    conn.close()
    return render_template('admin/profile.html', user=user)


# ── API: slug preview ──────────────────────────────────────────────────────

@app.route('/admin/api/slug')
@login_required
def api_slug():
    title = request.args.get('title', '')
    return jsonify({'slug': slugify(title)})


@app.route('/admin/api/preview', methods=['POST'])
@login_required
def api_preview():
    data = request.get_json()
    content = data.get('content', '')
    html = render_markdown(content)
    return jsonify({'html': html})


# ── Uploads static ─────────────────────────────────────────────────────────

@app.route('/uploads/<path:filename>')
def uploaded_file(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)


# ── Error pages ────────────────────────────────────────────────────────────

@app.errorhandler(404)
def not_found(e):
    return render_template('404.html'), 404


if __name__ == '__main__':
    init_db()
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'
    app.run(debug=debug, host='0.0.0.0', port=port)
