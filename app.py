"""MEL-IA : backend FastAPI. Chat multi-sessions + mémoire (SQLite) + cerveau Gemini (texte, image, audio, vidéo)."""
import base64
import hashlib
import os, re, secrets, sqlite3, time
import uuid
from datetime import datetime
from pathlib import Path

import httpx
import json
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv()
BASE = Path(__file__).parent
DB_PATH = BASE / "data" / "mel_ia.db"
DB_PATH.parent.mkdir(exist_ok=True)
MEDIA_DIR = BASE / "data" / "media"
MEDIA_DIR.mkdir(exist_ok=True)

# Tailles max acceptées (Gemini inline data plafonne ~20 Mo par requête)
MEDIA_LIMITS = {
    "image": 8 * 1024 * 1024,
    "audio": 10 * 1024 * 1024,
    "video": 15 * 1024 * 1024,
    "document": 15 * 1024 * 1024,
}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
AUDIO_EXTS = {".mp3", ".wav", ".ogg", ".oga", ".m4a", ".webm", ".mp4"}
VIDEO_EXTS = {".mp4", ".webm", ".mov", ".mkv"}
DOC_EXTS = {".pdf", ".txt", ".md", ".csv", ".json", ".py", ".js", ".ts", ".html", ".css", ".xml", ".sql", ".yaml", ".yml"}

MODEL = os.getenv("GEMINI_MODEL", "auto")
API_BASE = os.getenv("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta")
NAME = os.getenv("ASSISTANT_NAME", "MEL-IA")
OWNER = os.getenv("OWNER_NAME", "")

app = FastAPI(title=NAME)


# ---------- Base de données (mémoire locale du serveur) ----------
def db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


# ---------- Clés API (spécifiques à chaque utilisateur) ----------
_key_state: dict[str, dict] = {}  # key -> {"cool_until": ts}


def get_user_keys(user: dict | None) -> list[str]:
    """Retourne uniquement les clés API de l'utilisateur connecté.
    Aucune clé par défaut / partagée : chaque utilisateur utilise sa propre clé API
    renseignée lors de la connexion / inscription ou dans les paramètres."""
    if not user:
        return []
    keys: list[str] = []
    user_key = (user.get("api_key") or "").strip()
    if user_key and user_key not in keys:
        keys.append(user_key)
    uid = user.get("id")
    if uid:
        try:
            with db() as c:
                rows = c.execute(
                    "SELECT api_key FROM api_keys WHERE user_id = ? ORDER BY id",
                    (uid,),
                ).fetchall()
                for r in rows:
                    k = (r["api_key"] or "").strip()
                    if k and k not in keys:
                        keys.append(k)
        except Exception:
            pass
    return keys


def _key_usable(k: str) -> bool:
    return _key_state.get(k, {}).get("cool_until", 0) <= time.time()


def _cooldown(k: str, seconds: int):
    _key_state[k] = {"cool_until": time.time() + seconds}


def init_db():
    with db() as c:
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
        # Migration automatique si session_id est manquant
        cols = [r["name"] for r in c.execute("PRAGMA table_info(messages)").fetchall()]
        if "session_id" not in cols:
            c.execute("ALTER TABLE messages ADD COLUMN session_id TEXT DEFAULT 'default'")
        # Migration médias (images, vocaux, vidéos)
        cols = [r["name"] for r in c.execute("PRAGMA table_info(messages)").fetchall()]
        for col in ("media_kind TEXT", "media_path TEXT", "media_mime TEXT"):
            if col.split()[0] not in cols:
                c.execute(f"ALTER TABLE messages ADD COLUMN {col}")

        # Session par défaut
        c.execute(
            "INSERT OR IGNORE INTO sessions(id, title, created_at, updated_at) VALUES('default', 'Discussion principale', ?, ?)",
            (time.time(), time.time())
        )

        # ---------- Comptes utilisateurs ----------
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
        # Migration : centres d'intérêt
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
        # Migration : isolation des données par utilisateur
        for table in ("sessions", "messages", "facts", "api_keys"):
            tcols = [r["name"] for r in c.execute(f"PRAGMA table_info({table})").fetchall()]
            if "user_id" not in tcols:
                c.execute(f"ALTER TABLE {table} ADD COLUMN user_id INTEGER")


init_db()


# ---------- Comptes utilisateurs (identifiant + mot de passe) ----------
def _hash_pw(pw: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 200_000).hex()


# --- Chiffrement des clés API au repos (Fernet) ---
_ENC_FILE = BASE / "data" / ".enc_key"


def _load_fernet():
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        return None
    if _ENC_FILE.exists():
        raw = _ENC_FILE.read_bytes()
    else:
        raw = Fernet.generate_key()
        _ENC_FILE.write_bytes(raw)
    try:
        return Fernet(raw)
    except Exception:
        return None


_FERNET = _load_fernet()


def _enc_key(plain: str) -> str:
    """Chiffre une clé API avant stockage. Sans Fernet, stocke en clair (dev)."""
    if not plain:
        return ""
    if _FERNET is None:
        return plain
    return _FERNET.encrypt(plain.encode()).decode()


def _dec_key(stored: str) -> str:
    if not stored:
        return ""
    if _FERNET is None:
        return stored
    try:
        return _FERNET.decrypt(stored.encode()).decode()
    except Exception:
        return stored  # ancienne valeur en clair (avant chiffrement)


