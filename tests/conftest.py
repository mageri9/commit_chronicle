import sys
from pathlib import Path

# Гарантируем, что корень репозитория (с пакетом `src`) виден для импорта,
# даже если pytest запускается не из корня и проект не установлен как editable.
ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))