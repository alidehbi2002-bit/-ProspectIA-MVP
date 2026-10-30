@echo off
setlocal EnableExtensions
set "DOSSIER=%~dp0"
cd /d "%DOSSIER%"

echo ==============================================
echo    PROSPECTIA - INSTALLATION ET DEMARRAGE
echo ==============================================
echo.

if not exist "%DOSSIER%requirements.txt" (
  echo ERREUR : requirements.txt est introuvable.
  echo.
  echo Decompressez d'abord TOUT le fichier ZIP :
  echo   1. Clic droit sur le ZIP ^> Extraire tout
  echo   2. Ouvrez le dossier extrait
  echo   3. Lancez INSTALLER_ET_LANCER.bat dans ce dossier
  echo.
  pause
  exit /b 1
)
if not exist "%DOSSIER%serveur.py" (
  echo ERREUR : serveur.py est introuvable. Extrayez TOUT le ZIP.
  pause
  exit /b 1
)

set "PYTHON_CMD="
py -3 --version >nul 2>&1
if not errorlevel 1 set "PYTHON_CMD=py -3"
if not defined PYTHON_CMD (
  python --version >nul 2>&1
  if not errorlevel 1 set "PYTHON_CMD=python"
)
if not defined PYTHON_CMD (
  echo Python 3 est requis.
  echo Installez-le sur https://www.python.org/downloads/
  echo Cochez "Add python.exe to PATH" puis relancez ce fichier.
  pause
  exit /b 1
)

if not exist "%DOSSIER%.venv\Scripts\python.exe" (
  echo Premiere execution : creation de l'environnement Python...
  %PYTHON_CMD% -m venv "%DOSSIER%.venv"
  if errorlevel 1 (
    echo ERREUR : creation de l'environnement Python impossible.
    pause
    exit /b 1
  )
)

echo Installation des dependances du moteur gratuit...
"%DOSSIER%.venv\Scripts\python.exe" -m pip install -r "%DOSSIER%requirements.txt"
if errorlevel 1 (
  echo.
  echo ERREUR : installation impossible. Verifiez la connexion Internet.
  pause
  exit /b 1
)

echo.
echo Lancement de ProspectIA : http://127.0.0.1:8765/
echo Gardez cette fenetre ouverte pendant l'utilisation.
"%DOSSIER%.venv\Scripts\python.exe" "%DOSSIER%serveur.py"
if errorlevel 1 (
  echo.
  echo Le serveur n'a pas demarre. Verifiez que le port 8765 est libre.
)
pause
