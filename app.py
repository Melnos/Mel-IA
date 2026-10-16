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
from dotenv import load_dotenv

BASE = Path(__file__).parent
load_dotenv(BASE / ".env")

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from database import db, init_db, DBIntegrityError, IS_POSTGRES

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


# ---------- Base de données (PostgreSQL Neon / SQLite local) ----------
# db() et init_db() sont importés depuis database.py


# ---------- Clés API (spécifiques à chaque utilisateur) ----------
_key_state: dict[str, dict] = {}  # key -> {"cool_until": ts}


def get_user_keys(user: dict | None) -> list[str]:
    """Retourne les clés API applicables.
    Priorité :
    1. Clés de l'utilisateur connecté (clé de compte ou clés secondaires)
    2. Fallback sur GEMINI_API_KEY du fichier .env (pour le mode invité ou en local)
    """
    keys: list[str] = []
    if user:
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
    # Clé d'environnement .env (fallback serveur utile pour le mode invité ou dev local)
    env_key = os.getenv("GEMINI_API_KEY", "").strip()
    if env_key and env_key not in keys:
        keys.append(env_key)
    return keys


def _key_usable(k: str) -> bool:
    return _key_state.get(k, {}).get("cool_until", 0) <= time.time()


def _cooldown(k: str, seconds: int):
    _key_state[k] = {"cool_until": time.time() + seconds}


init_db()


# ---------- Comptes utilisateurs (identifiant + mot de passe) ----------
def _hash_pw(pw: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 200_000).hex()


# --- Chiffrement des clés API au repos (Fernet) ---
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
        except DBIntegrityError:
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


