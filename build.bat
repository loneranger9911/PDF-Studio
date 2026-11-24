@echo off
rem Run from the folder containing pdf_studio.py
python -m venv .venv
call .venv\Scripts\activate
pip install -r requirements.txt
pyinstaller --noconsole --onefile --name PDFStudio pdf_studio.py
echo.
echo Done. Your exe is at dist\PDFStudio.exe
pause
