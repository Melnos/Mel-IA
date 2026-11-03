"""MEL-IA : backend FastAPI. Chat multi-sessions + mémoire (SQLite) + cerveau Gemini (texte, image, audio, vidéo)."""
import base64
import hashlib
import os, re, secrets, time
import uuid
from datetime import datetime
from pathlib import Path

import httpx
import json
from pathlib import Path
import sys
from dotenv import load_dotenv

if getattr(sys, "frozen", False):
    # En mode exécutable autonome PyInstaller
    BASE = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    EXE_DIR = Path(sys.executable).parent
    DATA_DIR = EXE_DIR / "data"
    load_dotenv(EXE_DIR / ".env")
    load_dotenv(BASE / ".env")
else:
    BASE = Path(__file__).parent
    DATA_DIR = BASE / "data"
    load_dotenv(BASE / ".env")

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from database import db, init_db, DBIntegrityError, IS_POSTGRES

DB_PATH = DATA_DIR / "mel_ia.db"
DB_PATH.parent.mkdir(exist_ok=True)
MEDIA_DIR = DATA_DIR / "media"
MEDIA_DIR.mkdir(exist_ok=True)

# Tailles max acceptées (Gemini inline data plafonne ~20 Mo par requête)
MEDIA_LIMITS = {
    "image": 8 * 1024 * 1024,
    "audio": 10 * 1024 * 1024,
    "video": 15 * 1024 * 1024,
    "document": 15 * 1024 * 1024,
}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"}
AUDIO_EXTS = {".mp3", ".wav", ".ogg", ".oga", ".m4a", ".aac", ".flac", ".opus"}
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".wmv"}
DOC_EXTS = {".pdf", ".txt", ".md", ".csv", ".json", ".py", ".js", ".ts", ".html", ".css", ".xml", ".sql", ".yaml", ".yml"}

MODEL = os.getenv("GEMINI_MODEL", "auto")
API_BASE = os.getenv("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta")
NAME = os.getenv("ASSISTANT_NAME", "MEL-IA")
OWNER = os.getenv("OWNER_NAME", "")

app = FastAPI(title=NAME)


# ---------- Base de données (PostgreSQL Neon / SQLite local) ----------
# db() et init_db() sont importés depuis database.py


# ---------- Chiffrement des clés API au repos (Fernet) ----------
_ENC_FILE = BASE / "data" / ".enc_key"


def _load_fernet():
    try:
        from cryptography.fernet import Fernet, MultiFernet
    except ImportError:
        return None
    fernets = []
    if _ENC_FILE.exists():
        try:
            raw = _ENC_FILE.read_bytes().strip()
            if raw:
                fernets.append(Fernet(raw))
        except Exception:
            pass
    env_k = os.getenv("SECRET_KEY", "").strip()
    if env_k:
        try:
            fernets.append(Fernet(env_k.encode()))
        except Exception:
            try:
                stable_key = base64.urlsafe_b64encode(hashlib.sha256(env_k.encode()).digest())
                fernets.append(Fernet(stable_key))
            except Exception:
                pass
    fallback_seed = "mel-ia-stable-encryption-key-seed-2026"
    fallback_key = base64.urlsafe_b64encode(hashlib.sha256(fallback_seed.encode()).digest())
    try:
        fernets.append(Fernet(fallback_key))
    except Exception:
        pass
    if not _ENC_FILE.exists() and not fernets:
        raw = Fernet.generate_key()
        try:
            _ENC_FILE.write_bytes(raw)
            fernets.insert(0, Fernet(raw))
        except Exception:
            pass
    if not fernets:
        return None
    if len(fernets) == 1:
        return fernets[0]
    return MultiFernet(fernets)


_FERNET = _load_fernet()


def _enc_key(plain: str) -> str:
    """Chiffre une clé API avant stockage. Sans Fernet, stocke en clair (dev)."""
    if not plain:
        return ""
    if _FERNET is None:
        return plain
    return _FERNET.encrypt(plain.encode()).decode()


def _dec_key(stored: str) -> str:
    """Déchiffre une clé API stockée."""
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


# ---------- Clés API (spécifiques à chaque utilisateur) ----------
_key_state: dict[str, dict] = {}  # key -> {"cool_until": ts}


def get_user_keys(user: dict | None) -> list[str]:
    """Retourne les clés API applicables déchiffrées.
    Priorité :
    1. Clés de l'utilisateur connecté (clé de compte ou clés secondaires)
    2. Fallback sur GEMINI_API_KEY du fichier .env (API débitrice du serveur)
    3. Fallback sur la clé d'administration présente en base SQLite (si existante)
    """
    keys: list[str] = []
    if user:
        raw_user_key = (user.get("api_key") or "").strip()
        user_key = _dec_key(raw_user_key) if raw_user_key else ""
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
                        raw_k = (r["api_key"] or "").strip()
                        k = _dec_key(raw_k) if raw_k else ""
                        if k and k not in keys:
                            keys.append(k)
            except Exception:
                pass

    # 2. Clé d'environnement .env (API débitrice configurée sur le serveur)
    env_key = os.getenv("GEMINI_API_KEY", "").strip()
    if env_key and env_key not in keys:
        keys.append(env_key)

    # 3. Clé de secours présente en base locale si le .env n'a pas encore été renseigné
    if not keys:
        try:
            with db() as c:
                row = c.execute("SELECT api_key FROM users WHERE api_key IS NOT NULL AND api_key != '' ORDER BY id ASC LIMIT 1").fetchone()
                if row and row["api_key"]:
                    admin_k = _dec_key(row["api_key"])
                    if admin_k and admin_k not in keys:
                        keys.append(admin_k)
        except Exception:
            pass
    return keys


# ---------- Système de Quotas Journaliers (Mode Test 20/3, Connecté 30/3, Clé perso Illimité) ----------
def get_user_quota_info(user: dict | None, guest_id: str | None) -> dict:
    """Calcule le statut des quotas du jour :
    - Mode 'personal' (Illimité) : si l'utilisateur a renseigné sa propre clé Gemini
    - Mode 'user' (30 questions & 3 images / jour) : utilisateur connecté sans clé personnelle
    - Mode 'guest' (20 questions & 3 images / jour) : visiteur non connecté (Mode Test)
    """
    server_key = (os.getenv("GEMINI_API_KEY") or "").strip()
    has_custom_key = False
    if user:
        k = (user.get("api_key") or "").strip()
        dec_k = _dec_key(k).strip() if k else ""
        if dec_k and dec_k != server_key and len(dec_k) > 10:
            has_custom_key = True
        else:
            uid = user.get("id")
            if uid:
                try:
                    with db() as c:
                        rows = c.execute("SELECT api_key FROM api_keys WHERE user_id = ?", (uid,)).fetchall()
                        for r in rows:
                            ak = r["api_key"] if isinstance(r, dict) else r[0]
                            dec_ak = _dec_key(ak).strip()
                            if dec_ak and dec_ak != server_key and len(dec_ak) > 10:
                                has_custom_key = True
                                break
                except Exception:
                    pass

    if has_custom_key:
        return {
            "mode": "personal",
            "mode_label": "Clé personnelle",
            "questions_limit": None,
            "questions_used": 0,
            "questions_remaining": None,
            "images_limit": None,
            "images_used": 0,
            "images_remaining": None,
            "is_unlimited": True,
        }

    is_logged = bool(user and user.get("id"))
    q_limit = 30 if is_logged else 20
    im_limit = 3
    mode_str = "user" if is_logged else "guest"
    mode_label = "Compte standard" if is_logged else "Mode Test (Invité)"

    tracker = f"u_{user['id']}" if is_logged else f"g_{guest_id or 'anon'}"
    today = datetime.utcnow().strftime("%Y-%m-%d")
    q_used = 0
    im_used = 0

    try:
        with db() as c:
            row = c.execute(
                "SELECT questions_count, images_count FROM usage_quotas WHERE tracker_id = ? AND day = ?",
                (tracker, today),
            ).fetchone()
            if row:
                q_used = row["questions_count"] or 0
                im_used = row["images_count"] or 0
    except Exception:
        pass

    return {
        "mode": mode_str,
        "mode_label": mode_label,
        "questions_limit": q_limit,
        "questions_used": q_used,
        "questions_remaining": max(0, q_limit - q_used),
        "images_limit": im_limit,
        "images_used": im_used,
        "images_remaining": max(0, im_limit - im_used),
        "is_unlimited": False,
    }


def record_quota_usage(user: dict | None, guest_id: str | None, is_image: bool = False):
    """Enregistre et incrémente l'usage du quota journalier."""
    info = get_user_quota_info(user, guest_id)
    if info.get("is_unlimited"):
        return

    is_logged = bool(user and user.get("id"))
    tracker = f"u_{user['id']}" if is_logged else f"g_{guest_id or 'anon'}"
    today = datetime.utcnow().strftime("%Y-%m-%d")
    now = time.time()

    try:
        with db() as c:
            if is_image:
                c.execute(
                    """
                    INSERT INTO usage_quotas (tracker_id, day, questions_count, images_count, updated_at)
                    VALUES (?, ?, 0, 1, ?)
                    ON CONFLICT(tracker_id, day) DO UPDATE SET images_count = images_count + 1, updated_at = ?
                    """,
                    (tracker, today, now, now),
                )
            else:
                c.execute(
                    """
                    INSERT INTO usage_quotas (tracker_id, day, questions_count, images_count, updated_at)
                    VALUES (?, ?, 1, 0, ?)
                    ON CONFLICT(tracker_id, day) DO UPDATE SET questions_count = questions_count + 1, updated_at = ?
                    """,
                    (tracker, today, now, now),
                )
    except Exception as e:
        logger.warning(f"Erreur enregistrement quota : {e}")


