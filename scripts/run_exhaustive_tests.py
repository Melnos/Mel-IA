import sys
import os
import json
import time
import secrets
from pathlib import Path
from dotenv import load_dotenv

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = Path(r"c:\Users\djima\Downloads\mel-ia\mel-ia")
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

import httpx

BASE_URL = "http://127.0.0.1:8137"
client = httpx.Client(base_url=BASE_URL, timeout=90.0)

test_results = []

def run_test(name, fn):
    print(f"[*] TEST: {name} ... ", end="", flush=True)
    try:
        fn()
        print("PASS")
        test_results.append((name, True, "OK"))
    except Exception as e:
        print(f"FAIL -> {e}")
        test_results.append((name, False, str(e)))
    time.sleep(1.0)

# 1. Test Static files
def test_static_routes():
    r = client.get("/")
    assert r.status_code == 200, f"GET / status: {r.status_code}"
    assert "MEL-IA" in r.text
    
    r = client.get("/app")
    assert r.status_code == 200, f"GET /app status: {r.status_code}"
    assert "MEL-IA" in r.text
    assert "claude-drawer" in r.text
    
    r = client.get("/manifest.json")
    assert r.status_code == 200, f"GET /manifest.json status: {r.status_code}"
    assert "name" in r.json()
    
    r = client.get("/sw.js")
    assert r.status_code == 200, f"GET /sw.js status: {r.status_code}"
    assert "mel-ia-v13" in r.text or "mel-ia-v12" in r.text, "sw.js cache version is not mel-ia-v13"

# 2. Test Auth flow (Register, Login, Invalid Pass, Me)
gemini_key = os.getenv("GEMINI_API_KEY", "")
test_user = f"u_{secrets.token_hex(4)}"
test_pass = "Password123!"
auth_token = None

def test_auth_flow():
    global auth_token
    # Register
    r = client.post("/api/auth/register", json={
        "username": test_user,
        "password": test_pass,
        "api_key": gemini_key,
        "interests": "IA, Dev Web"
    })
    assert r.status_code == 200, f"Register failed: {r.text}"
    j = r.json()
    assert "token" in j, "Token not in register response"
    auth_token = j["token"]
    
    # Login with wrong pass
    r = client.post("/api/auth/login", json={"username": test_user, "password": "WrongPassword"})
    assert r.status_code == 401, f"Expected 401 on wrong pass, got: {r.status_code}"
    
    # Login with correct pass
    r = client.post("/api/auth/login", json={"username": test_user, "password": test_pass})
    assert r.status_code == 200, f"Login failed: {r.text}"
    j = r.json()
    assert "token" in j
    auth_token = j["token"]
    
    # Test /api/auth/me
    r = client.get("/api/auth/me", headers={"x-auth-token": auth_token})
    assert r.status_code == 200, f"/api/auth/me failed: {r.text}"
    assert r.json().get("username") == test_user
    assert r.json().get("logged_in") is True

# 3. Test Sessions & History
test_session_id = None
def test_sessions_and_history():
    global test_session_id
    headers = {"x-auth-token": auth_token}
    
    # List sessions (should contain 'default')
    r = client.get("/api/sessions", headers=headers)
    assert r.status_code == 200
    sessions = r.json().get("sessions", [])
    assert len(sessions) >= 1
    
    # Create new session
    r = client.post("/api/sessions", headers=headers)
    assert r.status_code == 200
    test_session_id = r.json().get("session_id")
    assert test_session_id, "No session_id created"

    # Rename session
    r = client.patch(f"/api/sessions/{test_session_id}", headers=headers, json={"title": "Session Renommée Test"})
    assert r.status_code == 200, f"Failed to rename session: {r.text}"
    assert r.json().get("title") == "Session Renommée Test"

    # Verify session list has updated title
    r = client.get("/api/sessions", headers=headers)
    assert r.status_code == 200
    matched = [s for s in r.json().get("sessions", []) if s["id"] == test_session_id]
    assert matched and matched[0]["title"] == "Session Renommée Test", f"Title not updated: {matched}"
    
    # Get empty history
    r = client.get(f"/api/history?session_id={test_session_id}", headers=headers)
    assert r.status_code == 200
    assert r.json().get("messages") == []