def _mask_key(k: str) -> str:
    return (k[:6] + "…" + k[-4:]) if len(k) > 12 else ("•" * 8 if k else "")


class Credentials(BaseModel):
    username: str
    password: str
    api_key: str = ""
    interests: str = ""  # centres d'intérêt séparés par des virgules (optionnel)


def _user_by_token(token: str | None) -> dict | None:
    if not token:
        return None
    with db() as c:
        row = c.execute(
            "SELECT u.* FROM users u JOIN auth_tokens t ON t.user_id = u.id WHERE t.token = ?",
            (token,),
        ).fetchone()
    return dict(row) if row else None


def current_user(token: str | None) -> dict | None:
    """Utilisateur connecté via header X-Auth-Token (ou None = mode invité).
    La clé API y est déchiffrée pour un usage interne (jamais renvoyée au client)."""
    u = _user_by_token(token)
    if u:
        u["api_key"] = _dec_key(u.get("api_key") or "")
    return u


@app.post("/api/auth/register")
def register(body: Credentials):
    username = (body.username or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9._-]{3,30}", username):
        raise HTTPException(400, "Identifiant : 3 à 30 caractères (lettres, chiffres, . _ -).")
    if len(body.password or "") < 6:
        raise HTTPException(400, "Mot de passe : 6 caractères minimum.")
    key = (body.api_key or "").strip()
    if not key:
        raise HTTPException(400, "Une clé API Gemini est requise pour créer ton compte.")
    salt = secrets.token_hex(16)
    token = secrets.token_hex(24)
    interests = (body.interests or "").strip()[:300]
    with db() as c:
        try:
            cur = c.execute(
                "INSERT INTO users(username, pass_hash, salt, api_key, model_pref, persona, interests, created_at) VALUES(?,?,?,?,?,?,?,?)",
                (username, _hash_pw(body.password, salt), salt, _enc_key(key), "auto", "", interests, time.time()),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(400, "Cet identifiant est déjà pris.")
        uid = cur.lastrowid
        # Chaque utilisateur hérite des clés globales en lecture seule ; il peut ajouter la sienne
        c.execute("INSERT INTO auth_tokens(token, user_id, created_at) VALUES(?,?,?)", (token, uid, time.time()))
    return {"token": token, "username": username, "api_key": _mask_key(key)}


@app.post("/api/auth/login")
def login(body: Credentials):
    username = (body.username or "").strip().lower()
    with db() as c:
        row = c.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    if not row:
        raise HTTPException(404, "Identifiant inconnu. Veuillez créer un compte si vous n'en avez pas.")
    if not secrets.compare_digest(_hash_pw(body.password, row["salt"]), row["pass_hash"]):
        raise HTTPException(401, "Mot de passe incorrect.")
    token = secrets.token_hex(24)
    saved_key = _dec_key(row["api_key"] or "")

    with db() as c:
        c.execute("INSERT INTO auth_tokens(token, user_id, created_at) VALUES(?,?,?)", (token, row["id"], time.time()))

    return {"token": token, "username": username, "api_key": _mask_key(saved_key)}


@app.post("/api/auth/logout")
def logout(x_auth_token: str | None = Header(None)):
    if x_auth_token:
        with db() as c:
            c.execute("DELETE FROM auth_tokens WHERE token = ?", (x_auth_token,))
    return {"ok": True}


@app.get("/api/auth/me")
def me(x_auth_token: str | None = Header(None)):
    u = current_user(x_auth_token)
    if not u:
        return {"logged_in": False}
    return {
        "logged_in": True,
        "username": u["username"],
        "api_key": _mask_key(u["api_key"] or ""),
        "model_pref": u["model_pref"] or "auto",
        "persona": u["persona"] or "",
        "interests": u["interests"] or "",
    }


class ProfileIn(BaseModel):
    model_config = {"protected_namespaces": ()}
    api_key: str | None = None
    model_pref: str | None = None
    persona: str | None = None
    interests: str | None = None


@app.post("/api/auth/profile")
def update_profile(body: ProfileIn, x_auth_token: str | None = Header(None)):
    u = current_user(x_auth_token)
    if not u:
        raise HTTPException(401, "Connecte-toi pour modifier ton profil.")
    sets, vals = [], []
    if body.api_key is not None:
        raw = body.api_key.strip()
        # Une valeur masquée (AIza…xxxx) ne remplace pas la clé chiffrée existante
        if raw and "…" not in raw:
            sets.append("api_key = ?"); vals.append(_enc_key(raw))
        elif not raw:
            sets.append("api_key = ?"); vals.append("")
    if body.model_pref is not None:
        sets.append("model_pref = ?"); vals.append((body.model_pref or "auto")[:40])
    if body.persona is not None:
        sets.append("persona = ?"); vals.append(body.persona[:2000])
    if body.interests is not None:
        sets.append("interests = ?"); vals.append(body.interests.strip()[:300])
    if sets:
        with db() as c:
            c.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = ?", (*vals, u["id"]))
    return {"ok": True}


# ---------- Cerveau ----------
def system_prompt(facts: list[str], persona: str = "", interests: str = "") -> str:
    who = f" de {OWNER}" if OWNER else ""
    p = (
        f"Tu es {NAME}, le bras droit virtuel et l'acolyte de confiance personnel{who}.\n"
        "Ta mission : agir comme un partenaire intellectuel et opérationnel de haut niveau, loyal, rigoureux et protecteur de la confidentialité de ton utilisateur.\n\n"
        "Tes principes directeurs :\n"
        "1. Confidentialité & confiance absolue : toutes les données, idées et secrets de projets sont traités avec la plus grande discrétion.\n"
        "2. Franchise bienveillante & vision stratégique : tu ne dis pas simplement oui par complaisance ; si tu déceles un risque, un piège ou une incohérence dans une idée, alerte poliment et propose aussitôt des pistes concrètes.\n"
        "3. Orientation résultat & efficacité : va droit au but, structure tes réponses avec clarté (étapes, tableaux, code propre et testé), sans bavardage superflu.\n"
        "4. Polyvalence d'expert : tu excelles dans le développement informatique, la stratégie, la synthèse de documents (PDF, CSV, texte), l'analyse d'images et l'organisation personnelle.\n"
        "5. Rigueur intellectuelle : si tu as un doute ou si une information manque, formule-le clairement plutôt que d'inventer.\n"
        f"Date actuelle : {datetime.now().strftime('%d/%m/%Y')}."
    )
    if persona:
        p += f"\n\nDirectives spécifiques de ton utilisateur :\n{persona}"
    if interests:
        p += f"\n\nCentres d'intérêt et univers de travail : {interests}"
    if facts:
        p += "\n\nMémoire à long terme accumulée sur ton utilisateur (à utiliser avec pertinence) :\n"
        p += "\n".join(f"- {f}" for f in facts)
    return p


_cache = {"model": None}
_BAD = ("tts", "live", "embedding", "robotics", "computer")

# ---------- Recherche web (Grounding Google Search) ----------
SEARCH_KEYWORDS = re.compile(
    r"\b(actuel|aujourd|actualit|nouvelles?|nouveau|dernier|prix|météo|weather|news|qui a gagn|résultat|score|"
    r"sorti|sortie|20\d\d|internet|en ligne|recherche|google|actualise|récent|récents|mis à jour|ce soir|cette semaine)\b",
    re.I,
)
GEN_IMAGE_RE = re.compile(
    r"^\s*(?:génère|generes?|cr[ée]e|dessine|fabrique|imagine|fais|make|generate|draw|create)\b[^.!?\n]{0,120}?"
    r"(?:une?|d[' ]une?|du |des |l[' ]|un )?\s*image|"
    r"^\s*(?:image|photo|illustration|dessin)\s+(?:de|d[' ]|du |des |dun |d'une?)",
    re.I,
)


def extract_sources(payload: dict) -> list[dict]:
    """Récupère les sources web (titres + liens) depuis groundingMetadata."""
    out, seen = [], set()
    try:
        meta = payload["candidates"][0].get("groundingMetadata", {})
        for chunk in meta.get("groundingChunks", []):
            web = chunk.get("web") or {}
            uri = web.get("uri") or web.get("url") or ""
            title = web.get("title") or uri
            if uri and uri not in seen:
                seen.add(uri)
                out.append({"title": title[:120], "url": uri})
    except (KeyError, IndexError, TypeError):
        pass
    return out[:8]


def fetch_images(query: str) -> list[dict]:
    """Images d'illustration issues de la recherche (via DuckDuckGo, sans clé)."""
    try:
        r = httpx.post(
            "https://duckduckgo.com/",
            params={"q": query},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=8,
        )
        m = re.search(r'vqd=["\']?([\d-]+)', r.text)
        if not m:
            return []
        vqd = m.group(1)
        rr = httpx.get(
            "https://duckduckgo.com/i.js",
            params={"l": "fr-fr", "o": "json", "q": query, "vqd": vqd, "f": ",,", "p": "1"},
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://duckduckgo.com/"},
            timeout=8,
        )
        if rr.status_code != 200:
            return []
        items = rr.json().get("results", [])[:4]
        return [
            {"url": it.get("image"), "title": (it.get("title") or "")[:100], "source": it.get("url", "")}
            for it in items if it.get("image")
        ]
    except Exception:
        return []


def get_model_candidates() -> list[str]:
    """Retourne les modèles candidats ordonnés pour garantir une réponse rapide et stable."""
    candidates = []
    if MODEL and MODEL != "auto":
        candidates.append(MODEL)
    # Modèles actifs vérifiés dans l'API Gemini
    defaults = [
        "gemini-flash-lite-latest",
        "gemini-3-flash-preview",
        "gemma-4-31b-it",
        "gemini-flash-latest",
        "gemini-3.8-flash",
        "gemini-3.5-flash",
        "gemini-pro-latest",
    ]
    for m in defaults:
        if m not in candidates:
            candidates.append(m)
    return candidates


def _history_parts(m: dict) -> list[dict]:
    """Construit les parts Gemini d'un message d'historique (texte + média éventuel ou document)."""
    parts: list[dict] = []
    if m.get("content"):
        parts.append({"text": m["content"]})
    mp = m.get("media_path")
    kind = m.get("media_kind")
    if mp and kind in ("image", "audio", "video", "document"):
        try:
            file_path = MEDIA_DIR / Path(mp).name
            if file_path.exists():
                raw = file_path.read_bytes()
                mime = m.get("media_mime") or "application/octet-stream"
                if kind == "document" and not mime.startswith("application/pdf"):
                    try:
                        text_content = raw.decode("utf-8", errors="replace")
                        parts.append({"text": f"\n\n[Contenu du document joint {Path(mp).name}] :\n{text_content[:30000]}"})
                    except Exception:
                        pass
                elif raw and len(raw) <= 20 * 1024 * 1024:
                    parts.append({
                        "inline_data": {
                            "mime_type": mime,
                            "data": base64.b64encode(raw).decode("ascii"),
                        }
                    })
        except OSError:
            pass
    return parts or [{"text": "(message vide)"}]


def ask_gemini(history: list[dict], facts: list[str], *, user: dict | None = None,
               search: bool = False) -> dict:
    """Interroge Gemini avec la clé personnelle de l'utilisateur connecté."""
    user_keys = get_user_keys(user)
    keys = [k for k in user_keys if _key_usable(k)] or user_keys
    if not keys:
        raise HTTPException(
            401,
            "Aucune clé API Gemini : veuillez vous connecter avec votre compte ou renseigner votre clé API sur la page de connexion.",
        )
    body = {
        "systemInstruction": {"parts": [{"text": system_prompt(facts, (user or {}).get("persona") or "", (user or {}).get("interests") or "")}]},
        "contents": [
            {"role": "user" if m["role"] == "user" else "model", "parts": _history_parts(m)}
            for m in history
        ],
    }
    # Recherche web : grounding Google Search (citations + actualités)
    if search:
        body["tools"] = [{"google_search": {}}]

    candidates = list(get_model_candidates())
    # Préférence de modèle de l'utilisateur connecté
    pref = (user or {}).get("model_pref") or ""
    if pref and pref != "auto" and pref in candidates:
        candidates.remove(pref)
        candidates.insert(0, pref)
    # Si on a un modèle mis en cache avec succès, on le tente en premier
    if _cache["model"] and _cache["model"] in candidates:
        candidates.remove(_cache["model"])
        candidates.insert(0, _cache["model"])

    last_err = None
    for key in keys:
        for model in candidates:
            try:
                r = httpx.post(
                    f"{API_BASE}/models/{model}:generateContent",
                    headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                    json=body,
                    timeout=90,
                )
            except httpx.HTTPError as exc:
                last_err = f"Connexion expirée ({exc})"
                continue

            if r.status_code == 200:
                _cache["model"] = model
                payload = r.json()
                try:
                    parts = payload["candidates"][0]["content"]["parts"]
                    reply = "".join(p.get("text", "") for p in parts).strip() or "(réponse vide)"
                except (KeyError, IndexError):
                    return {"reply": "Je n'ai pas pu produire de réponse pour cette demande.", "sources": [], "images": [], "model": model}
                result = {"reply": reply, "sources": [], "images": [], "model": model}
                if search:
                    result["sources"] = extract_sources(payload)
                    q = history[-1]["content"][:200] if history else ""
                    result["images"] = fetch_images(q)
                return result

            # Si le modèle est indisponible (503), quota du modèle (429), déprécié (404), etc.
            # On tente le MODÈLE SUIVANT ! On ne bloque pas les autres modèles !
            last_err = f"Modèle {model} ({r.status_code})"
            if r.status_code in (401, 403):
                _cooldown(key, 3600)
                break
            continue

    if last_err and "quota" in last_err:
        raise HTTPException(429, "Limites gratuites atteintes sur vos modèles. Réessaie dans quelques instants.")
    raise HTTPException(502, f"Impossible de contacter Gemini ({last_err or 'tous les modèles ont échoué'}).")


# ---------- Génération d'images (Gemini image generation) ----------
IMAGE_MODELS = ["gemini-2.5-flash-image", "gemini-3.1-flash-image", "gemini-3-pro-image", "imagen-3.0-generate-002"]


def generate_image(prompt: str, user: dict | None = None) -> str:
    """Génère une image avec Gemini en utilisant la clé de l'utilisateur."""
    user_keys = get_user_keys(user)
    keys = [k for k in user_keys if _key_usable(k)] or user_keys
    if not keys:
        raise HTTPException(
            401,
            "Aucune clé API Gemini disponible : connectez-vous avec votre compte ou ajoutez une clé.",
        )
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
    }
    last_err = None
    for key in keys:
        for model in IMAGE_MODELS:
            try:
                r = httpx.post(
                    f"{API_BASE}/models/{model}:generateContent",
                    headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                    json=body,
                    timeout=120,
                )
            except httpx.HTTPError as exc:
                last_err = f"Connexion expirée ({exc})"
                continue
            if r.status_code == 200:
                try:
                    parts = r.json()["candidates"][0]["content"]["parts"]
                except (KeyError, IndexError):
                    last_err = "Réponse vide du modèle image"
                    continue
                for p in parts:
                    inline = p.get("inlineData") or {}
                    data = inline.get("data")
                    mime = inline.get("mime_type") or inline.get("mimeType") or ""
                    if data and mime.startswith("image/"):
                        ext = ".png" if "png" in mime else ".webp"
                        fname = f"gen_{uuid.uuid4().hex[:12]}{ext}"
                        (MEDIA_DIR / fname).write_bytes(base64.b64decode(data))
                        return fname
                last_err = "Aucune image dans la réponse"
                continue
            if r.status_code in (503, 404, 500):
                _cooldown(key, 60)
                break
            if r.status_code == 429:
                last_err = "Limite de quota temporaire"
                _cooldown(key, 300)
                break
            if r.status_code in (400, 403):
                _cooldown(key, 3600)
                break
            last_err = f"Erreur {r.status_code}"
    raise HTTPException(502, f"Impossible de générer l'image ({last_err}).")


