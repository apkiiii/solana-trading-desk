"""Lanceur de l'application (utilisé par PyInstaller pour fabriquer SolanaTradingDesk.exe).

En développement : python SolanaTradingDesk.py
"""
from app.main import main

if __name__ == "__main__":
    raise SystemExit(main())