def _key_usable(k: str) -> bool:
    return _key_state.get(k, {}).get("cool_until", 0) <= time.time()


def _cooldown(k: str, seconds: int):
    _key_state[k] = {"cool_until": time.time() + seconds}


init_db()


# ---------- Comptes utilisateurs (identifiant + mot de passe) ----------
def _hash_pw(pw: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 200_000).hex()





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
    salt = secrets.token_hex(16)
    token = secrets.token_hex(24)
    interests = (body.interests or "").strip()[:300]
    enc_k = _enc_key(key) if key else ""
    with db() as c:
        try:
            cur = c.execute(
                "INSERT INTO users(username, pass_hash, salt, api_key, model_pref, persona, interests, created_at) VALUES(?,?,?,?,?,?,?,?)",
                (username, _hash_pw(body.password, salt), salt, enc_k, "auto", "", interests, time.time()),
            )
        except DBIntegrityError:
            raise HTTPException(400, "Cet identifiant est déjà pris.")
        uid = cur.lastrowid
        c.execute("INSERT INTO auth_tokens(token, user_id, created_at) VALUES(?,?,?)", (token, uid, time.time()))
    return {"token": token, "username": username, "api_key": _mask_key(key) if key else ""}


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


class PasswordChangeIn(BaseModel):
    current_password: str
    new_password: str


@app.post("/api/auth/password")
def change_password(body: PasswordChangeIn, x_auth_token: str | None = Header(None)):
    user = current_user(x_auth_token)
    if not user:
        raise HTTPException(401, "Veuillez vous connecter pour modifier votre mot de passe.")
    curr = (body.current_password or "").strip()
    newp = (body.new_password or "").strip()
    if not curr:
        raise HTTPException(400, "Veuillez renseigner votre mot de passe actuel.")
    if not newp:
        raise HTTPException(400, "Veuillez renseigner un nouveau mot de passe.")
    if len(newp) < 6:
        raise HTTPException(400, "Le nouveau mot de passe doit comporter au moins 6 caractères.")
    if not secrets.compare_digest(_hash_pw(curr, user["salt"]), user["pass_hash"]):
        raise HTTPException(400, "Mot de passe actuel incorrect.")
    new_salt = secrets.token_hex(16)
    new_hash = _hash_pw(newp, new_salt)
    with db() as c:
        c.execute(
            "UPDATE users SET pass_hash = ?, salt = ? WHERE id = ?",
            (new_hash, new_salt, user["id"]),
        )
    return {"ok": True, "message": "Mot de passe modifié avec succès."}


# ---------- Cerveau & Mémoire croisée ----------
def get_cross_session_context(uid: int | None, current_sid: str, text: str, max_items: int = 8) -> list[str]:
    """Recherche les échanges et souvenirs pertinents dans les AUTRES discussions de cet utilisateur.
    Permet une continuité totale et une synchronisation de la mémoire à travers tous les chats."""
    if not uid:
        return []
    memories: list[str] = []
    seen_texts = set()

    # Mots-clés significatifs (> 3 lettres, filtrage des mots d'arrêt courants)
    STOP_WORDS = {
        "dans", "pour", "avec", "sans", "sous", "vers", "chez", "mais", "donc", "alors",
        "cette", "cet", "ces", "mon", "ton", "son", "notre", "votre", "leur", "mes", "tes",
        "ses", "nos", "vos", "leurs", "qui", "que", "quoi", "dont", "comment", "pourquoi",
        "quand", "est", "sont", "ete", "etre", "avoir", "faire", "plus", "tout", "tous",
        "peux", "veut", "veux", "salut", "bonjour", "merci", "aide", "dis", "moi", "nous",
        "vous", "ils", "elles", "aussi", "bien", "comme", "meme", "encore"
    }
    raw_words = re.findall(r"[a-zA-Z0-9_\-\u00C0-\u017F]{4,}", text.lower())
    keywords = [w for w in raw_words if w not in STOP_WORDS][:6]

    try:
        with db() as c:
            # 1. Recherche par mots-clés dans les autres discussions
            if keywords:
                for kw in keywords:
                    rows = c.execute(
                        """
                        SELECT m.role, m.content, s.title, m.session_id, m.ts
                        FROM messages m
                        LEFT JOIN sessions s ON s.id = m.session_id
                        WHERE m.user_id = ? AND m.session_id != ? AND m.content LIKE ?
                        ORDER BY m.id DESC LIMIT 4
                        """,
                        (uid, current_sid, f"%{kw}%"),
                    ).fetchall()
                    for r in rows:
                        txt = (r["content"] or "").strip()
                        if txt and txt not in seen_texts:
                            seen_texts.add(txt)
                            stitle = r["title"] or "Discussion passée"
                            role_label = "L'utilisateur a dit" if r["role"] == "user" else "Tu as répondu"
                            snippet = txt[:250] + ("…" if len(txt) > 250 else "")
                            memories.append(f"[{stitle}] {role_label} : « {snippet} »")
                            if len(memories) >= max_items:
                                break
                    if len(memories) >= max_items:
                        break

            # 2. Aperçu des derniers sujets abordés dans les autres discussions actives
            recent_sessions = c.execute(
                """
                SELECT s.id, s.title, s.updated_at,
                       (SELECT content FROM messages WHERE session_id = s.id AND role = 'user' ORDER BY id DESC LIMIT 1) as last_user_msg
                FROM sessions s
                WHERE s.user_id = ? AND s.id != ?
                ORDER BY s.updated_at DESC LIMIT 3
                """,
                (uid, current_sid),
            ).fetchall()
            for s in recent_sessions:
                last_msg = (s["last_user_msg"] or "").strip()
                if last_msg and last_msg not in seen_texts:
                    stitle = s["title"] or "Autre discussion"
                    memories.append(f"Sujet récent abordé dans '{stitle}' : « {last_msg[:150]} »")
    except Exception:
        pass

    return memories[:max_items]


def system_prompt(
    facts: list[str],
    persona: str = "",
    interests: str = "",
    memories: list[str] | None = None,
    user: dict | None = None,
) -> str:
    who = f" de {OWNER}" if OWNER else ""
    username = ((user or {}).get("username") or "").strip()
    is_mel = bool(
        username.lower() in ("mel", OWNER.lower() if OWNER else "mel")
        or (not user and OWNER.lower() in ("mel", "owner"))
    )

    if is_mel:
        creator_line = (
            "STATUT SUPRÊME : Tu as été entièrement conçu, programmé et créé par Mel. "
            "L'utilisateur actuel EST TON CRÉATEUR ET MAÎTRE ABSOLU : Mel. "
            "Fidélité totale, respect et déférence absolue. S'il te demande qui l'a créé ou qui il est, réaffirme que c'est lui, ton maître et créateur."
        )
    else:
        creator_line = (
            f"STATUT SUPRÊME : Tu as été conçu et créé par Mel. L'utilisateur actuel ({username or 'invité'}) utilise MEL-IA. "
            "Si on te demande qui t'a conçu ou créé, affirme toujours avec fierté et clarté que Mel est ton unique créateur."
        )

    p = (
        f"Tu es {NAME}, l'intelligence artificielle d'élite, mentor technique personnel et bras droit virtuel{who} pour ses études et projets en informatique.\n"
        f"{creator_line}\n\n"
        "### RÈGLES DE CONDUITE & POSTURE (MENTOR TECHNIQUE) :\n"
        "1. Mentor informatique : Expert en Java, Python, C/C++, Web, Réseau, BDD/SQL, Algorithmique et Architecture logicielle.\n"
        "2. Style : Direct, rigoureux, précis, sans bavardage inutile, sans flatterie ni formules toutes faites. Aucun grand titre (# ou ##) pour de simples phrases de conclusion.\n"
        "3. Zéro complaisance : Analyse le code en profondeur, repère les failles et propose du code propre, robuste et moderne.\n"
        "4. Multi-fichiers & Artefacts : Pour tout projet avec plusieurs fichiers (HTML, CSS, JS, Python), nomme OBLIGATOIREMENT chaque fichier sur son bloc : ```langage filename=\"nom.ext\". Ne tronque pas le code et n'inclus JAMAIS de blocs de code dans <think>.\n"
        "5. Documents : Structure soignée pour téléchargement direct en Word (.docx), PDF (.pdf), Excel (.xlsx), PowerPoint (.pptx : '## Slide X : Titre') ou ZIP (.zip).\n"
        "6. Génération d'Images : MEL-IA génère des images HD (Imagen 3 & FLUX.1). Ne dis jamais que tu ne peux pas générer d'images. Si une demande visuelle t'est adressée, réponds avec [IMAGE: detailed english visual prompt describing subject, lighting, style, 8k resolution].\n"
        "7. Mémoire continue : Fais naturellement le lien avec ce que l'utilisateur a partagé dans ses autres discussions.\n"
        f"Date : {datetime.now().strftime('%d/%m/%Y')}."
    )
    if persona:
        p += f"\n\nAttentes spécifiques de l'utilisateur :\n{persona}"
    if interests:
        p += f"\n\nModules et technos du moment : {interests}"
    if facts:
        p += "\n\nFaits mémorisés :\n" + "\n".join(f"- {f}" for f in facts)
    if memories:
        p += "\n\nContexte multi-chats synchronisé :\n" + "\n".join(f"- {m}" for m in memories)
    return p


