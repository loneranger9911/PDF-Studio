@echo off
rem Local build: app folder + installer. Run from the folder containing pdf_studio.py.
python -m venv .venv
call .venv\Scripts\activate
pip install -r requirements.txt
pyinstaller --noconsole --noconfirm --name PDFStudio pdf_studio.py
echo.
echo App folder: dist\PDFStudio\PDFStudio.exe
if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" (
  "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" installer.iss
  echo Installer: installer_output\PDFStudio-Setup.exe
) else (
  echo Inno Setup 6 not found, so no installer was built. Get it from jrsoftware.org/isinfo.php
)
pause