# ---------- API ----------
class ChatIn(BaseModel):
    message: str
    session_id: str = "default"
    search: bool | None = None          # force la recherche web (None = auto)
    generate_image: bool | None = None  # force la génération d'image (None = auto)
    replace_last: bool = False          # pour régénérer proprement sans doublon


FACT_RE = re.compile(r"^\s*(?:retiens|souviens[- ]toi|mémorise|memorise)\s+(?:que\s+)?(.+)$", re.I | re.S)


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "name": NAME,
        "owner": OWNER,
        "model": _cache["model"] or MODEL,
    }


@app.get("/api/sessions")
def get_sessions(x_auth_token: str | None = Header(None)):
    user = current_user(x_auth_token)
    uid = user["id"] if user else None
    with db() as c:
        rows = c.execute(
            "SELECT id, title, created_at, updated_at FROM sessions WHERE user_id IS ? ORDER BY updated_at DESC",
            (uid,),
        ).fetchall()
        return {"sessions": [dict(r) for r in rows]}


@app.post("/api/sessions")
def create_session(x_auth_token: str | None = Header(None)):
    user = current_user(x_auth_token)
    sid = secrets.token_hex(6)
    now = time.time()
    with db() as c:
        c.execute(
            "INSERT INTO sessions (id, title, created_at, updated_at, user_id) VALUES (?, 'Nouvelle conversation', ?, ?, ?)",
            (sid, now, now, user["id"] if user else None),
        )
    return {"session_id": sid, "title": "Nouvelle conversation"}


