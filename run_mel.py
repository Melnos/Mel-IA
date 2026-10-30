"""
MEL-IA — Launcher Desktop & Exécutable Windows
Lance le serveur FastAPI/Uvicorn et ouvre automatiquement le navigateur.
"""
import multiprocessing
import os
import sys
import threading
import time
import webbrowser

multiprocessing.freeze_support()

def open_browser(url: str):
    time.sleep(1.2)
    try:
        webbrowser.open(url)
    except Exception:
        pass

def main():
    port = int(os.getenv("PORT", "8137"))
    host = os.getenv("HOST", "127.0.0.1")
    url = f"http://{host}:{port}/app"
    
    print("=" * 64)
    print("           MEL-IA — Assistant Personnel Intelligent           ")
    print("=" * 64)
    print(f"  Serveur actif sur   : {url}")
    print("  Ouverture du navigateur web...")
    print("  Laissez cette fenêtre ouverte pendant l'utilisation.")
    print("  (Appuyez sur Ctrl+C pour quitter proprement)")
    print("=" * 64)
    
    threading.Thread(target=open_browser, args=(url,), daemon=True).start()
    
    import uvicorn
    from app import app
    
    uvicorn.run(app, host=host, port=port, log_level="warning")

if __name__ == "__main__":
    main()
