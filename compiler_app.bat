@echo off
title MEL-IA - Compilation en Executable Windows
cd /d "%~dp0"
echo ========================================================
echo    MEL-IA - Compilation en cours (PyInstaller)
echo ========================================================
echo Verification de l'icone...
if not exist "static\icons\icon.ico" (
    python -c "from PIL import Image; img = Image.open('static/icons/icon-512.png'); img.save('static/icons/icon.ico', format='ICO', sizes=[(16,16), (32,32), (48,48), (64,64), (128,128), (256,256)])"
)
echo Lancement de la compilation...
pyinstaller --noconfirm --onedir --name "MEL-IA" --icon "static/icons/icon.ico" --add-data "static;static" --hidden-import "uvicorn.logging" --hidden-import "uvicorn.loops.auto" --hidden-import "uvicorn.protocols.http.auto" --hidden-import "uvicorn.protocols.websockets.auto" --hidden-import "uvicorn.lifespans.on" --hidden-import "uvicorn.lifespans.off" run_mel.py
if exist ".env" (
    copy /y ".env" "dist\MEL-IA\.env" >nul
)
if exist "data" (
    if not exist "dist\MEL-IA\data" mkdir "dist\MEL-IA\data"
    copy /y "data\*" "dist\MEL-IA\data\" >nul
)
echo ========================================================
echo Compilation terminee avec succes !
echo L'executable se trouve dans : dist\MEL-IA\MEL-IA.exe
echo ========================================================
pause