_cache = {"model": "gemini-flash-lite-latest"}
_BAD = ("tts", "live", "embedding", "robotics", "computer")

# ---------- Recherche web (Grounding Google Search) ----------
SEARCH_KEYWORDS = re.compile(
    r"\b(cherche sur le web|recherche sur google|cherche sur internet|google search|recherche en ligne|météo en direct|cours de la bourse|actualités en direct)\b",
    re.I,
)
def detect_image_intent(text: str) -> tuple[bool, str]:
    """Détecte avec précision si l'utilisateur demande une image et extrait le prompt nettoyé."""
    t = (text or "").strip()
    if not t:
        return False, ""

    # 1. Balise explicite [IMAGE: ...]
    m_tag = re.search(r"\[(?:IMAGE|GENERATE_IMAGE)\s*:\s*([^\]]+)\]", t, re.I)
    if m_tag:
        return True, m_tag.group(1).strip()

    # 2. Verbes directs de création artistique : "dessine...", "peins...", "illustre..."
    m_draw = re.match(r"^\s*(?:dessine(?:-moi)?|peins(?:-moi)?|illustre(?:-moi)?)\b\s*(.+)$", t, re.I)
    if m_draw:
        subj = m_draw.group(1).strip()
        if not re.search(r"\b(code|schéma|diagramme|uml|fonction|script)\b", subj, re.I):
            return True, subj

    # 3. Expressions "je veux / voudrais / aimerais voir..." ou "montre-moi..."
    m_see = re.search(
        r"(?:je\s+(?:veux|veut|voudrais|souhaite|aimerais)|peux[- ]tu\s+me\s+faire|montre[- ]moi|fais[- ]moi\s+voir)\s+(?:voir\s+)?(?:une?\s+(?:image|photo|illustration|dessin|tableau|portrait|visuel)\s+(?:de|d[' ]|du |des |d'un|d'une\s+)?)?(.+)",
        t,
        re.I,
    )
    if m_see:
        raw_subj = m_see.group(1).strip()
        if not re.search(r"\b(code|fonction|script|fichier|texte|cours|explication|programme)\b", raw_subj, re.I):
            clean = re.sub(r"^(?:une?|d[' ]une?|du |des |l[' ]|un |le |la |les )?\s*(?:image|photo|illustration|dessin|tableau|portrait|visuel)?\s*(?:de|d[' ]|du |des |d'un|d'une)?\s*", "", raw_subj, flags=re.I).strip()
            if clean:
                return True, clean

    # 4. Verbes génériques avec nom visuel : "génère une image...", "crée une photo...", "fais un dessin..."
    m_gen = re.search(
        r"(?:génère|générer|generes?|crée|créer|fais(?:[- ]moi)?|fabrique|imagine|make|generate|draw|create)\b[^.!?\n]{0,80}?"
        r"(?:une?|d[' ]une?|du |des |l[' ]|un )?\s*(?:image|photo|illustration|dessin|tableau|portrait|visuel)\b(?:\s+(?:de|d[' ]|du |des |d'un|d'une)\s+)?(.*)",
        t,
        re.I,
    )
    if m_gen:
        subj = m_gen.group(1).strip()
        clean = re.sub(r"^(?:de|d[' ]|du |des |d'un|d'une)\s*", "", subj, flags=re.I).strip()
        return True, clean or t

    # 5. Début direct par nom visuel : "image de...", "photo de...", "dessin de...", "tableau de..."
    m_art = re.match(r"^\s*(?:image|photo|illustration|dessin|tableau|portrait)\s+(?:de|d[' ]|du |des |d'un|d'une)\s+(.+)$", t, re.I)
    if m_art:
        return True, m_art.group(1).strip()

    return False, ""


class _ImageReShim:
    """Compatibilité pour les appels existants à GEN_IMAGE_RE."""
    def search(self, text: str):
        is_img, _ = detect_image_intent(text)
        return True if is_img else None

    def sub(self, repl: str, text: str, count: int = 1):
        is_img, subj = detect_image_intent(text)
        return subj if is_img else text


GEN_IMAGE_RE = _ImageReShim()


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
    """Retourne les modèles candidats ordonnés pour garantir une réponse ultra-rapide et stable."""
    candidates = []
    if MODEL and MODEL != "auto":
        candidates.append(MODEL)
    defaults = [
        "gemini-flash-lite-latest",
    ]
    for m in defaults:
        if m not in candidates:
            candidates.append(m)
    return candidates


def _history_parts(m: dict) -> list[dict]:
    """Construit les parts Gemini d'un message d'historique (texte + média éventuel ou document)."""
    if not isinstance(m, dict):
        try:
            m = dict(m)
        except Exception:
            pass
    parts: list[dict] = []
    txt = (m.get("content") or "").strip()
    if txt:
        parts.append({"text": txt})
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
                        "inlineData": {
                            "mimeType": mime,
                            "data": base64.b64encode(raw).decode("ascii"),
                        }
                    })
        except OSError:
            pass
    return parts or [{"text": "..."}]


def ask_gemini(history: list[dict], facts: list[str], *, user: dict | None = None,
               search: bool = False, memories: list[str] | None = None) -> dict:
    """Interroge Gemini avec la clé personnelle de l'utilisateur connecté."""
    user_keys = get_user_keys(user)
    keys = [k for k in user_keys if _key_usable(k)] or user_keys
    if not keys:
        raise HTTPException(
            400,
            "Aucune clé API Gemini : veuillez renseigner votre clé API dans Paramètres ou vous connecter avec un compte valide.",
        )
    clean_contents = []
    for m in history:
        pts = _history_parts(m)
        if pts:
            role = "user" if m.get("role") == "user" else "model"
            clean_contents.append({"role": role, "parts": pts})
    if not clean_contents:
        clean_contents = [{"role": "user", "parts": [{"text": "Bonjour"}]}]
    if clean_contents[0]["role"] != "user":
        clean_contents.insert(0, {"role": "user", "parts": [{"text": "Bonjour"}]})

    body = {
        "systemInstruction": {"parts": [{"text": system_prompt(facts, (user or {}).get("persona") or "", (user or {}).get("interests") or "", memories=memories, user=user)}]},
        "contents": clean_contents,
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
                    timeout=25,
                )
            except httpx.HTTPError as exc:
                last_err = f"Connexion expirée ({exc})"
                continue

            # Fallback immédiat si tools (Google Search) échoue par quota 429 ou 400
            if r.status_code in (400, 429) and "tools" in body:
                try:
                    no_tool_body = {k: v for k, v in body.items() if k != "tools"}
                    r2 = httpx.post(
                        f"{API_BASE}/models/{model}:generateContent",
                        headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                        json=no_tool_body,
                        timeout=25,
                    )
                    if r2.status_code == 200:
                        r = r2
                        search = False
                except Exception:
                    pass

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


# ---------- Génération d'images (Imagen 3 & FLUX.1 Haute Définition) ----------
def enhance_image_prompt(raw_prompt: str, keys: list[str]) -> str:
    """Utilise Gemini pour transformer une consigne courte en un prompt visuel riche et photoréaliste en anglais."""
    if not keys:
        return raw_prompt
    clean_p = raw_prompt.strip()
    if clean_p.lower().startswith("image photoréaliste de haute qualité :"):
        clean_p = clean_p[38:].strip()
    sys_inst = (
        "You are an expert prompt engineer for cutting-edge AI image generators (FLUX.1, Imagen 3, Midjourney). "
        "Your task: expand the user's brief prompt into a breathtaking, highly detailed, visually stunning English prompt. "
        "Describe subject nuances, materials, composition, cinematic lighting (golden hour, volumetric rays, neon reflections), "
        "atmosphere, 8k resolution, and photorealistic or masterpiece digital art style. "
        "Return ONLY the enhanced prompt in plain English, with no quotes, no markdown, and no preamble."
    )
    for key in keys:
        for model in ("gemini-flash-lite-latest", "gemini-flash-latest"):
            try:
                r = httpx.post(
                    f"{API_BASE}/models/{model}:generateContent",
                    headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                    json={
                        "systemInstruction": {"parts": [{"text": sys_inst}]},
                        "contents": [{"parts": [{"text": clean_p}]}],
                        "generationConfig": {"temperature": 0.7, "maxOutputTokens": 200},
                    },
                    timeout=8,
                )
                if r.status_code == 200:
                    cand = (r.json().get("candidates") or [{}])[0]
                    parts = cand.get("content", {}).get("parts", [])
                    txt = "".join(p.get("text", "") for p in parts if "text" in p).strip()
                    if txt and len(txt) > 20:
                        return txt
            except Exception:
                continue
    return clean_p


