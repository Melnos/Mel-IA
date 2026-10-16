import os
import re
import time
import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger("mel_ia.db")

try:
    import psycopg2
    import psycopg2.extras
    import psycopg2.pool
    HAS_PG = True
    PG_IntegrityError = psycopg2.IntegrityError
except ImportError:
    HAS_PG = False
    class PG_IntegrityError(Exception):
        pass

# Exception unifiée pour les contraintes d'intégrité (ex: nom d'utilisateur déjà pris)
DBIntegrityError = (sqlite3.IntegrityError, PG_IntegrityError)

from dotenv import load_dotenv

BASE = Path(__file__).parent
load_dotenv(BASE / ".env")
SQLITE_PATH = BASE / "data" / "mel_ia.db"
SQLITE_PATH.parent.mkdir(exist_ok=True)

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

# Détection et normalisation de l'URL Neon / PostgreSQL
IS_POSTGRES = False
PG_DSN = ""
_pg_pool = None

if DATABASE_URL and HAS_PG:
    dsn = DATABASE_URL
    if dsn.startswith("postgres://"):
        dsn = dsn.replace("postgres://", "postgresql://", 1)
    # Nettoyer channel_binding si présent (peut bloquer le handshake SSL sous Windows libpq)
    dsn = re.sub(r"[&?]channel_binding=[^&]*", "", dsn)
    # Assurer sslmode=require si c'est Neon ou un hôte distant sans paramètre ssl
    if "sslmode=" not in dsn and ("neon.tech" in dsn or not dsn.startswith("postgresql://localhost")):
        sep = "&" if "?" in dsn else "?"
        dsn = f"{dsn}{sep}sslmode=require"
    PG_DSN = dsn

    # Boucle de tentative : Neon suspend ses instances gratuites, le réveil peut prendre ~10-15s
    for attempt in range(1, 4):
        try:
            logger.info(f"Connexion à Neon PostgreSQL en cours... (essai {attempt}/3)")
            test_conn = psycopg2.connect(PG_DSN, connect_timeout=25)
            test_conn.close()
            _pg_pool = psycopg2.pool.ThreadedConnectionPool(
                minconn=1,
                maxconn=10,
                dsn=PG_DSN,
                connect_timeout=25,
            )
            IS_POSTGRES = True
            logger.info("Connecté avec succès à la base de données PostgreSQL (Neon).")
            break
        except Exception as e:
            if attempt < 3:
                time.sleep(2)
            else:
                logger.warning(f"Impossible de se connecter à PostgreSQL/Neon ({e}). Fallback sur SQLite local.")
                IS_POSTGRES = False



class SQLiteRowWrapper:
    """Permet l'accès par clé r['col'] et la conversion dict(r) comme sqlite3.Row."""
    def __init__(self, data: dict):
        self._data = data

    def __getitem__(self, key):
        return self._data[key]

    def get(self, key, default=None):
        return self._data.get(key, default)

    def keys(self):
        return self._data.keys()

    def values(self):
        return self._data.values()

    def items(self):
        return self._data.items()

    def __iter__(self):
        return iter(self._data)

    def __repr__(self):
        return repr(self._data)


class PostgresCursorWrapper:
    def __init__(self, cur):
        self._cur = cur
        self.lastrowid = None

    def execute(self, sql: str, params: tuple | list = ()):
        # 1. Remplacer 'IS ?' par 'IS NOT DISTINCT FROM %s' pour gérer NULL et valeurs
        cleaned_sql = re.sub(r"\bIS\s+\?", "IS NOT DISTINCT FROM %s", sql, flags=re.IGNORECASE)
        # 2. Remplacer les placeholders '?' de SQLite par '%s' de psycopg2
        cleaned_sql = cleaned_sql.replace("?", "%s")
        # 3. Gérer 'INSERT OR IGNORE'
        if "INSERT OR IGNORE INTO" in cleaned_sql.upper():
            cleaned_sql = re.sub(r"INSERT\s+OR\s+IGNORE\s+INTO", "INSERT INTO", cleaned_sql, flags=re.IGNORECASE)
            if "ON CONFLICT" not in cleaned_sql.upper():
                cleaned_sql += " ON CONFLICT DO NOTHING"

        # 4. Gérer lastrowid pour les INSERT
        is_insert = cleaned_sql.strip().upper().startswith("INSERT INTO")
        has_returning = "RETURNING" in cleaned_sql.upper()

        if is_insert and not has_returning and ("users" in cleaned_sql or "messages" in cleaned_sql or "facts" in cleaned_sql or "api_keys" in cleaned_sql):
            cleaned_sql += " RETURNING id"
            self._cur.execute(cleaned_sql, params)
            try:
                row = self._cur.fetchone()
                if row:
                    self.lastrowid = row.get("id")
            except Exception:
                pass
            return self

        self._cur.execute(cleaned_sql, params)
        return self

    def fetchone(self):
        r = self._cur.fetchone()
        return SQLiteRowWrapper(r) if r is not None else None

    def fetchall(self):
        rows = self._cur.fetchall()
        return [SQLiteRowWrapper(r) for r in rows] if rows else []

    @property
    def rowcount(self):
        return self._cur.rowcount


class PostgresConnectionWrapper:
    def __init__(self, conn):
        self._conn = conn

    def cursor(self):
        cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        return PostgresCursorWrapper(cur)

    def execute(self, sql: str, params: tuple | list = ()):
        cur = self.cursor()
        return cur.execute(sql, params)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()