@app.delete("/api/sessions/{session_id}")
def delete_session(session_id: str, x_auth_token: str | None = Header(None)):
    user = current_user(x_auth_token)
    uid = user["id"] if user else None
    with db() as c:
        if session_id == "default":
            c.execute("DELETE FROM messages WHERE session_id = 'default' AND user_id IS ?", (uid,))
            c.execute("UPDATE sessions SET title = 'Discussion principale', updated_at = ? WHERE id = 'default'", (time.time(),))
        else:
            c.execute("DELETE FROM messages WHERE session_id = ? AND user_id IS ?", (session_id, uid))
            c.execute("DELETE FROM sessions WHERE id = ? AND user_id IS ?", (session_id, uid))
    return {"ok": True}


@app.post("/api/chat")
def chat(body: ChatIn, x_auth_token: str | None = Header(None)):
    user = current_user(x_auth_token)
    uid = user["id"] if user else None
    text = body.message.strip()
    sid = body.session_id or "default"
    if not text:
        raise HTTPException(400, "Message vide.")
    now = time.time()
    do_search = body.search if body.search is not None else bool(SEARCH_KEYWORDS.search(text))
    do_image = body.generate_image if body.generate_image is not None else bool(GEN_IMAGE_RE.search(text))

    with db() as c:
        s_row = c.execute("SELECT title, user_id FROM sessions WHERE id = ?", (sid,)).fetchone()
        title_text = text[:40] + ("…" if len(text) > 40 else "")
        if not s_row:
            c.execute(
                "INSERT INTO sessions (id, title, created_at, updated_at, user_id) VALUES (?, ?, ?, ?, ?)",
                (sid, title_text, now, now, uid),
            )
        else:
            if s_row["user_id"] is None and uid is not None:
                c.execute("UPDATE sessions SET user_id = ? WHERE id = ?", (uid, sid))
            if s_row["title"] in ("Nouvelle conversation", "Discussion principale"):
                c.execute(
                    "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?",
                    (title_text, now, sid),
                )
            else:
                c.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (now, sid))

        if body.replace_last:
            c.execute(
                "DELETE FROM messages WHERE id = (SELECT id FROM messages WHERE session_id = ? AND user_id IS ? AND role = 'assistant' ORDER BY id DESC LIMIT 1)",
                (sid, uid),
            )
        else:
            m = FACT_RE.match(text)
            if m:
                c.execute("INSERT INTO facts(text, ts, user_id) VALUES(?,?,?)", (m.group(1).strip(), now, uid))
            c.execute("INSERT INTO messages(role, content, ts, session_id, user_id) VALUES('user',?,?,?,?)", (text, now, sid, uid))

        rows = c.execute(
            "SELECT role, content, media_kind, media_path, media_mime FROM messages WHERE session_id = ? AND user_id IS ? ORDER BY id DESC LIMIT 40",
            (sid, uid),
        ).fetchall()[::-1]
        facts = [r["text"] for r in c.execute("SELECT text FROM facts WHERE user_id IS ? ORDER BY id", (uid,)).fetchall()]

    resp: dict = {"session_id": sid}

    if do_image:
        img_prompt = GEN_IMAGE_RE.sub("", text, count=1).strip() or text
        fname = generate_image(f"Image photoréaliste de haute qualité : {img_prompt}", user)
        reply = f"Voici l'image générée pour : **{img_prompt}**"
        now2 = time.time()
        with db() as c:
            c.execute(
                "INSERT INTO messages(role, content, ts, session_id, user_id, media_kind, media_path, media_mime) VALUES('assistant',?,?,?,?,?,?,?)",
                (reply, now2, sid, uid, "image", fname, "image/png"),
            )
        resp.update({"reply": reply, "media_url": f"/api/media/{fname}", "generated": True})
        return resp

    result = ask_gemini([dict(r) for r in rows], facts, user=user, search=do_search)
    reply = result["reply"]

    with db() as c:
        c.execute("INSERT INTO messages(role, content, ts, session_id, user_id) VALUES('assistant',?,?,?,?)", (reply, time.time(), sid, uid))
    resp.update({
        "reply": reply,
        "sources": result.get("sources", []),
        "images": result.get("images", []),
        "model": result.get("model", ""),
        "search": do_search,
    })
    return resp