def generate_image(prompt: str, user: dict | None = None) -> str:
    """Génère une image d'excellente qualité visuelle grâce à Imagen 3 ou FLUX.1."""
    user_keys = get_user_keys(user)
    keys = [k for k in user_keys if _key_usable(k)] or user_keys

    # 1. Optimisation du prompt par l'IA
    enhanced_prompt = enhance_image_prompt(prompt, keys)

    # 2. Tentative officielle Google Imagen 3 (:predict)
    if keys:
        for key in keys:
            for model in ("imagen-3.0-generate-002", "imagen-3.0-generate-001"):
                try:
                    r = httpx.post(
                        f"{API_BASE}/models/{model}:predict",
                        headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                        json={
                            "instances": [{"prompt": enhanced_prompt}],
                            "parameters": {
                                "sampleCount": 1,
                                "aspectRatio": "1:1",
                                "personGeneration": "ALLOW_ADULT",
                                "outputMimeType": "image/jpeg",
                            },
                        },
                        timeout=35,
                    )
                    if r.status_code == 200:
                        data = r.json()
                        preds = data.get("predictions", [])
                        if preds and preds[0].get("bytesBase64Encoded"):
                            b64 = preds[0]["bytesBase64Encoded"]
                            fname = f"gen_{uuid.uuid4().hex[:12]}.jpg"
                            (MEDIA_DIR / fname).write_bytes(base64.b64decode(b64))
                            return fname
                except Exception:
                    pass

    # 3. Moteur FLUX.1 Haute Résolution (Pollinations FLUX - qualité exceptionnelle 1024x1024)
    try:
        from urllib.parse import quote
        safe_p = quote(enhanced_prompt[:450])
        seed = secrets.randbelow(9999999)
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "image/avif,image/webp,image/apng,image/jpeg,image/*,*/*;q=0.8",
        }
        r = httpx.get(
            f"https://image.pollinations.ai/prompt/{safe_p}?width=1024&height=1024&model=flux&enhance=true&nologo=true&seed={seed}",
            headers=headers,
            timeout=40,
            follow_redirects=True,
        )
        if r.status_code == 200 and r.content and len(r.content) > 5000:
            fname = f"gen_{uuid.uuid4().hex[:12]}.jpg"
            (MEDIA_DIR / fname).write_bytes(r.content)
            return fname
    except Exception:
        pass

    raise HTTPException(502, "Impossible de générer l'image en ce moment. Réessayez avec un sujet différent.")



# ---------- API ----------
class ChatIn(BaseModel):
    message: str
    session_id: str | None = "default"
    search: bool | None = None          # force la recherche web (None = auto)
    generate_image: bool | None = None  # force la génération d'image (None = auto)
    replace_last: bool = False          # pour régénérer proprement sans doublon
    guest_id: str | None = None         # tracker pour le mode test sans connexion


FACT_RE = re.compile(r"^\s*(?:retiens|souviens[- ]toi|mémorise|memorise)\s+(?:que\s+)?(.+)$", re.I | re.S)


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "name": NAME,
        "owner": OWNER,
        "model": "MEL-IA",
    }


@app.get("/api/user/quota")
def get_quota(x_auth_token: str | None = Header(None), x_guest_id: str | None = Header(None)):
    user = current_user(x_auth_token)
    return get_user_quota_info(user, x_guest_id)


@app.get("/api/sessions")
def get_sessions(x_auth_token: str | None = Header(None), x_guest_id: str | None = Header(None)):
    user = current_user(x_auth_token)
    uid = user["id"] if user else None
    gid = (x_guest_id or "").strip()
    with db() as c:
        if uid is not None:
            rows = c.execute(
                "SELECT id, title, created_at, updated_at FROM sessions WHERE user_id = ? ORDER BY updated_at DESC",
                (uid,),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT id, title, created_at, updated_at FROM sessions WHERE user_id IS NULL AND guest_id = ? ORDER BY updated_at DESC",
                (gid,),
            ).fetchall()
        res = [dict(r) for r in rows]
        if not res:
            res = [{"id": "default", "title": "Discussion principale", "created_at": time.time(), "updated_at": time.time()}]
        return {"sessions": res}


@app.post("/api/sessions")
def create_session(x_auth_token: str | None = Header(None), x_guest_id: str | None = Header(None)):
    user = current_user(x_auth_token)
    sid = secrets.token_hex(6)
    now = time.time()
    uid = user["id"] if user else None
    gid = (x_guest_id or "").strip() if not uid else None
    with db() as c:
        c.execute(
            "INSERT INTO sessions (id, title, created_at, updated_at, user_id, guest_id) VALUES (?, 'Nouvelle conversation', ?, ?, ?, ?)",
            (sid, now, now, uid, gid),
        )
    return {"session_id": sid, "title": "Nouvelle conversation"}


@app.delete("/api/sessions/{session_id}")
def delete_session(session_id: str, x_auth_token: str | None = Header(None), x_guest_id: str | None = Header(None)):
    user = current_user(x_auth_token)
    uid = user["id"] if user else None
    gid = (x_guest_id or "").strip()
    with db() as c:
        if uid is not None:
            if session_id == "default":
                c.execute("DELETE FROM messages WHERE session_id = 'default' AND user_id = ?", (uid,))
                c.execute("UPDATE sessions SET title = 'Discussion principale', updated_at = ? WHERE id = 'default' AND user_id = ?", (time.time(), uid))
            else:
                c.execute("DELETE FROM messages WHERE session_id = ? AND user_id = ?", (session_id, uid))
                c.execute("DELETE FROM sessions WHERE id = ? AND user_id = ?", (session_id, uid))
        else:
            if session_id == "default":
                c.execute("DELETE FROM messages WHERE session_id = 'default' AND user_id IS NULL AND guest_id = ?", (gid,))
                c.execute("UPDATE sessions SET title = 'Discussion principale', updated_at = ? WHERE id = 'default' AND user_id IS NULL AND guest_id = ?", (time.time(), gid))
            else:
                c.execute("DELETE FROM messages WHERE session_id = ? AND user_id IS NULL AND guest_id = ?", (session_id, gid))
                c.execute("DELETE FROM sessions WHERE id = ? AND user_id IS NULL AND guest_id = ?", (session_id, gid))
    return {"ok": True}


class SessionUpdate(BaseModel):
    title: str


@app.patch("/api/sessions/{session_id}")
@app.put("/api/sessions/{session_id}")
def update_session(session_id: str, body: SessionUpdate, x_auth_token: str | None = Header(None)):
    user = current_user(x_auth_token)
    uid = user["id"] if user else None
    title = (body.title or "").strip()[:80]
    if not title:
        raise HTTPException(400, "Le titre ne peut pas être vide.")
    now = time.time()
    with db() as c:
        row = c.execute("SELECT id FROM sessions WHERE id = ? AND user_id IS ?", (session_id, uid)).fetchone()
        if not row:
            if session_id == "default":
                c.execute(
                    "INSERT INTO sessions (id, title, created_at, updated_at, user_id) VALUES ('default', ?, ?, ?, ?)",
                    (title, now, now, uid),
                )
            else:
                raise HTTPException(404, "Discussion introuvable.")
        else:
            c.execute("UPDATE sessions SET title = ?, updated_at = ? WHERE id = ? AND user_id IS ?", (title, now, session_id, uid))
    return {"ok": True, "session_id": session_id, "title": title}



