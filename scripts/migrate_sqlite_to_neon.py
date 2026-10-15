"""Script de migration des données SQLite (mel_ia.db) vers Neon PostgreSQL.
Usage : python scripts/migrate_sqlite_to_neon.py
"""
import os
import sys
import sqlite3
from pathlib import Path
from dotenv import load_dotenv

# Assurer l'encodage UTF-8 pour la console Windows
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

load_dotenv(ROOT_DIR / ".env")

import database

def migrate():
    db_url = os.getenv("DATABASE_URL", "").strip()
    if not db_url:
        print("[!] Erreur : La variable DATABASE_URL n'est pas definie dans votre fichier .env.")
        print("    Veuillez d'abord y ajouter l'URL de connexion fournie par Neon (ex: postgresql://...neon.tech/neondb?sslmode=require).")
        sys.exit(1)

    sqlite_path = ROOT_DIR / "data" / "mel_ia.db"
    if not sqlite_path.exists():
        print(f"[*] Aucun fichier SQLite trouve a l'emplacement : {sqlite_path}")
        print("[*] Initialisation directe du schema Neon...")
        database.init_db()
        print("[+] Base de donnees Neon initialisee avec succes.")
        return

    print("[*] Initialisation du schema sur Neon PostgreSQL...")
    database.init_db()

    print(f"[*] Connexion a SQLite : {sqlite_path}")
    s_conn = sqlite3.connect(sqlite_path)
    s_conn.row_factory = sqlite3.Row

    print("[*] Connexion a Neon PostgreSQL...")
    with database.db() as p_conn:
        tables = ["users", "auth_tokens", "sessions", "messages", "facts", "api_keys"]

        for table in tables:
            try:
                rows = s_conn.execute(f"SELECT * FROM {table}").fetchall()
            except sqlite3.OperationalError:
                print(f"[-] Table {table} inexistante dans SQLite, ignoree.")
                continue

            if not rows:
                print(f"[>] Table {table} : 0 enregistrement.")
                continue

            cols = [desc[0] for desc in s_conn.execute(f"SELECT * FROM {table} LIMIT 1").description]
            cols_str = ", ".join(cols)
            placeholders = ", ".join(["%s"] * len(cols))

            pk = "id" if "id" in cols else ("token" if "token" in cols else cols[0])
            insert_sql = f"INSERT INTO {table} ({cols_str}) VALUES ({placeholders}) ON CONFLICT ({pk}) DO NOTHING"

            inserted = 0
            for r in rows:
                p_conn.execute(insert_sql, tuple(r[c] for c in cols))
                inserted += 1

            print(f"[+] Table {table} : {inserted} enregistrement(s) migre(s).")

            if "id" in cols and table in ("users", "messages", "facts", "api_keys"):
                try:
                    p_conn.execute(f"SELECT setval('{table}_id_seq', COALESCE((SELECT MAX(id) FROM {table}), 1))")
                except Exception:
                    pass

    s_conn.close()
    print("\n[+] Migration terminee avec succes vers Neon PostgreSQL !")

if __name__ == "__main__":
    migrate()