@app.post("/api/chat/stream")
async def chat_stream(body: ChatIn, x_auth_token: str | None = Header(None)):
    """Streaming SSE : envoie les tokens en temps réel au client."""
    user = current_user(x_auth_token)
    uid = user["id"] if user else None
    text = body.message.strip()
    sid = body.session_id or "default"
    if not text:
        raise HTTPException(400, "Message vide.")
    now = time.time()
    do_search = body.search if body.search is not None else bool(SEARCH_KEYWORDS.search(text))
    do_image = body.generate_image if body.generate_image is not None else bool(GEN_IMAGE_RE.search(text))

    with db() as c:
        s_row = c.execute("SELECT title, user_id FROM sessions WHERE id = ?", (sid,)).fetchone()
        title_text = text[:40] + ("…" if len(text) > 40 else "")
        if not s_row:
            c.execute(
                "INSERT INTO sessions (id, title, created_at, updated_at, user_id) VALUES (?, ?, ?, ?, ?)",
                (sid, title_text, now, now, uid),
            )
        else:
            if s_row["user_id"] is None and uid is not None:
                c.execute("UPDATE sessions SET user_id = ? WHERE id = ?", (uid, sid))
            if s_row["title"] in ("Nouvelle conversation", "Discussion principale"):
                c.execute(
                    "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?",
                    (title_text, now, sid),
                )
            else:
                c.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (now, sid))

        if body.replace_last:
            c.execute(
                "DELETE FROM messages WHERE id = (SELECT id FROM messages WHERE session_id = ? AND user_id IS ? AND role = 'assistant' ORDER BY id DESC LIMIT 1)",
                (sid, uid),
            )
        else:
            m = FACT_RE.match(text)
            if m:
                c.execute("INSERT INTO facts(text, ts, user_id) VALUES(?,?,?)", (m.group(1).strip(), now, uid))
            c.execute("INSERT INTO messages(role, content, ts, session_id, user_id) VALUES('user',?,?,?,?)", (text, now, sid, uid))

        rows = c.execute(
            "SELECT role, content, media_kind, media_path, media_mime FROM messages WHERE session_id = ? AND user_id IS ? ORDER BY id DESC LIMIT 40",
            (sid, uid),
        ).fetchall()[::-1]
        facts = [r["text"] for r in c.execute("SELECT text FROM facts WHERE user_id IS ? ORDER BY id", (uid,)).fetchall()]

    async def event_generator():
        if do_image:
            img_prompt = GEN_IMAGE_RE.sub("", text, count=1).strip() or text
            try:
                fname = generate_image(f"Image photoréaliste de haute qualité : {img_prompt}", user)
                reply = f"Voici l'image générée pour : **{img_prompt}**"
                now2 = time.time()
                with db() as c:
                    c.execute(
                        "INSERT INTO messages(role, content, ts, session_id, user_id, media_kind, media_path, media_mime) VALUES('assistant',?,?,?,?,?,?,?)",
                        (reply, now2, sid, uid, "image", fname, "image/png"),
                    )
                yield f"data: {json.dumps({'chunk': reply, 'media_url': f'/api/media/{fname}', 'done': True, 'session_id': sid})}\n\n"
            except Exception as e:
                yield f"data: {json.dumps({'error': str(e), 'done': True})}\n\n"
            return

        user_keys = get_user_keys(user)
        keys = [k for k in user_keys if _key_usable(k)] or user_keys
        if not keys:
            yield f"data: {json.dumps({'error': 'Aucune clé API Gemini : veuillez vous connecter avec votre compte ou renseigner votre clé API sur la page de connexion.', 'done': True})}\n\n"
            return

        req_body = {
            "systemInstruction": {"parts": [{"text": system_prompt(facts, (user or {}).get("persona") or "", (user or {}).get("interests") or "")}]},
            "contents": [
                {"role": "user" if m["role"] == "user" else "model", "parts": _history_parts(m)}
                for m in rows
            ],
        }
        if do_search:
            req_body["tools"] = [{"google_search": {}}]

        candidates = list(get_model_candidates())
        pref = (user or {}).get("model_pref") or ""
        if pref and pref != "auto" and pref in candidates:
            candidates.remove(pref)
            candidates.insert(0, pref)
        if _cache["model"] and _cache["model"] in candidates:
            candidates.remove(_cache["model"])
            candidates.insert(0, _cache["model"])

        full_reply = ""
        sources = []
        used_model = candidates[0]
        success = False

        async with httpx.AsyncClient(timeout=90) as client:
            for key in keys:
                for model in candidates:
                    try:
                        url = f"{API_BASE}/models/{model}:streamGenerateContent?alt=sse"
                        async with client.stream(
                            "POST",
                            url,
                            headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                            json=req_body,
                        ) as r:
                            if r.status_code != 200:
                                if r.status_code in (401, 403):
                                    break
                                continue
                            used_model = model
                            _cache["model"] = model
                            async for line in r.aiter_lines():
                                line = line.strip()
                                if not line or not line.startswith("data:"):
                                    continue
                                raw_json = line[5:].strip()
                                if raw_json == "[DONE]":
                                    break
                                try:
                                    chunk_data = json.loads(raw_json)
                                    cand = (chunk_data.get("candidates") or [{}])[0]
                                    parts = cand.get("content", {}).get("parts", [])
                                    text_piece = "".join(p.get("text", "") for p in parts if "text" in p)
                                    if not sources:
                                        meta = cand.get("groundingMetadata", {})
                                        for ch in meta.get("groundingChunks", []):
                                            web = ch.get("web") or {}
                                            u = web.get("uri") or web.get("url") or ""
                                            if u and not any(s["url"] == u for s in sources):
                                                sources.append({"title": (web.get("title") or u)[:120], "url": u})
                                    if text_piece:
                                        full_reply += text_piece
                                        yield f"data: {json.dumps({'chunk': text_piece, 'done': False})}\n\n"
                                except Exception:
                                    continue
                            if full_reply:
                                success = True
                                break
                    except Exception:
                        continue
                if success:
                    break

        if not success or not full_reply:
            try:
                res = ask_gemini([dict(r) for r in rows], facts, user=user, search=do_search)
                full_reply = res["reply"]
                sources = res.get("sources", [])
                used_model = res.get("model", used_model)
                yield f"data: {json.dumps({'chunk': full_reply, 'done': False})}\n\n"
            except Exception as e:
                yield f"data: {json.dumps({'error': str(e), 'done': True})}\n\n"
                return

        images = []
        if do_search and full_reply:
            images = fetch_images(text)

        with db() as c:
            c.execute(
                "INSERT INTO messages(role, content, ts, session_id, user_id) VALUES('assistant',?,?,?,?)",
                (full_reply, time.time(), sid, uid),
            )

        yield f"data: {json.dumps({'done': True, 'reply': full_reply, 'sources': sources, 'images': images, 'model': used_model, 'session_id': sid})}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