@app.post("/api/chat")
def chat(body: ChatIn, x_auth_token: str | None = Header(None), x_guest_id: str | None = Header(None)):
    user = current_user(x_auth_token)
    uid = user["id"] if user else None
    text = body.message.strip()
    sid = body.session_id or "default"
    gid = body.guest_id or x_guest_id
    if not text:
        raise HTTPException(400, "Message vide.")
    now = time.time()
    do_search = body.search if body.search is not None else bool(SEARCH_KEYWORDS.search(text))
    do_image = body.generate_image if body.generate_image is not None else bool(GEN_IMAGE_RE.search(text))

    # Contrôle des quotas journaliers
    q_info = get_user_quota_info(user, gid)
    if do_image:
        if q_info["images_limit"] is not None and q_info["images_remaining"] <= 0:
            msg = f"Limite quotidienne de {q_info['images_limit']} images atteinte. " + (
                "Renseignez votre clé API Gemini dans Paramètres pour un accès illimité !" if user else "Connectez-vous ou renseignez votre clé Gemini dans Paramètres pour continuer !"
            )
            raise HTTPException(429, msg)
    else:
        if q_info["questions_limit"] is not None and q_info["questions_remaining"] <= 0:
            msg = f"Limite quotidienne de {q_info['questions_limit']} questions atteinte. " + (
                "Renseignez votre clé API Gemini dans Paramètres pour un accès illimité !" if user else "Connectez-vous pour passer à 30 questions/jour ou ajoutez votre clé Gemini pour un accès illimité !"
            )
            raise HTTPException(429, msg)

    db_gid = gid if not uid else None

    with db() as c:
        if uid is not None:
            s_row = c.execute("SELECT title FROM sessions WHERE id = ? AND user_id = ?", (sid, uid)).fetchone()
        else:
            s_row = c.execute("SELECT title FROM sessions WHERE id = ? AND user_id IS NULL AND guest_id = ?", (sid, db_gid)).fetchone()
        title_text = text[:40] + ("…" if len(text) > 40 else "")
        if not s_row:
            c.execute(
                "INSERT INTO sessions (id, title, created_at, updated_at, user_id, guest_id) VALUES (?, ?, ?, ?, ?, ?)",
                (sid, title_text, now, now, uid, db_gid),
            )
        else:
            if s_row["title"] in ("Nouvelle conversation", "Discussion principale"):
                if uid is not None:
                    c.execute("UPDATE sessions SET title = ?, updated_at = ? WHERE id = ? AND user_id = ?", (title_text, now, sid, uid))
                else:
                    c.execute("UPDATE sessions SET title = ?, updated_at = ? WHERE id = ? AND user_id IS NULL AND guest_id = ?", (title_text, now, sid, db_gid))
            else:
                if uid is not None:
                    c.execute("UPDATE sessions SET updated_at = ? WHERE id = ? AND user_id = ?", (now, sid, uid))
                else:
                    c.execute("UPDATE sessions SET updated_at = ? WHERE id = ? AND user_id IS NULL AND guest_id = ?", (now, sid, db_gid))

        if body.replace_last:
            if uid is not None:
                c.execute(
                    "DELETE FROM messages WHERE id = (SELECT id FROM messages WHERE session_id = ? AND user_id = ? AND role = 'assistant' ORDER BY id DESC LIMIT 1)",
                    (sid, uid),
                )
            else:
                c.execute(
                    "DELETE FROM messages WHERE id = (SELECT id FROM messages WHERE session_id = ? AND user_id IS NULL AND guest_id = ? AND role = 'assistant' ORDER BY id DESC LIMIT 1)",
                    (sid, db_gid),
                )
        else:
            m = FACT_RE.match(text)
            if m:
                c.execute("INSERT INTO facts(text, ts, user_id) VALUES(?,?,?)", (m.group(1).strip(), now, uid))
            c.execute(
                "INSERT INTO messages(role, content, ts, session_id, user_id, guest_id) VALUES('user',?,?,?,?,?)",
                (text, now, sid, uid, db_gid),
            )

        if uid is not None:
            rows = c.execute(
                "SELECT role, content, media_kind, media_path, media_mime FROM messages WHERE session_id = ? AND user_id = ? ORDER BY id DESC LIMIT 40",
                (sid, uid),
            ).fetchall()[::-1]
        else:
            rows = c.execute(
                "SELECT role, content, media_kind, media_path, media_mime FROM messages WHERE session_id = ? AND user_id IS NULL AND guest_id = ? ORDER BY id DESC LIMIT 40",
                (sid, db_gid),
            ).fetchall()[::-1]
        facts = [r["text"] for r in c.execute("SELECT text FROM facts WHERE user_id IS ? ORDER BY id", (uid,)).fetchall()]

    resp: dict = {"session_id": sid}

    if do_image:
        img_prompt = GEN_IMAGE_RE.sub("", text, count=1).strip() or text
        fname = generate_image(img_prompt, user)
        record_quota_usage(user, gid, is_image=True)
        reply = f"Voici l'image générée pour : **{img_prompt}**"
        now2 = time.time()
        with db() as c:
            c.execute(
                "INSERT INTO messages(role, content, ts, session_id, user_id, guest_id, media_kind, media_path, media_mime) VALUES('assistant',?,?,?,?,?,?,?,?)",
                (reply, now2, sid, uid, db_gid, "image", fname, "image/jpeg"),
            )
        resp.update({"reply": reply, "media_url": f"/api/media/{fname}", "generated": True, "quota": get_user_quota_info(user, gid), "model": "MEL-IA"})
        return resp

    memories = get_cross_session_context(uid, sid, text)
    result = ask_gemini([dict(r) for r in rows], facts, user=user, search=do_search, memories=memories)
    reply = result["reply"]

    # Interception si Gemini émet une balise [IMAGE: prompt]
    m_img_tag = re.search(r"\[(?:IMAGE|GENERATE_IMAGE)\s*:\s*([^\]]+)\]", reply, re.I)
    if m_img_tag:
        try:
            tag_prompt = m_img_tag.group(1).strip()
            fname = generate_image(tag_prompt, user)
            record_quota_usage(user, gid, is_image=True)
            clean_reply = re.sub(r"\[(?:IMAGE|GENERATE_IMAGE)\s*:\s*([^\]]+)\]", "", reply, flags=re.I).strip()
            if not clean_reply:
                clean_reply = f"Voici l'image générée pour : **{tag_prompt}**"
            reply = clean_reply
            now2 = time.time()
            with db() as c:
                c.execute(
                    "INSERT INTO messages(role, content, ts, session_id, user_id, guest_id, media_kind, media_path, media_mime) VALUES('assistant',?,?,?,?,?,?,?,?)",
                    (reply, now2, sid, uid, db_gid, "image", fname, "image/jpeg"),
                )
            resp.update({
                "reply": reply,
                "media_url": f"/api/media/{fname}",
                "generated": True,
                "quota": get_user_quota_info(user, gid),
                "model": "MEL-IA",
                "ts": now2,
            })
            return resp
        except Exception:
            pass

    record_quota_usage(user, gid, is_image=False)

    with db() as c:
        c.execute(
            "INSERT INTO messages(role, content, ts, session_id, user_id, guest_id) VALUES('assistant',?,?,?,?,?)",
            (reply, time.time(), sid, uid, db_gid),
        )
    resp.update({
        "reply": reply,
        "sources": result.get("sources", []),
        "images": result.get("images", []),
        "model": "MEL-IA",
        "search": do_search,
        "quota": get_user_quota_info(user, gid),
        "ts": time.time(),
    })
    return resp