# 4. Test Chat (Sync and Fallback handling)
def test_chat_generation():
    global test_session_id
    sid = test_session_id or "default"
    headers = {"x-auth-token": auth_token}
    r = client.post(
        "/api/chat",
        headers=headers,
        json={"message": "Reponds uniquement 'OK' en un mot.", "session_id": sid}
    )
    assert r.status_code == 200, f"/api/chat failed: {r.text}"
    reply = r.json().get("reply", "")
    assert reply, "Chat returned empty reply"
    
    # Verify history has 2 messages with timestamps
    r = client.get(f"/api/history?session_id={sid}", headers=headers)
    assert r.status_code == 200
    msgs = r.json().get("messages", [])
    assert len(msgs) == 2, f"Expected 2 messages, got {len(msgs)}"
    assert msgs[0]["role"] == "user"
    assert msgs[1]["role"] == "assistant"
    assert "ts" in msgs[0] and msgs[0]["ts"] > 0, "Missing timestamp on user message"
    assert "ts" in msgs[1] and msgs[1]["ts"] > 0, "Missing timestamp on assistant message"

# 5. Test Streaming SSE
def test_chat_stream():
    global test_session_id
    sid = test_session_id or "default"
    headers = {"x-auth-token": auth_token}
    tokens = []
    done_received = False
    
    with client.stream(
        "POST",
        "/api/chat-stream",
        headers=headers,
        json={"message": "Dis bonjour et compte jusqu'a 2.", "session_id": sid}
    ) as r:
        assert r.status_code == 200
        for line in r.iter_lines():
            if line.startswith("data:"):
                raw = line[5:].strip()
                if not raw or raw == "[DONE]":
                    continue
                payload = json.loads(raw)
                if "chunk" in payload:
                    tokens.append(payload["chunk"])
                if payload.get("done"):
                    done_received = True
                    break
                    
    assert done_received, "Stream did not return done: true"
    full_text = "".join(tokens)
    assert len(full_text) > 0, "No tokens received from stream"

# 6. Test Document Exports (Word, Excel)
def test_document_exports():
    # Word docx export
    r = client.post(
        "/api/export/docx",
        json={"markdown": "# Titre du Rapport\n\nVoici un test complet avec une liste :\n- Element A\n- Element B\n\n| Col 1 | Col 2 |\n|---|---|\n| Val 1 | Val 2 |", "filename": "rapport_test"}
    )
    assert r.status_code == 200, f"/api/export/docx failed: {r.status_code}"
    assert len(r.content) > 1000, "Docx file too small"
    assert "wordprocessingml" in r.headers.get("content-type", "")
    
    # Excel xlsx export
    r = client.post(
        "/api/export/xlsx",
        json={"tables": [[["Produit", "Prix", "Stock"], ["Ordinateur", "899 €", "12"], ["Souris", "29 €", "50"]]], "filename": "inventaire_test"}
    )
    assert r.status_code == 200, f"/api/export/xlsx failed: {r.status_code}"
    assert len(r.content) > 1000, "Xlsx file too small"
    assert "spreadsheetml" in r.headers.get("content-type", "")

# 7. Test Media Chat Upload (Document texte)
def test_media_chat():
    global test_session_id
    sid = test_session_id or "default"
    headers = {"x-auth-token": auth_token}
    fake_txt = b"Rapport d'analyse Mel-IA 2026. Tout fonctionne correctement."
    files = {"file": ("note.txt", fake_txt, "text/plain")}
    data = {"message": "Resume ce document joint", "session_id": sid}
    r = client.post("/api/chat-media", headers=headers, data=data, files=files, timeout=90.0)
    assert r.status_code == 200, f"/api/chat-media failed: {r.text}"
    j = r.json()
    assert "reply" in j
    assert len(j["reply"]) > 0