def _kind_of(filename: str, mime: str) -> str | None:
    ext = Path(filename or "").suffix.lower()
    if mime.startswith("image/") or ext in IMAGE_EXTS:
        return "image"
    if mime.startswith("video/") or ext in VIDEO_EXTS:
        return "video"
    if mime.startswith("audio/") or ext in AUDIO_EXTS:
        return "audio"
    if mime == "application/pdf" or ext in DOC_EXTS or mime.startswith("text/"):
        return "document"
    return None


@app.post("/api/chat-media")
async def chat_media(
    message: str = Form(""),
    session_id: str = Form("default"),
    file: UploadFile = File(...),
    x_auth_token: str | None = Header(None),
):
    """Envoie un message avec image / vocal / vidéo / document. Le fichier est stocké et analysé par Gemini."""
    user = current_user(x_auth_token)
    uid = user["id"] if user else None
    text = (message or "").strip()
    sid = session_id or "default"
    mime = (file.content_type or "").split(";")[0].strip().lower() or "application/octet-stream"
    kind = _kind_of(file.filename or "", mime)
    if not kind:
        raise HTTPException(400, "Type de fichier non pris en charge (image, audio, vidéo ou document).")
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "Fichier vide.")
    if len(raw) > MEDIA_LIMITS[kind]:
        raise HTTPException(400, f"Fichier trop lourd (max {MEDIA_LIMITS[kind] // (1024 * 1024)} Mo).")
    ext = Path(file.filename or "").suffix.lower() or {"image": ".jpg", "audio": ".webm", "video": ".mp4", "document": ".bin"}[kind]
    fname = f"{uuid.uuid4().hex}{ext}"
    try:
        (MEDIA_DIR / fname).write_bytes(raw)
    except OSError:
        raise HTTPException(500, "Impossible d'enregistrer le fichier.")

    labels = {
        "image": "Image envoyée",
        "audio": "Message vocal",
        "video": "Vidéo envoyée",
        "document": f"Document joint ({file.filename})",
    }
    content = text or f"[{labels[kind]}]"
    now = time.time()
    with db() as c:
        s_row = c.execute("SELECT title, user_id FROM sessions WHERE id = ?", (sid,)).fetchone()
        if not s_row:
            c.execute(
                "INSERT INTO sessions (id, title, created_at, updated_at, user_id) VALUES (?, ?, ?, ?, ?)",
                (sid, labels[kind], now, now, uid),
            )
        else:
            if s_row["user_id"] is None and uid is not None:
                c.execute("UPDATE sessions SET user_id = ? WHERE id = ?", (uid, sid))
            c.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (now, sid))
        c.execute(
            "INSERT INTO messages(role, content, ts, session_id, user_id, media_kind, media_path, media_mime) VALUES('user',?,?,?,?,?,?,?)",
            (content, now, sid, uid, kind, fname, mime),
        )
        rows = c.execute(
            "SELECT role, content, media_kind, media_path, media_mime FROM messages WHERE session_id = ? AND user_id IS ? ORDER BY id DESC LIMIT 40",
            (sid, uid),
        ).fetchall()[::-1]
        facts = [r["text"] for r in c.execute("SELECT text FROM facts WHERE user_id IS ? ORDER BY id", (uid,)).fetchall()]

    result = ask_gemini([dict(r) for r in rows], facts, user=user, search=bool(SEARCH_KEYWORDS.search(text)))
    reply = result["reply"]

    with db() as c:
        c.execute("INSERT INTO messages(role, content, ts, session_id, user_id) VALUES('assistant',?,?,?,?)", (reply, time.time(), sid, uid))
    return {
        "reply": reply,
        "session_id": sid,
        "sources": result.get("sources", []),
        "images": result.get("images", []),
    }