@contextmanager
def db():
    """Fournit une connexion à la base de données.
    Utilise PostgreSQL (Neon) si configuré, sinon SQLite local.
    S'assure du commit automatique et de la libération des ressources."""
    if IS_POSTGRES and _pg_pool:
        conn = None
        is_from_pool = False
        try:
            conn = _pg_pool.getconn()
            is_from_pool = True
            # Vérification de la vivacité (Neon auto-suspend après inactivité)
            is_dead = False
            if conn.closed:
                is_dead = True
            else:
                try:
                    with conn.cursor() as test_cur:
                        test_cur.execute("SELECT 1")
                except Exception:
                    is_dead = True
            if is_dead:
                try:
                    _pg_pool.putconn(conn, close=True)
                except Exception:
                    pass
                conn = _pg_pool.getconn()
                is_from_pool = True
            wrapper = PostgresConnectionWrapper(conn)
            yield wrapper
            wrapper.commit()
        except Exception:
            if conn:
                try:
                    conn.rollback()
                except Exception:
                    pass
            raise
        finally:
            if conn:
                if is_from_pool and _pg_pool:
                    try:
                        _pg_pool.putconn(conn)
                    except Exception:
                        pass
                else:
                    try:
                        conn.close()
                    except Exception:
                        pass
    else:
        conn = sqlite3.connect(SQLITE_PATH)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def init_db():
    """Initialise le schéma de base de données selon le moteur actif (PostgreSQL ou SQLite)."""
    with db() as c:
        if IS_POSTGRES:
            c.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    title TEXT,
                    created_at DOUBLE PRECISION,
                    updated_at DOUBLE PRECISION,
                    user_id INTEGER
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id SERIAL PRIMARY KEY,
                    role TEXT,
                    content TEXT,
                    ts DOUBLE PRECISION,
                    session_id TEXT DEFAULT 'default',
                    media_kind TEXT,
                    media_path TEXT,
                    media_mime TEXT,
                    user_id INTEGER
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS facts (
                    id SERIAL PRIMARY KEY,
                    text TEXT,
                    ts DOUBLE PRECISION,
                    user_id INTEGER
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS api_keys (
                    id SERIAL PRIMARY KEY,
                    label TEXT,
                    api_key TEXT,
                    created_at DOUBLE PRECISION,
                    user_id INTEGER
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    username TEXT UNIQUE,
                    pass_hash TEXT,
                    salt TEXT,
                    api_key TEXT,
                    model_pref TEXT,
                    persona TEXT,
                    interests TEXT,
                    created_at DOUBLE PRECISION
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS auth_tokens (
                    token TEXT PRIMARY KEY,
                    user_id INTEGER,
                    created_at DOUBLE PRECISION
                )
            """)
            # Index optimisés pour PostgreSQL
            c.execute("CREATE INDEX IF NOT EXISTS idx_messages_user_sid ON messages(user_id, session_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_auth_tokens_token ON auth_tokens(token)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_facts_user ON facts(user_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_api_keys_user ON api_keys(user_id)")

            # Session principale par défaut
            c.execute(
                "INSERT INTO sessions (id, title, created_at, updated_at, user_id) VALUES ('default', 'Discussion principale', %s, %s, NULL) ON CONFLICT (id) DO NOTHING",
                (time.time(), time.time()),
            )
        else:
            # Mode SQLite local
            c.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    title TEXT,
                    created_at REAL,
                    updated_at REAL
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY,
                    role TEXT,
                    content TEXT,
                    ts REAL,
                    session_id TEXT DEFAULT 'default'
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS facts (
                    id INTEGER PRIMARY KEY,
                    text TEXT,
                    ts REAL
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS api_keys (
                    id INTEGER PRIMARY KEY,
                    label TEXT,
                    api_key TEXT UNIQUE,
                    created_at REAL
                )
            """)
            cols = [r["name"] for r in c.execute("PRAGMA table_info(messages)").fetchall()]
            if "session_id" not in cols:
                c.execute("ALTER TABLE messages ADD COLUMN session_id TEXT DEFAULT 'default'")
            for col in ("media_kind TEXT", "media_path TEXT", "media_mime TEXT"):
                if col.split()[0] not in cols:
                    c.execute(f"ALTER TABLE messages ADD COLUMN {col}")

            c.execute(
                "INSERT INTO sessions(id, title, created_at, updated_at) VALUES('default', 'Discussion principale', ?, ?) ON CONFLICT (id) DO NOTHING",
                (time.time(), time.time())
            )

            c.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY,
                    username TEXT UNIQUE,
                    pass_hash TEXT,
                    salt TEXT,
                    api_key TEXT,
                    model_pref TEXT,
                    persona TEXT,
                    interests TEXT,
                    created_at REAL
                )
            """)
            ucols = [r["name"] for r in c.execute("PRAGMA table_info(users)").fetchall()]
            if "interests" not in ucols:
                c.execute("ALTER TABLE users ADD COLUMN interests TEXT")
            c.execute("""
                CREATE TABLE IF NOT EXISTS auth_tokens (
                    token TEXT PRIMARY KEY,
                    user_id INTEGER,
                    created_at REAL
                )
            """)
            for table in ("sessions", "messages", "facts", "api_keys"):
                tcols = [r["name"] for r in c.execute(f"PRAGMA table_info({table})").fetchall()]
                if "user_id" not in tcols:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN user_id INTEGER")
