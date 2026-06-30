#!/usr/bin/env python3
"""Скачивает пакет с PyPI в папку packages/

Использование:
  python scripts/add_package.py requests
  python scripts/add_package.py requests rich tqdm
"""
import subprocess
import sys
from pathlib import Path

PACKAGES_DIR = Path(__file__).parent.parent / "packages"
PACKAGES_DIR.mkdir(exist_ok=True)

def download(spec):
    print(f"⬇  Скачиваю {spec} ...")
    subprocess.run([
        sys.executable, "-m", "pip", "download", spec,
        "--dest", str(PACKAGES_DIR),
        "--no-deps",
    ])

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Использование: python scripts/add_package.py <пакет> [пакет2 ...]")
        sys.exit(1)
    for spec in sys.argv[1:]:
        download(spec)
    print("\nГотово. Запусти: git add packages/ && git commit && git push")