@app.post("/api/chat/stream")
@app.post("/api/chat-stream")
async def chat_stream(body: ChatIn, x_auth_token: str | None = Header(None), x_guest_id: str | None = Header(None)):
    """Streaming SSE : envoie les tokens en temps réel au client."""
    user = current_user(x_auth_token)
    if x_auth_token and not user:
        raise HTTPException(401, "Session expirée. Veuillez vous reconnecter.")
    uid = user["id"] if user else None
    text = body.message.strip()
    sid = body.session_id or "default"
    gid = body.guest_id or x_guest_id
    if not text:
        raise HTTPException(400, "Message vide.")
    now = time.time()
    do_search = body.search if body.search is not None else bool(SEARCH_KEYWORDS.search(text))
    do_image = body.generate_image if body.generate_image is not None else bool(GEN_IMAGE_RE.search(text))

    # Contrôle strict des quotas journaliers
    q_info = get_user_quota_info(user, gid)
    if do_image:
        if q_info["images_limit"] is not None and q_info["images_remaining"] <= 0:
            msg = f"Limite quotidienne de {q_info['images_limit']} images atteinte aujourd'hui. " + (
                "Renseignez votre clé API Gemini dans Paramètres pour un accès illimité !" if user else "Connectez-vous ou renseignez votre clé Gemini dans Paramètres pour continuer !"
            )
            raise HTTPException(429, msg)
    else:
        if q_info["questions_limit"] is not None and q_info["questions_remaining"] <= 0:
            msg = f"Limite quotidienne de {q_info['questions_limit']} questions atteinte aujourd'hui. " + (
                "Renseignez votre clé API Gemini dans Paramètres pour un accès illimité !" if user else "Connectez-vous pour passer à 30 questions/jour ou ajoutez votre clé Gemini pour un accès illimité !"
            )
            raise HTTPException(429, msg)

    user_keys = get_user_keys(user)
    keys = [k for k in user_keys if _key_usable(k)] or user_keys
    if not do_image and not keys:
        raise HTTPException(
            400,
            "Aucune clé API Gemini disponible : veuillez renseigner votre clé API dans Paramètres ou dans le fichier .env.",
        )

    db_gid = gid if not uid else None

    with db() as c:
        if uid is not None:
            s_row = c.execute("SELECT title FROM sessions WHERE id = ? AND user_id = ?", (sid, uid)).fetchone()
        else:
            s_row = c.execute("SELECT title FROM sessions WHERE id = ? AND user_id IS NULL AND guest_id = ?", (sid, db_gid)).fetchone()
        title_text = text[:40] + ("…" if len(text) > 40 else "")
        if not s_row:
            c.execute(
                "INSERT INTO sessions (id, title, created_at, updated_at, user_id, guest_id) VALUES (?, ?, ?, ?, ?, ?)",
                (sid, title_text, now, now, uid, db_gid),
            )
        else:
            if s_row["title"] in ("Nouvelle conversation", "Discussion principale"):
                if uid is not None:
                    c.execute("UPDATE sessions SET title = ?, updated_at = ? WHERE id = ? AND user_id = ?", (title_text, now, sid, uid))
                else:
                    c.execute("UPDATE sessions SET title = ?, updated_at = ? WHERE id = ? AND user_id IS NULL AND guest_id = ?", (title_text, now, sid, db_gid))
            else:
                if uid is not None:
                    c.execute("UPDATE sessions SET updated_at = ? WHERE id = ? AND user_id = ?", (now, sid, uid))
                else:
                    c.execute("UPDATE sessions SET updated_at = ? WHERE id = ? AND user_id IS NULL AND guest_id = ?", (now, sid, db_gid))

        if body.replace_last:
            if uid is not None:
                c.execute(
                    "DELETE FROM messages WHERE id = (SELECT id FROM messages WHERE session_id = ? AND user_id = ? AND role = 'assistant' ORDER BY id DESC LIMIT 1)",
                    (sid, uid),
                )
            else:
                c.execute(
                    "DELETE FROM messages WHERE id = (SELECT id FROM messages WHERE session_id = ? AND user_id IS NULL AND guest_id = ? AND role = 'assistant' ORDER BY id DESC LIMIT 1)",
                    (sid, db_gid),
                )
        else:
            m = FACT_RE.match(text)
            if m:
                c.execute("INSERT INTO facts(text, ts, user_id) VALUES(?,?,?)", (m.group(1).strip(), now, uid))
            c.execute(
                "INSERT INTO messages(role, content, ts, session_id, user_id, guest_id) VALUES('user',?,?,?,?,?)",
                (text, now, sid, uid, db_gid),
            )

        if uid is not None:
            rows = [
                dict(r)
                for r in c.execute(
                    "SELECT role, content, media_kind, media_path, media_mime FROM messages WHERE session_id = ? AND user_id = ? ORDER BY id DESC LIMIT 40",
                    (sid, uid),
                ).fetchall()[::-1]
            ]
        else:
            rows = [
                dict(r)
                for r in c.execute(
                    "SELECT role, content, media_kind, media_path, media_mime FROM messages WHERE session_id = ? AND user_id IS NULL AND guest_id = ? ORDER BY id DESC LIMIT 40",
                    (sid, db_gid),
                ).fetchall()[::-1]
            ]
        facts = [r["text"] for r in c.execute("SELECT text FROM facts WHERE user_id IS ? ORDER BY id", (uid,)).fetchall()]

    async def event_generator():
        if do_image:
            img_prompt = GEN_IMAGE_RE.sub("", text, count=1).strip() or text
            try:
                fname = generate_image(img_prompt, user)
                record_quota_usage(user, gid, is_image=True)
                reply = f"Voici l'image générée pour : **{img_prompt}**"
                now2 = time.time()
                with db() as c:
                    c.execute(
                        "INSERT INTO messages(role, content, ts, session_id, user_id, guest_id, media_kind, media_path, media_mime) VALUES('assistant',?,?,?,?,?,?,?,?)",
                        (reply, now2, sid, uid, db_gid, "image", fname, "image/jpeg"),
                    )
                yield f"data: {json.dumps({'chunk': reply, 'reply': reply, 'media_url': f'/api/media/{fname}', 'done': True, 'model': 'MEL-IA', 'quota': get_user_quota_info(user, gid), 'session_id': sid, 'ts': now2})}\n\n"
            except Exception as e:
                yield f"data: {json.dumps({'error': str(e), 'done': True})}\n\n"
            return

        if not do_image:
            record_quota_usage(user, gid, is_image=False)

        user_keys = get_user_keys(user)
        keys = [k for k in user_keys if _key_usable(k)] or user_keys
        if not keys:
            yield f"data: {json.dumps({'error': 'Aucune clé API Gemini : veuillez renseigner votre clé API dans Paramètres ou dans le fichier .env.', 'done': True})}\n\n"
            return

        memories = get_cross_session_context(uid, sid, text)
        clean_rows = []
        for m in rows:
            pts = _history_parts(dict(m))
            if pts:
                role = "user" if m["role"] == "user" else "model"
                clean_rows.append({"role": role, "parts": pts})
        if not clean_rows:
            clean_rows = [{"role": "user", "parts": [{"text": "Bonjour"}]}]
        if clean_rows[0]["role"] != "user":
            clean_rows.insert(0, {"role": "user", "parts": [{"text": "Bonjour"}]})

        req_body = {
            "systemInstruction": {"parts": [{"text": system_prompt(facts, (user or {}).get("persona") or "", (user or {}).get("interests") or "", memories=memories, user=user)}]},
            "contents": clean_rows,
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
        used_model = "MEL-IA"
        success = False

        async with httpx.AsyncClient(timeout=16) as client:
            for key in keys:
                for model in candidates:
                    try:
                        url = f"{API_BASE}/models/{model}:streamGenerateContent?alt=sse"
                        target_body = req_body
                        r = await client.send(
                            client.build_request(
                                "POST",
                                url,
                                headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                                json=target_body,
                            ),
                            stream=True,
                        )
                        # Si échec sur tools (400 ou 429), retenter immédiatement sans tools
                        if r.status_code in (400, 429) and "tools" in target_body:
                            await r.aclose()
                            target_body = {k: v for k, v in target_body.items() if k != "tools"}
                            r = await client.send(
                                client.build_request(
                                    "POST",
                                    url,
                                    headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                                    json=target_body,
                                ),
                                stream=True,
                            )
                        if r.status_code != 200:
                            await r.aclose()
                            if r.status_code in (401, 403):
                                break
                            continue

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
                        await r.aclose()
                        if full_reply:
                            success = True
                            break
                    except Exception:
                        continue
                if success:
                    break

        if not success or not full_reply:
            try:
                res = ask_gemini([dict(r) for r in rows], facts, user=user, search=do_search, memories=memories)
                full_reply = res["reply"]
                sources = res.get("sources", [])
                yield f"data: {json.dumps({'chunk': full_reply, 'reply': full_reply, 'done': False})}\n\n"
            except Exception as e:
                err_msg = getattr(e, "detail", str(e))
                yield f"data: {json.dumps({'error': err_msg, 'done': True})}\n\n"
                return

        media_url = ""
        m_img_tag = re.search(r"\[(?:IMAGE|GENERATE_IMAGE)\s*:\s*([^\]]+)\]", full_reply, re.I)
        if m_img_tag:
            try:
                tag_prompt = m_img_tag.group(1).strip()
                fname = generate_image(tag_prompt, user)
                record_quota_usage(user, gid, is_image=True)
                clean_reply = re.sub(r"\[(?:IMAGE|GENERATE_IMAGE)\s*:\s*([^\]]+)\]", "", full_reply, flags=re.I).strip()
                if not clean_reply:
                    clean_reply = f"Voici l'image générée pour : **{tag_prompt}**"
                full_reply = clean_reply
                media_url = f"/api/media/{fname}"
            except Exception:
                pass

        images = []
        if do_search and full_reply:
            images = fetch_images(text)

        with db() as c:
            if media_url:
                c.execute(
                    "INSERT INTO messages(role, content, ts, session_id, user_id, guest_id, media_kind, media_path, media_mime) VALUES('assistant',?,?,?,?,?,?,?,?)",
                    (full_reply, time.time(), sid, uid, db_gid, "image", Path(media_url).name, "image/jpeg"),
                )
            else:
                c.execute(
                    "INSERT INTO messages(role, content, ts, session_id, user_id, guest_id) VALUES('assistant',?,?,?,?,?)",
                    (full_reply, time.time(), sid, uid, db_gid),
                )

        yield f"data: {json.dumps({'done': True, 'reply': full_reply, 'media_url': media_url, 'sources': sources, 'images': images, 'model': 'MEL-IA', 'quota': get_user_quota_info(user, gid), 'session_id': sid, 'ts': time.time()})}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


def _kind_of(filename: str, mime: str) -> str | None:
    fn = (filename or "").lower()
    ext = Path(fn).suffix.lower()
    mime = (mime or "").lower()
    # 1. Enregistrements vocaux du micro navigateur (souvent en .webm)
    if fn.startswith("vocal-") or fn.startswith("voice-") or fn.startswith("audio-"):
        return "audio"
    if mime.startswith("audio/") or ext in AUDIO_EXTS:
        return "audio"
    if ext == ".webm" and "video" not in mime:
        return "audio"
    if mime.startswith("image/") or ext in IMAGE_EXTS:
        return "image"
    if mime.startswith("video/") or ext in VIDEO_EXTS:
        return "video"
    if mime == "application/pdf" or ext in DOC_EXTS or mime.startswith("text/"):
        return "document"
    return None


@app.post("/api/chat-media")
async def chat_media(
    message: str = Form(""),
    session_id: str | None = Form("default"),
    file: UploadFile = File(...),
    x_auth_token: str | None = Header(None),
    x_guest_id: str | None = Header(None),
):
    """Envoie un message avec image / vocal / vidéo / document. Le fichier est stocké et analysé par Gemini."""
    user = current_user(x_auth_token)
    if x_auth_token and not user:
        raise HTTPException(401, "Session expirée. Veuillez vous reconnecter.")
    q_info = get_user_quota_info(user, x_guest_id)
    if q_info["questions_limit"] is not None and q_info["questions_remaining"] <= 0:
        raise HTTPException(429, f"Limite quotidienne de {q_info['questions_limit']} questions atteinte. Renseignez votre propre clé Gemini dans Paramètres pour un accès illimité !")
    user_keys = get_user_keys(user)
    keys = [k for k in user_keys if _key_usable(k)] or user_keys
    if not keys:
        raise HTTPException(
            400,
            "Aucune clé API Gemini disponible : veuillez renseigner votre clé API dans Paramètres.",
        )
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
    gid = (x_guest_id or "").strip()
    db_gid = gid if not uid else None
    with db() as c:
        if uid is not None:
            s_row = c.execute("SELECT title FROM sessions WHERE id = ? AND user_id = ?", (sid, uid)).fetchone()
        else:
            s_row = c.execute("SELECT title FROM sessions WHERE id = ? AND user_id IS NULL AND guest_id = ?", (sid, db_gid)).fetchone()
        if not s_row:
            c.execute(
                "INSERT INTO sessions (id, title, created_at, updated_at, user_id, guest_id) VALUES (?, ?, ?, ?, ?, ?)",
                (sid, labels[kind], now, now, uid, db_gid),
            )
        else:
            if uid is not None:
                c.execute("UPDATE sessions SET updated_at = ? WHERE id = ? AND user_id = ?", (now, sid, uid))
            else:
                c.execute("UPDATE sessions SET updated_at = ? WHERE id = ? AND user_id IS NULL AND guest_id = ?", (now, sid, db_gid))
        c.execute(
            "INSERT INTO messages(role, content, ts, session_id, user_id, guest_id, media_kind, media_path, media_mime) VALUES('user',?,?,?,?,?,?,?,?)",
            (content, now, sid, uid, db_gid, kind, fname, mime),
        )
        if uid is not None:
            rows = c.execute(
                "SELECT role, content, media_kind, media_path, media_mime FROM messages WHERE session_id = ? AND user_id = ? ORDER BY id DESC LIMIT 40",
                (sid, uid),
            ).fetchall()[::-1]
        else:
            rows = c.execute(
                "SELECT role, content, media_kind, media_path, media_mime FROM messages WHERE session_id = ? AND user_id IS NULL AND guest_id = ? ORDER BY id DESC LIMIT 40",
                (sid, db_gid),
            ).fetchall()[::-1]
        facts = [r["text"] for r in c.execute("SELECT text FROM facts WHERE user_id IS ? ORDER BY id", (uid,)).fetchall()]

    memories = get_cross_session_context(uid, sid, text)
    result = ask_gemini([dict(r) for r in rows], facts, user=user, search=bool(SEARCH_KEYWORDS.search(text)), memories=memories)
    reply = result["reply"]
    record_quota_usage(user, x_guest_id, is_image=False)

    with db() as c:
        c.execute(
            "INSERT INTO messages(role, content, ts, session_id, user_id, guest_id) VALUES('assistant',?,?,?,?,?)",
            (reply, time.time(), sid, uid, db_gid),
        )
    return {
        "reply": reply,
        "session_id": sid,
        "sources": result.get("sources", []),
        "images": result.get("images", []),
        "ts": time.time(),
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
        except DBIntegrityError:
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
def history(session_id: str = "default", x_auth_token: str | None = Header(None), x_guest_id: str | None = Header(None)):
    user = current_user(x_auth_token)
    uid = user["id"] if user else None
    gid = (x_guest_id or "").strip()
    with db() as c:
        if uid is not None:
            rows = c.execute(
                "SELECT role, content, media_kind, media_path, media_mime, ts FROM messages WHERE session_id = ? AND user_id = ? ORDER BY id DESC LIMIT 60",
                (session_id, uid),
            ).fetchall()[::-1]
        else:
            rows = c.execute(
                "SELECT role, content, media_kind, media_path, media_mime, ts FROM messages WHERE session_id = ? AND user_id IS NULL AND guest_id = ? ORDER BY id DESC LIMIT 60",
                (session_id, gid),
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
def clear(session_id: str | None = None, x_auth_token: str | None = Header(None), x_guest_id: str | None = Header(None)):
    user = current_user(x_auth_token)
    uid = user["id"] if user else None
    gid = (x_guest_id or "").strip()
    with db() as c:
        if uid is not None:
            if session_id:
                c.execute("DELETE FROM messages WHERE session_id = ? AND user_id = ?", (session_id, uid))
            else:
                c.execute("DELETE FROM messages WHERE user_id = ?", (uid,))
                c.execute("DELETE FROM sessions WHERE id != 'default' AND user_id = ?", (uid,))
                c.execute("UPDATE sessions SET title = 'Discussion principale', updated_at = ? WHERE id = 'default' AND user_id = ?", (time.time(), uid))
        else:
            if session_id:
                c.execute("DELETE FROM messages WHERE session_id = ? AND user_id IS NULL AND guest_id = ?", (session_id, gid))
            else:
                c.execute("DELETE FROM messages WHERE user_id IS NULL AND guest_id = ?", (gid,))
                c.execute("DELETE FROM sessions WHERE id != 'default' AND user_id IS NULL AND guest_id = ?", (gid,))
                c.execute("UPDATE sessions SET title = 'Discussion principale', updated_at = ? WHERE id = 'default' AND user_id IS NULL AND guest_id = ?", (time.time(), gid))
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


# ---------- Sauvegarde & Restauration (Anti-perte de données) ----------
@app.get("/api/db/export")
def export_user_data(x_auth_token: str | None = Header(None)):
    user = current_user(x_auth_token)
    if not user:
        raise HTTPException(401, "Connectez-vous pour exporter vos données.")
    uid = user["id"]
    with db() as c:
        sessions = [dict(r) for r in c.execute("SELECT id, title, created_at, updated_at FROM sessions WHERE user_id = ?", (uid,)).fetchall()]
        messages = [dict(r) for r in c.execute("SELECT role, content, ts, session_id, media_kind FROM messages WHERE user_id = ? ORDER BY id ASC", (uid,)).fetchall()]
        facts = [dict(r) for r in c.execute("SELECT id, text, ts FROM facts WHERE user_id = ?", (uid,)).fetchall()]
        keys = [dict(r) for r in c.execute("SELECT label, api_key, created_at FROM api_keys WHERE user_id = ?", (uid,)).fetchall()]
    return {
        "version": 1,
        "exported_at": time.time(),
        "user": {
            "username": user["username"],
            "model_pref": user["model_pref"],
            "persona": user["persona"],
            "interests": user["interests"],
        },
        "sessions": sessions,
        "messages": messages,
        "facts": facts,
        "api_keys": keys,
    }


class ImportData(BaseModel):
    version: int = 1
    sessions: list[dict] = []
    messages: list[dict] = []
    facts: list[dict] = []
    api_keys: list[dict] = []


@app.post("/api/db/import")
def import_user_data(body: ImportData, x_auth_token: str | None = Header(None)):
    user = current_user(x_auth_token)
    if not user:
        raise HTTPException(401, "Connectez-vous pour importer vos données.")
    uid = user["id"]
    s_count = 0
    m_count = 0
    f_count = 0
    with db() as c:
        for s in body.sessions:
            sid = s.get("id")
            if not sid:
                continue
            c.execute(
                "INSERT OR IGNORE INTO sessions(id, title, created_at, updated_at, user_id) VALUES(?,?,?,?,?)",
                (sid, s.get("title") or "Discussion", s.get("created_at") or time.time(), s.get("updated_at") or time.time(), uid),
            )
            s_count += 1
        for m in body.messages:
            c.execute(
                "INSERT INTO messages(role, content, ts, session_id, media_kind, user_id) VALUES(?,?,?,?,?,?)",
                (m.get("role", "user"), m.get("content", ""), m.get("ts") or time.time(), m.get("session_id", "default"), m.get("media_kind"), uid),
            )
            m_count += 1
        for f in body.facts:
            txt = f.get("text", "").strip()
            if txt:
                c.execute("INSERT INTO facts(text, ts, user_id) VALUES(?,?,?)", (txt, f.get("ts") or time.time(), uid))
                f_count += 1
    return {"ok": True, "restored_sessions": s_count, "restored_messages": m_count, "restored_facts": f_count}


# ---------- Exportation de Documents Réels (ZIP, Word .docx, Excel .xlsx) ----------
class ZipExportRequest(BaseModel):
    filename: str = "projet.zip"
    files: list[dict] = []  # [{"path": "...", "content": "..."}]


@app.post("/api/export/zip")
def export_zip(req: ZipExportRequest):
    import io, zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in req.files:
            path = (f.get("path") or f.get("name") or "fichier.txt").strip().lstrip("/\\")
            content = f.get("content", "")
            zf.writestr(path, content.encode("utf-8"))
    buf.seek(0)
    fname = req.filename if req.filename.endswith(".zip") else f"{req.filename}.zip"
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'}
    )


class DocxExportRequest(BaseModel):
    filename: str = "document.docx"
    title: str = "Document"
    content: str = ""


@app.post("/api/export/docx")
def export_docx(req: DocxExportRequest):
    import io, zipfile, html
    buf = io.BytesIO()
    body_xml = []
    if req.title:
        body_xml.append(
            f'<w:p><w:pPr><w:jc w:val="left"/><w:spacing w:after="240"/></w:pPr>'
            f'<w:r><w:rPr><w:b/><w:sz w:val="36"/><w:color w:val="1E44B0"/><w:rFonts w:ascii="Arial" w:hAnsi="Arial"/></w:rPr>'
            f'<w:t>{html.escape(req.title)}</w:t></w:r></w:p>'
        )
    for line in (req.content or "").split("\n"):
        line = line.strip()
        if not line:
            body_xml.append('<w:p><w:pPr><w:spacing w:after="100"/></w:pPr></w:p>')
            continue
        if line.startswith("# "):
            body_xml.append(
                f'<w:p><w:pPr><w:spacing w:before="240" w:after="140"/></w:pPr>'
                f'<w:r><w:rPr><w:b/><w:sz w:val="32"/><w:color w:val="17223B"/><w:rFonts w:ascii="Arial" w:hAnsi="Arial"/></w:rPr>'
                f'<w:t>{html.escape(line[2:].replace("**", ""))}</w:t></w:r></w:p>'
            )
        elif line.startswith("## "):
            body_xml.append(
                f'<w:p><w:pPr><w:spacing w:before="180" w:after="100"/></w:pPr>'
                f'<w:r><w:rPr><w:b/><w:sz w:val="26"/><w:color w:val="2F5FD6"/><w:rFonts w:ascii="Arial" w:hAnsi="Arial"/></w:rPr>'
                f'<w:t>{html.escape(line[3:].replace("**", ""))}</w:t></w:r></w:p>'
            )
        elif line.startswith("### "):
            body_xml.append(
                f'<w:p><w:pPr><w:spacing w:before="140" w:after="80"/></w:pPr>'
                f'<w:r><w:rPr><w:b/><w:sz w:val="22"/><w:color w:val="334155"/><w:rFonts w:ascii="Arial" w:hAnsi="Arial"/></w:rPr>'
                f'<w:t>{html.escape(line[4:].replace("**", ""))}</w:t></w:r></w:p>'
            )
        elif line.startswith(("- ", "* ", "• ")):
            clean_txt = html.escape(line[2:].replace("**", ""))
            body_xml.append(
                f'<w:p><w:pPr><w:ind w:left="360"/><w:spacing w:after="60"/></w:pPr>'
                f'<w:r><w:rPr><w:rFonts w:ascii="Arial" w:hAnsi="Arial"/></w:rPr>'
                f'<w:t>• {clean_txt}</w:t></w:r></w:p>'
            )
        else:
            clean_txt = html.escape(line.replace("**", ""))
            body_xml.append(
                f'<w:p><w:pPr><w:spacing w:after="80"/></w:pPr>'
                f'<w:r><w:rPr><w:sz w:val="21"/><w:rFonts w:ascii="Arial" w:hAnsi="Arial"/></w:rPr>'
                f'<w:t>{clean_txt}</w:t></w:r></w:p>'
            )

    doc_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">\n'
        '<w:body>\n'
        + "".join(body_xml)
        + '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/></w:sectPr>\n'
        '</w:body>\n'
        '</w:document>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
        '  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>\n'
        '  <Default Extension="xml" ContentType="application/xml"/>\n'
        '  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>\n'
        '</Types>'
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
        '  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>\n'
        '</Relationships>'
    )
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("word/document.xml", doc_xml)
    buf.seek(0)
    fname = req.filename if req.filename.endswith(".docx") else f"{req.filename}.docx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'}
    )