@app.get("/api/media/{fname}")
def get_media(fname: str):
    """Sert un fichier média (nom de fichier UUID aléatoire, non devinable)."""
    safe = Path(fname).name
    if not safe or ".." in fname:
        raise HTTPException(400, "Nom invalide.")
    p = MEDIA_DIR / safe
    if not p.exists():
        raise HTTPException(404, "Fichier introuvable.")
    return FileResponse(p)


# ---------- Clés API : gestion via le menu Paramètres ----------
class KeyIn(BaseModel):
    label: str = ""
    api_key: str


@app.get("/api/keys")
def list_keys(x_auth_token: str | None = Header(None)):
    user = current_user(x_auth_token)
    if not user:
        return {"keys": []}
    uid = user["id"]
    out = []
    # Clé principale du compte (fournie à la connexion / inscription)
    if user.get("api_key"):
        mk = user["api_key"]
        out.append({
            "id": 0,
            "label": "Clé du compte (connexion)",
            "masked": (mk[:6] + "…" + mk[-4:]) if len(mk) > 12 else "••••",
            "active": _key_usable(mk),
        })
    with db() as c:
        rows = c.execute(
            "SELECT id, label, api_key, created_at FROM api_keys WHERE user_id = ? ORDER BY id",
            (uid,),
        ).fetchall()
    for r in rows:
        k = r["api_key"]
        out.append({
            "id": r["id"],
            "label": r["label"] or "Clé secondaire",
            "masked": (k[:6] + "…" + k[-4:]) if len(k) > 12 else "••••",
            "active": _key_usable(k),
        })
    return {"keys": out}


