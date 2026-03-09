import sqlite3
import os
from datetime import datetime
import hashlib

DB_PATH = os.path.join(os.path.dirname(__file__), 'blog.db')


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    conn = get_db()
    c = conn.cursor()

    c.executescript('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            full_name TEXT,
            bio TEXT,
            avatar TEXT,
            role TEXT DEFAULT 'author',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            slug TEXT UNIQUE NOT NULL,
            description TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            slug TEXT UNIQUE NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            slug TEXT UNIQUE NOT NULL,
            content TEXT NOT NULL,
            excerpt TEXT,
            featured_image TEXT,
            status TEXT DEFAULT 'draft',
            is_featured INTEGER DEFAULT 0,
            author_id INTEGER REFERENCES users(id),
            views INTEGER DEFAULT 0,
            published_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS post_categories (
            post_id INTEGER REFERENCES posts(id) ON DELETE CASCADE,
            category_id INTEGER REFERENCES categories(id) ON DELETE CASCADE,
            PRIMARY KEY (post_id, category_id)
        );

        CREATE TABLE IF NOT EXISTS post_tags (
            post_id INTEGER REFERENCES posts(id) ON DELETE CASCADE,
            tag_id INTEGER REFERENCES tags(id) ON DELETE CASCADE,
            PRIMARY KEY (post_id, tag_id)
        );

        CREATE TABLE IF NOT EXISTS comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id INTEGER REFERENCES posts(id) ON DELETE CASCADE,
            author_name TEXT NOT NULL,
            author_email TEXT NOT NULL,
            content TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS media (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL,
            original_name TEXT,
            file_size INTEGER,
            mime_type TEXT,
            uploaded_by INTEGER REFERENCES users(id),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS subscribers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS post_likes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
            session_id TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(post_id, session_id)
        );
    ''')

    # Default settings
    defaults = {
        'site_name': 'My Blog',
        'site_description': 'A place for thoughts, ideas and stories.',
        'site_tagline': 'Welcome to My Blog',
        'posts_per_page': '6',
        'allow_comments': '1',
        'footer_text': '© 2024 My Blog. All rights reserved.',
        'social_twitter': '',
        'social_instagram': '',
        'social_linkedin': '',
        'site_logo': '',
    }
    for key, value in defaults.items():
        c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value))

    # Default admin user (password: admin123)
    pw_hash = hashlib.sha256('admin123'.encode()).hexdigest()
    c.execute("""
        INSERT OR IGNORE INTO users (username, email, password_hash, full_name, role)
        VALUES ('admin', 'admin@blog.com', ?, 'Administrator', 'admin')
    """, (pw_hash,))

    # Default categories
    cats = [('Technology', 'technology', 'Tech articles and tutorials'),
            ('Lifestyle', 'lifestyle', 'Life tips and personal stories'),
            ('Travel', 'travel', 'Travel guides and adventures')]
    for name, slug, desc in cats:
        c.execute("INSERT OR IGNORE INTO categories (name, slug, description) VALUES (?, ?, ?)",
                  (name, slug, desc))

    # Sample post
    c.execute("SELECT id FROM posts WHERE slug = 'welcome-to-my-blog'")
    if not c.fetchone():
        content = '''# Welcome to My Blog!

Thank you for visiting my blog. This is your first post, and you can edit or delete it from the admin panel.

## Getting Started

Head over to the **Admin Panel** at `/admin` to:

- **Write new posts** with our markdown editor
- **Manage categories** and tags
- **Upload images** to the media library
- **Customize** your blog settings

## Markdown Support

Your blog supports full **Markdown** formatting:

- *Italic* and **Bold** text
- [Links](https://example.com)
- `Code snippets`
- Blockquotes
- Lists (like this one!)

```python
# Even syntax highlighted code blocks!
def hello():
    print("Hello, World!")
```

> "The best time to start a blog was yesterday. The second best time is now."

Happy blogging! ✨'''

        now = datetime.now().isoformat()
        c.execute("""
            INSERT INTO posts (title, slug, content, excerpt, status, author_id, published_at, created_at)
            VALUES (?, ?, ?, ?, 'published', 1, ?, ?)
        """, ('Welcome to My Blog!', 'welcome-to-my-blog', content,
              'Thank you for visiting my blog. This is your first post — head to the admin panel to get started!',
              now, now))

        post_id = c.lastrowid
        c.execute("INSERT OR IGNORE INTO post_categories VALUES (?, 1)", (post_id,))

    # Migrations for existing databases
    try:
        conn.execute("ALTER TABLE posts ADD COLUMN is_featured INTEGER DEFAULT 0")
    except Exception:
        pass  # column already exists

    conn.commit()
    conn.close()


def get_setting(key, default=''):
    conn = get_db()
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row['value'] if row else default


def get_all_settings():
    conn = get_db()
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    conn.close()
    return {r['key']: r['value'] for r in rows}
