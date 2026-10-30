@echo off
title MEL-IA - Serveur Local
cd /d "%~dp0"
echo ========================================================
echo    MEL-IA - Assistant Personnel (Bleu Nuit & Cyan)
echo ========================================================
echo Demarrage du serveur web sur http://127.0.0.1:8137 ...
echo Ouverture du navigateur...
start http://127.0.0.1:8137/
python -m uvicorn app:app --host 127.0.0.1 --port 8137 --reload
pause