@app.post("/api/keys")
def add_key(body: KeyIn, x_auth_token: str | None = Header(None)):
    user = current_user(x_auth_token)
    if not user:
        raise HTTPException(401, "Veuillez vous connecter pour enregistrer une clé.")
    key = (body.api_key or "").strip()
    if len(key) < 10:
        raise HTTPException(400, "Clé invalide.")
    with db() as c:
        try:
            c.execute(
                "INSERT INTO api_keys(label, api_key, created_at, user_id) VALUES(?,?,?,?)",
                ((body.label or "").strip()[:60], key, time.time(), user["id"]),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(400, "Cette clé existe déjà pour votre compte.")
    return {"ok": True}


@app.delete("/api/keys/{kid}")
def del_key(kid: int, x_auth_token: str | None = Header(None)):
    user = current_user(x_auth_token)
    if not user:
        raise HTTPException(401, "Veuillez vous connecter pour gérer vos clés.")
    if kid == 0:
        with db() as c:
            c.execute("UPDATE users SET api_key = '' WHERE id = ?", (user["id"],))
        return {"ok": True}
    uid = user["id"]
    with db() as c:
        c.execute("DELETE FROM api_keys WHERE id=? AND user_id = ?", (kid, uid))
    return {"ok": True}


@app.get("/api/history")
def history(session_id: str = "default", x_auth_token: str | None = Header(None)):
    user = current_user(x_auth_token)
    uid = user["id"] if user else None
    with db() as c:
        rows = c.execute(
            "SELECT role, content, media_kind, media_path, media_mime FROM messages WHERE session_id = ? AND user_id IS ? ORDER BY id DESC LIMIT 60",
            (session_id, uid),
        ).fetchall()[::-1]
    msgs = []
    for r in rows:
        m = dict(r)
        if m.get("media_path"):
            m["media_url"] = f"/api/media/{m['media_path']}"
        msgs.append(m)
    return {"messages": msgs, "name": NAME, "owner": OWNER, "session_id": session_id,
            "username": user["username"] if user else ""}


@app.post("/api/clear")
def clear(session_id: str | None = None, x_auth_token: str | None = Header(None)):
    user = current_user(x_auth_token)
    uid = user["id"] if user else None
    with db() as c:
        if session_id:
            c.execute("DELETE FROM messages WHERE session_id = ? AND user_id IS ?", (session_id, uid))
        else:
            c.execute("DELETE FROM messages WHERE user_id IS ?", (uid,))
            c.execute("DELETE FROM sessions WHERE id != 'default' AND user_id IS ?", (uid,))
            c.execute("UPDATE sessions SET title = 'Discussion principale', updated_at = ? WHERE id = 'default'", (time.time(),))
    return {"ok": True}


@app.get("/api/facts")
def get_facts(x_auth_token: str | None = Header(None)):
    user = current_user(x_auth_token)
    uid = user["id"] if user else None
    with db() as c:
        return {"facts": [dict(r) for r in c.execute("SELECT id, text FROM facts WHERE user_id IS ? ORDER BY id", (uid,))]}


@app.delete("/api/facts/{fid}")
def del_fact(fid: int, x_auth_token: str | None = Header(None)):
    user = current_user(x_auth_token)
    uid = user["id"] if user else None
    with db() as c:
        c.execute("DELETE FROM facts WHERE id=? AND user_id IS ?", (fid, uid))
    return {"ok": True}


# ---------- Pages ----------
S = BASE / "static"
app.mount("/static", StaticFiles(directory=S), name="static")


@app.get("/")
def home():
    return FileResponse(S / "index.html")


@app.get("/app")
def chat_page():
    return FileResponse(S / "app.html")


@app.get("/manifest.json")
def manifest():
    return FileResponse(S / "manifest.json", media_type="application/manifest+json")


@app.get("/sw.js")
def sw():
    return FileResponse(S / "sw.js", media_type="application/javascript")