# 8. Test Facts & Profile customization
def test_facts_and_profile():
    headers = {"x-auth-token": auth_token}
    r = client.post(
        "/api/auth/profile",
        headers=headers,
        json={"persona": "Tu es un tuteur patient.", "interests": "Python, IA", "model_pref": "auto"}
    )
    assert r.status_code == 200
    
    # Add key in api_keys
    r = client.post(
        "/api/keys",
        headers=headers,
        json={"label": "Cle Test Secondaire", "api_key": gemini_key or "AIzaSy_dummy_key_mock"}
    )
    assert r.status_code == 200
    
    # List keys
    r = client.get("/api/keys", headers=headers)
    assert r.status_code == 200
    keys = r.json().get("keys", [])
    found = any(k.get("label") == "Cle Test Secondaire" for k in keys)
    assert found, "Secondary key not found in list"
    
    # Delete key
    target = next(k for k in keys if k.get("label") == "Cle Test Secondaire")
    r = client.delete(f"/api/keys/{target['id']}", headers=headers)
    assert r.status_code == 200

# 9. Test Data Export & Import (Anti-data-loss Backup)
def test_backup_and_restore():
    headers = {"x-auth-token": auth_token}
    r = client.get("/api/db/export", headers=headers)
    assert r.status_code == 200, f"Export failed: {r.status_code}"
    backup_data = r.json()
    assert "user" in backup_data
    assert "sessions" in backup_data
    assert "messages" in backup_data
    
    # Restore
    r = client.post("/api/db/import", headers=headers, json=backup_data)
    assert r.status_code == 200, f"Import failed: {r.status_code}"

# 10. Test Modification de mot de passe
def test_password_change():
    global auth_token
    headers = {"x-auth-token": auth_token}
    # Current wrong pass
    r = client.post("/api/auth/password", headers=headers, json={
        "current_password": "WrongPassword999!",
        "new_password": "NewSecretPassword456!"
    })
    assert r.status_code == 400, f"Expected 400 on wrong password, got {r.status_code}"

    # Short new pass (<6)
    r = client.post("/api/auth/password", headers=headers, json={
        "current_password": test_pass,
        "new_password": "123"
    })
    assert r.status_code == 400, f"Expected 400 on short password, got {r.status_code}"

    # Success change
    new_pwd = "NewSecretPassword456!"
    r = client.post("/api/auth/password", headers=headers, json={
        "current_password": test_pass,
        "new_password": new_pwd
    })
    assert r.status_code == 200, f"Password change failed: {r.text}"
    assert r.json().get("ok") is True

    # Old password no longer works
    r = client.post("/api/auth/login", json={"username": test_user, "password": test_pass})
    assert r.status_code == 401, "Old password should not work"

    # New password works
    r = client.post("/api/auth/login", json={"username": test_user, "password": new_pwd})
    assert r.status_code == 200, "New password failed to login"
    auth_token = r.json()["token"]

# 11. Test Delete Session & Cleanup
def test_session_cleanup():
    headers = {"x-auth-token": auth_token}
    sid = test_session_id or "default"
    r = client.delete(f"/api/sessions/{sid}", headers=headers)
    assert r.status_code == 200
    
    # Check session is cleared
    r = client.get(f"/api/history?session_id={sid}", headers=headers)
    assert len(r.json().get("messages", [])) == 0

print("\n" + "="*55)
print("=== LANCEMENT DU BANC DE TEST EXHAUSTIF MEL-IA ===")
print("="*55 + "\n")

run_test("Fichiers Statiques & Manifest & Cache v9", test_static_routes)
run_test("Authentification & Inscription & Sécurité Pass", test_auth_flow)
run_test("Gestion des Sessions & Isolation Historique", test_sessions_and_history)
run_test("Génération IA Gemini & Sauvegarde Messages", test_chat_generation)
run_test("Streaming SSE temps réel & Signal [DONE]", test_chat_stream)
run_test("Générateurs Documents Word (.docx) & Excel (.xlsx)", test_document_exports)
run_test("Upload et Analyse Multimodale Documents (.txt)", test_media_chat)
run_test("Personnalisation Profil & Clés Secondaires", test_facts_and_profile)
run_test("Sauvegarde Globale & Restauration (Anti-perte)", test_backup_and_restore)
run_test("Modification de mot de passe & Sécurité Hash", test_password_change)
run_test("Nettoyage & Suppression de Session", test_session_cleanup)

print("\n" + "="*55)
passes = sum(1 for _, ok, _ in test_results if ok)
fails = sum(1 for _, ok, _ in test_results if not ok)
print(f"RÉSULTAT GLOBAL : {passes}/{len(test_results)} TESTS RÉUSSIS ({fails} échecs)")
print("="*55)
if fails > 0:
    sys.exit(1)