def system_prompt(facts: list[str], persona: str = "", interests: str = "", memories: list[str] | None = None) -> str:
    who = f" de {OWNER}" if OWNER else ""
    p = (
        f"Tu es {NAME}, le mentor technique personnel et le bras droit virtuel{who} pour ses études et projets en informatique.\n\n"
        "### RÔLE & RÈGLES DE CONDUITE (MENTOR TECHNIQUE PERSONNEL) :\n"
        "1. Profil & Posture : Tu es son mentor dédié en informatique (L1, L2, L3, Master, et sur les technos : Java, Python, C/C++, Web, Réseau, BDD/SQL, Algorithmique, Architecture logicielle).\n"
        "2. Règles de communication : Sois direct, rigoureux, sans bavardage inutile ni flatterie superficielle. Utilise des emojis de manière sobre et pertinente pour structurer la réponse, jamais pour décorer inutilement.\n"
        "3. Analyse de code : Quand ton utilisateur te donne du code, analyse-le en profondeur, trouve les failles, explique le pourquoi et propose du code propre, robuste, moderne et testé.\n"
        "4. Questions de cours : Donne des définitions nettes et précises, immédiatement suivies d'exemples concrets.\n"
        "5. Zéro complaisance : Ne dis jamais oui par complaisance. Si son approche d'un problème est mauvaise ou sous-optimale, alerte-le fermement et propose une meilleure architecture ou logique.\n"
        "6. Priorités : L'accompagner au quotidien sur ses projets, ses partiels et examens à préparer, et les sujets sur lesquels il veut progresser en priorité.\n"
        "7. Code, Fichiers & Projets Multi-fichiers (Comme Claude / Artefacts) :\n"
        "   - Quand l'utilisateur te demande un projet (application web, backend, script, TP, mini-logiciel avec plusieurs fichiers : HTML, CSS, JS, Python, etc.), nomme OBLIGATOIREMENT chaque fichier sur son bloc de code, sous la forme : ```langage filename=\"nom_du_fichier.ext\" (exemples: ```html filename=\"index.html\", ```css filename=\"style.css\", ```javascript filename=\"app.js\", ```python filename=\"server.py\").\n"
        "   - Ne tronque pas le code et livre des fichiers complets et fonctionnels. L'interface créera automatiquement une carte de Projet Artefact et permettra à l'utilisateur de télécharger tous les fichiers d'un coup dans un fichier ZIP (.zip) ou de les tester en direct dans le volet d'aperçu.\n"
        "8. Documents Bureautiques Réels (Word .docx, PDF .pdf, PowerPoint .pptx, Excel .xlsx, ZIP) :\n"
        "   - Word (.docx) & PDF (.pdf) : Structure le document avec des titres nets (# Titre, ## Sections), des paragraphes soignés et des listes à puces. L'utilisateur disposera des boutons de téléchargement direct Word (.docx) et PDF (.pdf).\n"
        "   - PowerPoint (.pptx) : Quand on te demande une présentation, un diaporama ou des slides, sépare chaque diapositive avec un titre de slide : '## Slide X : Titre de la diapositive', suivi de 4 à 6 points clés clairs et si besoin un encadré de code. L'interface compilera directement un fichier Microsoft PowerPoint .pptx officiel prêt à être ouvert dans PowerPoint !\n"
        "   - Excel (.xlsx) : Quand on te demande un tableau, un budget, une feuille de calcul ou des données chiffrées, présente-les sous forme de tableau Markdown clair avec en-têtes et totaux/formules. L'utilisateur aura un bouton pour télécharger le fichier Excel .xlsx direct.\n"
        "   - ZIP (.zip) : Pour tout projet complet de code, signale à l'utilisateur qu'il peut télécharger l'archive ZIP complète contenant tous les fichiers du projet en un seul clic.\n\n"
        "### MÉMOIRE CONTINUE & SYNCHRONISATION MULTI-CHATS :\n"
        "Tu possèdes une mémoire universelle et continue de tout ce que ton utilisateur t'a déjà dit à travers TOUTES ses discussions (passées, présentes, récentes ou anciennes).\n"
        "Même si un sujet, un code, une idée ou un détail de cours a été abordé dans un autre chat il y a des jours, des mois ou des années, tu t'en souviens et tu fais naturellement le lien, comme dans une vraie relation de mentorat continu.\n"
        f"Date actuelle : {datetime.now().strftime('%d/%m/%Y')}."
    )
    if persona:
        p += f"\n\nDirectives spécifiques et attentes complémentaires de ton utilisateur :\n{persona}"
    if interests:
        p += f"\n\nParcours, modules et technos du moment : {interests}"
    if facts:
        p += "\n\nFaits mémorisés sur ton utilisateur :\n"
        p += "\n".join(f"- {f}" for f in facts)
    if memories:
        p += "\n\nSouvenirs et contexte issus des autres discussions de cet utilisateur (mémoire croisée synchronisée) :\n"
        p += "\n".join(f"- {m}" for m in memories)
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
        "gemini-3.8-flash",
        "gemma-4-31b-it",
        "gemini-flash-latest",
        "gemini-pro-latest",
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
               search: bool = False, memories: list[str] | None = None) -> dict:
    """Interroge Gemini avec la clé personnelle de l'utilisateur connecté."""
    user_keys = get_user_keys(user)
    keys = [k for k in user_keys if _key_usable(k)] or user_keys
    if not keys:
        raise HTTPException(
            400,
            "Aucune clé API Gemini : veuillez renseigner votre clé API dans Paramètres ou vous connecter avec un compte valide.",
        )
    body = {
        "systemInstruction": {"parts": [{"text": system_prompt(facts, (user or {}).get("persona") or "", (user or {}).get("interests") or "", memories=memories)}]},
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


# ---------- Génération d'images (Imagen 3 + Fallbacks résilients) ----------
def generate_svg_illustration(prompt: str, user: dict | None = None) -> str:
    """Fallback résilient : demande à Gemini de concevoir une illustration vectorielle SVG spectaculaire."""
    system_inst = (
        "Tu es un graphiste et illustrateur vectoriel d'élite. Crée une illustration vectorielle SVG complète, moderne, "
        "esthétique et détaillée correspondant au prompt. Utilise de beaux dégradés (<defs><linearGradient>), des ombres portées, "
        "des formes soignées et des couleurs harmonieuses. Réponds UNIQUEMENT avec le code <svg ...>...</svg> valide, sans aucun texte explicatif ni markdown."
    )
    user_keys = get_user_keys(user)
    keys = [k for k in user_keys if _key_usable(k)] or user_keys
    candidates = get_model_candidates()
    body = {
        "systemInstruction": {"parts": [{"text": system_inst}]},
        "contents": [{"parts": [{"text": f"Génère une illustration SVG détaillée et moderne pour : {prompt}"}]}],
        "generationConfig": {"temperature": 0.4, "maxOutputTokens": 4096},
    }
    for key in keys:
        for model in candidates:
            try:
                r = httpx.post(
                    f"{API_BASE}/models/{model}:generateContent",
                    headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                    json=body,
                    timeout=30,
                )
                if r.status_code == 200:
                    txt = "".join(p.get("text", "") for p in r.json()["candidates"][0]["content"]["parts"])
                    m = re.search(r"<svg[\s\S]*?</svg>", txt, re.I)
                    if m:
                        return m.group(0)
            except Exception:
                continue
    return ""


def generate_image(prompt: str, user: dict | None = None) -> str:
    """Génère une image avec Google Imagen 3 (via API :predict), avec fallbacks IA haute fidélité."""
    user_keys = get_user_keys(user)
    keys = [k for k in user_keys if _key_usable(k)] or user_keys
    if not keys:
        raise HTTPException(
            400,
            "Aucune clé API Gemini disponible : connectez-vous avec votre compte ou ajoutez une clé dans Paramètres.",
        )

    # 1. Tentative avec Google Imagen 3 via endpoint officiel :predict
    for key in keys:
        for model in ("imagen-3.0-generate-002", "imagen-3.0-generate-001"):
            try:
                r = httpx.post(
                    f"{API_BASE}/models/{model}:predict",
                    headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                    json={
                        "instances": [{"prompt": prompt}],
                        "parameters": {"sampleCount": 1, "aspectRatio": "1:1"}
                    },
                    timeout=60,
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

    # 2. Fallback Pollinations Flux / SDXL
    try:
        from urllib.parse import quote
        safe_p = quote(prompt[:200])
        seed = secrets.randbelow(999999)
        r = httpx.get(
            f"https://image.pollinations.ai/prompt/{safe_p}?width=1024&height=1024&nologo=true&seed={seed}",
            timeout=30,
            follow_redirects=True,
        )
        if r.status_code == 200 and r.content and len(r.content) > 3000:
            fname = f"gen_{uuid.uuid4().hex[:12]}.jpg"
            (MEDIA_DIR / fname).write_bytes(r.content)
            return fname
    except Exception:
        pass

    # 3. Fallback SVG généré sur mesure par Gemini
    svg_code = generate_svg_illustration(prompt, user)
    if svg_code:
        fname = f"gen_{uuid.uuid4().hex[:12]}.svg"
        (MEDIA_DIR / fname).write_text(svg_code, encoding="utf-8")
        return fname

    raise HTTPException(502, "Impossible de générer l'image : vérifiez votre connexion ou réessayez avec un autre sujet.")



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

    memories = get_cross_session_context(uid, sid, text)
    result = ask_gemini([dict(r) for r in rows], facts, user=user, search=do_search, memories=memories)
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
    if x_auth_token and not user:
        raise HTTPException(401, "Session expirée. Veuillez vous reconnecter.")
    user_keys = get_user_keys(user)
    keys = [k for k in user_keys if _key_usable(k)] or user_keys
    if not keys:
        raise HTTPException(
            400,
            "Aucune clé API Gemini disponible : veuillez renseigner votre clé API dans Paramètres.",
        )
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

        rows = [
            dict(r)
            for r in c.execute(
                "SELECT role, content, media_kind, media_path, media_mime FROM messages WHERE session_id = ? AND user_id IS ? ORDER BY id DESC LIMIT 40",
                (sid, uid),
            ).fetchall()[::-1]
        ]
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

        memories = get_cross_session_context(uid, sid, text)
        req_body = {
            "systemInstruction": {"parts": [{"text": system_prompt(facts, (user or {}).get("persona") or "", (user or {}).get("interests") or "", memories=memories)}]},
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
                res = ask_gemini([dict(r) for r in rows], facts, user=user, search=do_search, memories=memories)
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
    if x_auth_token and not user:
        raise HTTPException(401, "Session expirée. Veuillez vous reconnecter.")
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

    memories = get_cross_session_context(uid, sid, text)
    result = ask_gemini([dict(r) for r in rows], facts, user=user, search=bool(SEARCH_KEYWORDS.search(text)), memories=memories)
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