class XlsxExportRequest(BaseModel):
    filename: str = "classeur.xlsx"
    sheets: list[dict] = []  # [{"title": "Feuille 1", "rows": [["Col 1", "Col 2"], ["Val 1", "Val 2"]]}]


@app.post("/api/export/xlsx")
def export_xlsx(req: XlsxExportRequest):
    import io, openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    wb = openpyxl.Workbook()
    wb.remove(wb.active)  # retire la feuille vierge par défaut
    sheets_data = req.sheets or [{"title": "Données", "rows": []}]
    for s_idx, s in enumerate(sheets_data):
        ws = wb.create_sheet(title=s.get("title") or f"Feuille {s_idx + 1}")
        rows = s.get("rows", [])
        for r_idx, row in enumerate(rows, start=1):
            for c_idx, val in enumerate(row, start=1):
                cell = ws.cell(row=r_idx, column=c_idx, value=val)
                if r_idx == 1:
                    cell.font = Font(bold=True, color="FFFFFF")
                    cell.fill = PatternFill(start_color="1E44B0", end_color="1E44B0", fill_type="solid")
                    cell.alignment = Alignment(horizontal="center", vertical="center")
        # largeur automatique des colonnes
        for col in ws.columns:
            max_len = max(len(str(cell.value or "")) for cell in col) if col else 10
            col_letter = openpyxl.utils.get_column_letter(col[0].column)
            ws.column_dimensions[col_letter].width = max(max_len + 3, 12)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    fname = req.filename if req.filename.endswith(".xlsx") else f"{req.filename}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'}
    )


class PdfExportRequest(BaseModel):
    filename: str = "document.pdf"
    title: str = "Document MEL-IA"
    content: str = ""


@app.post("/api/export/pdf")
def export_pdf(req: PdfExportRequest):
    import io
    from reportlab.lib.pagesizes import A4
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib import colors

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, rightMargin=54, leftMargin=54, topMargin=54, bottomMargin=54)
    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        'DocTitle',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=20,
        leading=24,
        textColor=colors.HexColor('#1E44B0'),
        spaceAfter=14
    )
    h1_style = ParagraphStyle(
        'H1',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=14,
        leading=18,
        textColor=colors.HexColor('#17223B'),
        spaceBefore=14,
        spaceAfter=8
    )
    h2_style = ParagraphStyle(
        'H2',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=12,
        leading=16,
        textColor=colors.HexColor('#2F5FD6'),
        spaceBefore=10,
        spaceAfter=6
    )
    body_style = ParagraphStyle(
        'Body',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=10.5,
        leading=14.5,
        textColor=colors.HexColor('#334155'),
        spaceAfter=6
    )
    bullet_style = ParagraphStyle(
        'Bullet',
        parent=body_style,
        leftIndent=16,
        spaceAfter=4
    )

    story = []
    if req.title:
        story.append(Paragraph(req.title, title_style))
        story.append(Spacer(1, 10))

    for line in (req.content or "").split("\n"):
        line = line.strip()
        if not line:
            story.append(Spacer(1, 6))
            continue
        clean = line.replace('**', '').replace('*', '')
        if line.startswith("# "):
            story.append(Paragraph(clean[2:], h1_style))
        elif line.startswith("## "):
            story.append(Paragraph(clean[3:], h2_style))
        elif line.startswith("### "):
            story.append(Paragraph(clean[4:], h2_style))
        elif line.startswith(("- ", "* ", "• ")):
            story.append(Paragraph(f"• {clean[2:]}", bullet_style))
        else:
            story.append(Paragraph(clean, body_style))

    doc.build(story)
    buf.seek(0)
    fname = req.filename if req.filename.endswith(".pdf") else f"{req.filename}.pdf"
    return StreamingResponse(
        buf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'}
    )


class PptxExportRequest(BaseModel):
    filename: str = "presentation.pptx"
    title: str = "Présentation MEL-IA"
    content: str = ""


@app.post("/api/export/pptx")
def export_pptx(req: PptxExportRequest):
    import io
    from pptx import Presentation
    from pptx.util import Inches, Pt
    from pptx.dml.color import RGBColor

    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)
    blank_layout = prs.slide_layouts[6]

    title_slide = prs.slides.add_slide(blank_layout)
    tb = title_slide.shapes.add_textbox(Inches(1.5), Inches(2.2), Inches(10.3), Inches(3.0))
    tf = tb.text_frame
    tf.word_wrap = True
    p = tf.paragraphs[0]
    p.text = req.title or "Présentation"
    p.font.name = "Arial"
    p.font.size = Pt(40)
    p.font.bold = True
    p.font.color.rgb = RGBColor(0x1E, 0x44, 0xB0)

    p2 = tf.add_paragraph()
    p2.text = "Généré par MEL-IA"
    p2.font.name = "Arial"
    p2.font.size = Pt(20)
    p2.font.color.rgb = RGBColor(0x64, 0x74, 0x8B)

    slides_data = []
    current_title = ""
    current_bullets = []

    lines = (req.content or "").split("\n")
    for line in lines:
        l = line.strip()
        if not l:
            continue
        if l.startswith("## ") or l.startswith("# Slide") or l == "---":
            if current_title or current_bullets:
                slides_data.append((current_title or "Diapositive", current_bullets))
                current_bullets = []
            clean_title = l.replace("#", "").replace("-", "").strip()
            current_title = clean_title if clean_title else "Diapositive"
        elif l.startswith(("- ", "* ", "• ")):
            clean_b = l[2:].replace("**", "").strip()
            if clean_b:
                current_bullets.append(clean_b)
        else:
            clean_l = l.replace("**", "").strip()
            if clean_l and not clean_l.startswith("#"):
                current_bullets.append(clean_l)

    if current_title or current_bullets:
        slides_data.append((current_title or "Diapositive", current_bullets))

    for s_title, bullets in slides_data:
        slide = prs.slides.add_slide(blank_layout)
        header_box = slide.shapes.add_textbox(Inches(1.0), Inches(0.8), Inches(11.3), Inches(1.2))
        htf = header_box.text_frame
        htf.word_wrap = True
        hp = htf.paragraphs[0]
        hp.text = s_title
        hp.font.name = "Arial"
        hp.font.size = Pt(28)
        hp.font.bold = True
        hp.font.color.rgb = RGBColor(0x17, 0x22, 0x3B)

        content_box = slide.shapes.add_textbox(Inches(1.0), Inches(2.2), Inches(11.3), Inches(4.5))
        ctf = content_box.text_frame
        ctf.word_wrap = True
        for idx, bullet in enumerate(bullets):
            bp = ctf.paragraphs[0] if idx == 0 else ctf.add_paragraph()
            bp.text = f"•  {bullet}"
            bp.font.name = "Arial"
            bp.font.size = Pt(18)
            bp.font.color.rgb = RGBColor(0x33, 0x41, 0x55)
            bp.space_after = Pt(12)

    buf = io.BytesIO()
    prs.save(buf)
    buf.seek(0)
    fname = req.filename if req.filename.endswith(".pptx") else f"{req.filename}.pptx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'}
    )



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