#!/usr/bin/env python3
"""
piphub-repo v2.0.0 — CLI для управления pip-репозиториями в Termux.

  piphub-repo install requests rich
  piphub-repo push requests rich          # скачать + запушить в зеркало
  piphub-repo build myscript.py           # собрать .whl и запушить
  piphub-repo update                      # обновить пакеты в зеркале
  piphub-repo outdated                    # устаревшие пакеты в зеркале
  piphub-repo sync                        # синхронизировать метаданные
  piphub-repo uninstall requests          # удалить пакет из pip
  piphub-repo list
  piphub-repo use tastade
  piphub-repo ping
  piphub-repo config --show
  piphub-repo status
"""

from __future__ import annotations

import argparse
import configparser
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import time
import urllib.request
from pathlib import Path

VERSION  = "2.6.0"
APP_NAME = "piphub-repo"

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "piphub-repo"
DB_PATH    = CONFIG_DIR / "repos.db"

PIP_CONF_PATHS = [
    Path("/data/data/com.termux/files/usr/etc/pip.conf"),
    Path.home() / ".config" / "pip" / "pip.conf",
    Path.home() / "pip.conf",
]

BUILTIN_REPOS = [
    {
        "name": "tastade",
        "display": "Tastade Personal Repo",
        "url": "https://tastade.github.io/piphub-repo/simple/",
        "meta_url": "https://tastade.github.io/piphub-repo/mirror_meta.json",
        "fallback_url": "https://pypi.org/simple/",
        "description": "Личный репозиторий Tastade — пакеты для Termux (aarch64)",
    },
    {
        "name": "pypi",
        "display": "PyPI (официальный)",
        "url": "https://pypi.org/simple/",
        "meta_url": None,
        "fallback_url": None,
        "description": "Официальный Python Package Index",
    },
    {
        "name": "tuna",
        "display": "Tuna Mirror (TUNA)",
        "url": "https://pypi.tuna.tsinghua.edu.cn/simple/",
        "meta_url": None,
        "fallback_url": "https://pypi.org/simple/",
        "description": "Зеркало Университета Цинхуа",
    },
]

# ─── Rich / plain fallback ────────────────────────────────────────────────────

try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.prompt import Prompt, Confirm
    from rich.markup import escape
    from rich.syntax import Syntax
    HAS_RICH = True
    console = Console()
except ImportError:
    HAS_RICH = False
    console = None

def rprint(*args, **kwargs):
    if HAS_RICH:
        console.print(*args, **kwargs)
    else:
        text = " ".join(str(a) for a in args)
        print(re.sub(r'\[/?[^\]]*\]', '', text))

def rinput(prompt: str, default: str = "") -> str:
    if HAS_RICH:
        return Prompt.ask(prompt, default=default) if default else Prompt.ask(prompt)
    plain_prompt = re.sub(r'\[/?[^\]]*\]', '', prompt)
    disp = f"{plain_prompt} [{default}]: " if default else f"{plain_prompt}: "
    val = input(disp).strip()
    return val or default

def rconfirm(prompt: str, default: bool = True) -> bool:
    if HAS_RICH:
        return Confirm.ask(prompt, default=default)
    plain_prompt = re.sub(r'\[/?[^\]]*\]', '', prompt)
    yn = "Y/n" if default else "y/N"
    val = input(f"{plain_prompt} [{yn}]: ").strip().lower()
    return (val in ("y", "yes", "д", "да")) if val else default

# ─── База данных ──────────────────────────────────────────────────────────────

def db_connect() -> sqlite3.Connection:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS repos (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            name         TEXT UNIQUE NOT NULL,
            display      TEXT NOT NULL,
            url          TEXT NOT NULL,
            meta_url     TEXT,
            fallback_url TEXT,
            description  TEXT,
            builtin      INTEGER DEFAULT 0,
            active       INTEGER DEFAULT 0,
            ping_ms      INTEGER,
            last_ping    TEXT
        );
        CREATE TABLE IF NOT EXISTS install_log (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            package      TEXT NOT NULL,
            repo_name    TEXT,
            status       TEXT,
            installed_at TEXT DEFAULT (datetime('now'))
        );
    """)
    conn.commit()
    return conn

def seed_builtins(conn):
    for r in BUILTIN_REPOS:
        conn.execute("""
            INSERT OR IGNORE INTO repos
              (name, display, url, meta_url, fallback_url, description, builtin)
            VALUES (?, ?, ?, ?, ?, ?, 1)
        """, (r["name"], r["display"], r["url"],
              r.get("meta_url"), r.get("fallback_url"), r.get("description", "")))
    conn.commit()

def get_active(conn):
    return conn.execute("SELECT * FROM repos WHERE active=1 LIMIT 1").fetchone()

def set_active(conn, name: str):
    conn.execute("UPDATE repos SET active=0")
    conn.execute("UPDATE repos SET active=1 WHERE name=?", (name,))
    conn.commit()

def all_repos(conn):
    return conn.execute("SELECT * FROM repos ORDER BY builtin DESC, name").fetchall()

# ─── pip.conf ─────────────────────────────────────────────────────────────────

def find_pip_conf() -> Path:
    for p in PIP_CONF_PATHS:
        if p.exists():
            return p
    termux = Path("/data/data/com.termux/files/usr/etc/pip.conf")
    if termux.parent.exists():
        return termux
    return PIP_CONF_PATHS[1]

def read_pip_conf(path: Path) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    if path.exists():
        cfg.read(str(path), encoding="utf-8")
    return cfg

def write_pip_conf(path: Path, cfg: configparser.ConfigParser):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        cfg.write(f)

def apply_repo(row, with_fallback: bool = True) -> Path:
    path = find_pip_conf()
    cfg  = read_pip_conf(path)
    if not cfg.has_section("global"):
        cfg.add_section("global")
    cfg.set("global", "index-url", row["url"])
    if with_fallback and row["fallback_url"]:
        cfg.set("global", "extra-index-url", row["fallback_url"])
    elif cfg.has_option("global", "extra-index-url"):
        cfg.remove_option("global", "extra-index-url")
    write_pip_conf(path, cfg)
    return path

def reset_pip(conn) -> Path:
    path = find_pip_conf()
    cfg  = read_pip_conf(path)
    if cfg.has_section("global"):
        for opt in ("index-url", "extra-index-url"):
            if cfg.has_option("global", opt):
                cfg.remove_option("global", opt)
    write_pip_conf(path, cfg)
    conn.execute("UPDATE repos SET active=0")
    conn.commit()
    return path

# ─── Сеть ────────────────────────────────────────────────────────────────────

def http_get(url: str, timeout: int = 10) -> bytes | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": f"{APP_NAME}/{VERSION}"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except Exception:
        return None

def ping_url(url: str) -> int | None:
    t0 = time.monotonic()
    if http_get(url, timeout=8) is None:
        return None
    return int((time.monotonic() - t0) * 1000)


    data = http_get(url)
    try:
        return json.loads(data.decode()) if data else None
    except Exception:
        return None

def fetch_meta(url: str) -> dict | None:
    data = http_get(url)
    try:
        return json.loads(data.decode()) if data else None
    except Exception:
        return None

def pypi_latest_version(package: str) -> str | None:
    """Получить последнюю версию пакета с PyPI."""
    data = http_get(f"https://pypi.org/pypi/{package}/json", timeout=8)
    if not data:
        return None
    try:
        return json.loads(data.decode())["info"]["version"]
    except Exception:
        return None


    """Получить последнюю версию пакета с PyPI."""
    data = http_get(f"https://pypi.org/pypi/{package}/json", timeout=8)
    if not data:
        return None
    try:
        return json.loads(data.decode())["info"]["version"]
    except Exception:
        return None

# ─── Git хелпер ───────────────────────────────────────────────────────────────

def find_mirror_dir(repo_dir_arg: str | None = None) -> Path | None:
    if repo_dir_arg:
        p = Path(repo_dir_arg).expanduser()
        return p if (p / "scripts" / "add_package.py").exists() else None
    candidates = [
        Path.home() / "piphub-repo",
        Path.home() / "pypi-mirror",
        Path.home() / "проекты" / "piphub-repo",
    ]
    for c in candidates:
        if (c / "scripts" / "add_package.py").exists():
            return c
    return None

def git_push_mirror(repo_dir: Path, commit_msg: str) -> bool:
    """git add packages/ → commit → pull --rebase → push в origin (GitHub)."""
    subprocess.call(["git", "add", "packages/"], cwd=repo_dir)
    result = subprocess.run(
        ["git", "commit", "-m", commit_msg],
        cwd=repo_dir, capture_output=True, text=True
    )
    if result.returncode != 0:
        if "nothing to commit" in result.stdout + result.stderr:
            rprint("[yellow]Нечего коммитить — пакеты уже актуальны.[/yellow]")
            return True
        rprint(f"[red]✗ git commit:[/red] {result.stderr.strip()}")
        return False

    subprocess.call(["git", "pull", "--rebase"], cwd=repo_dir)
    rc = subprocess.call(["git", "push"], cwd=repo_dir)
    return rc == 0

# ─── Bash completion ──────────────────────────────────────────────────────────

BASH_COMPLETION = '''
_piphub_repo_completion() {
    local cur prev words cword
    _init_completion || return
    local commands="install uninstall push build update outdated verify sync list use add remove edit info ping search config status doctor bootstrap self-update freeze restore clean stats history export import completion"
    if [[ $cword -eq 1 ]]; then
        COMPREPLY=($(compgen -W "$commands" -- "$cur"))
    fi
}
complete -F _piphub_repo_completion piphub-repo
'''

# ─── Команды ─────────────────────────────────────────────────────────────────

def cmd_list(args, conn):
    repos = all_repos(conn)
    active_name = get_active(conn)
    active_name = active_name["name"] if active_name else None
    if HAS_RICH:
        t = Table(title="piphub-repo — репозитории", header_style="bold cyan")
        t.add_column("", width=2)
        t.add_column("Имя", style="bold")
        t.add_column("Описание")
        t.add_column("Задержка", justify="right")
        t.add_column("Тип", justify="center")
        for r in repos:
            mark  = "●" if r["name"] == active_name else " "
            color = "green" if r["name"] == active_name else "white"
            ping  = f"{r['ping_ms']} мс" if r["ping_ms"] else "—"
            typ   = "[dim]встр.[/dim]" if r["builtin"] else "[yellow]польз.[/yellow]"
            t.add_row(f"[{color}]{mark}[/{color}]",
                      f"[{color}]{escape(r['name'])}[/{color}]",
                      escape(r["display"]), ping, typ)
        console.print(t)
        rprint(f"\n[dim]pip.conf: {find_pip_conf()}[/dim]")
    else:
        for r in repos:
            mark = "●" if r["name"] == active_name else " "
            print(f"{mark} {r['name']:15} {r['display']}")

def cmd_use(args, conn):
    name = args.name
    if not name:
        repos = all_repos(conn)
        rprint("\n[bold cyan]Выбери репозиторий:[/bold cyan]")
        for i, r in enumerate(repos, 1):
            rprint(f"  [bold]{i}.[/bold] {r['name']:15} {r['display']}")
        choice = rinput("\nИмя или номер")
        if choice.isdigit():
            idx = int(choice) - 1
            name = repos[idx]["name"] if 0 <= idx < len(repos) else choice
        else:
            name = choice.strip()
    row = conn.execute("SELECT * FROM repos WHERE name=?", (name,)).fetchone()
    if not row:
        rprint(f"[red]Репозиторий '{name}' не найден.[/red]")
        rprint("Добавь: [bold]piphub-repo add --name <имя> --url <url>[/bold]")
        return
    path = apply_repo(row, with_fallback=not args.no_fallback)
    set_active(conn, name)
    rprint(f"\n[green]✓[/green] Активирован: [bold]{name}[/bold]")
    rprint(f"[dim]  index-url = {row['url']}[/dim]")
    if row["fallback_url"] and not args.no_fallback:
        rprint(f"[dim]  extra-index-url = {row['fallback_url']}[/dim]")
    rprint(f"[dim]  pip.conf = {path}[/dim]")

def cmd_install(args, conn):
    if args.repo:
        row = conn.execute("SELECT * FROM repos WHERE name=?", (args.repo,)).fetchone()
        if not row:
            rprint(f"[red]Репозиторий '{args.repo}' не найден.[/red]"); return
    else:
        row = get_active(conn)
        if not row:
            rprint("[yellow]Нет активного репозитория.[/yellow]")
            rprint("Запусти: [bold]piphub-repo use tastade[/bold]")
            return
    extra = row["fallback_url"] if not args.no_fallback else None
    rprint(f"\n[bold cyan]piphub-repo install[/bold cyan] {', '.join(args.packages)}")
    rprint(f"[dim]  репозиторий: {row['name']} ({row['url']})[/dim]\n")
    cmd = [sys.executable, "-m", "pip", "install", "--index-url", row["url"]]
    if extra: cmd += ["--extra-index-url", extra]
    if args.upgrade: cmd.append("--upgrade")
    if args.no_deps: cmd.append("--no-deps")
    if args.quiet:   cmd.append("-q")
    cmd += args.packages
    rc = subprocess.call(cmd)
    status = "ok" if rc == 0 else "error"
    for pkg in args.packages:
        conn.execute("INSERT INTO install_log (package, repo_name, status) VALUES (?,?,?)",
                     (pkg, row["name"], status))
    conn.commit()
    if rc == 0:
        rprint(f"\n[green]✓[/green] Установлено: {', '.join(args.packages)}")
    else:
        rprint(f"\n[red]✗[/red] Ошибка (код {rc})")

def cmd_uninstall(args, conn):
    rprint(f"\n[bold cyan]piphub-repo uninstall[/bold cyan] {', '.join(args.packages)}")
    rc = subprocess.call([sys.executable, "-m", "pip", "uninstall", "-y"] + args.packages)
    if rc == 0:
        rprint(f"\n[green]✓[/green] Удалено: {', '.join(args.packages)}")
    else:
        rprint(f"\n[red]✗[/red] Ошибка (код {rc})")

def cmd_push(args, conn):
    repo_dir = find_mirror_dir(args.repo_dir)
    if repo_dir is None:
        rprint("[red]Не найдена папка зеркала.[/red]")
        rprint("Укажи: [bold]piphub-repo push requests --dir ~/piphub-repo[/bold]")
        return

    rprint(f"\n[bold cyan]piphub-repo push[/bold cyan] → {', '.join(args.packages)}")
    rprint(f"[dim]  зеркало: {repo_dir}[/dim]\n")

    # Скачиваем пакеты
    rprint("[bold]Шаг 1/4:[/bold] Скачиваю пакеты...")
    pip_cmd = [sys.executable, "-m", "pip", "download",
               "--dest", str(repo_dir / "packages")]
    if not args.with_deps:
        pip_cmd.append("--no-deps")
    pip_cmd += args.packages
    rc = subprocess.call(pip_cmd)
    if rc != 0:
        rprint("[red]✗ Ошибка при скачивании[/red]"); return

    rprint("\n[bold]Шаг 2-4/4:[/bold] Коммит и push...")
    ok = git_push_mirror(repo_dir, f"feat: add {', '.join(args.packages)}")
    if ok:
        rprint(f"\n[green]✓[/green] Готово! [{', '.join(args.packages)}] добавлены в зеркало.")
        rprint("[dim]  CI пересоберёт индекс через ~30 секунд[/dim]")
    else:
        rprint("[red]✗ Ошибка при push[/red]")

def cmd_build(args, conn):
    """Собирает .whl из .py/.sh/других файлов и пушит в зеркало."""
    repo_dir = find_mirror_dir(args.repo_dir)
    if repo_dir is None:
        rprint("[red]Не найдена папка зеркала.[/red]")
        rprint("Укажи: [bold]piphub-repo build file.py --dir ~/piphub-repo[/bold]")
        return

    files = [Path(f) for f in args.files]
    missing = [f for f in files if not f.exists()]
    if missing:
        rprint(f"[red]Файлы не найдены: {', '.join(str(f) for f in missing)}[/red]")
        return

    # Определяем имя пакета
    pkg_name = args.name
    if not pkg_name:
        # Берём имя первого файла без расширения
        pkg_name = re.sub(r'[^a-z0-9_]', '_', files[0].stem.lower())

    pkg_version = args.version or "1.0.0"
    pkg_author  = args.author  or "Tastade"
    pkg_desc    = args.description or f"Package {pkg_name}"

    rprint(f"\n[bold cyan]piphub-repo build[/bold cyan] → {pkg_name} v{pkg_version}")
    rprint(f"[dim]  файлы: {', '.join(str(f) for f in files)}[/dim]")
    rprint(f"[dim]  зеркало: {repo_dir}[/dim]\n")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        pkg_dir = tmp / pkg_name
        pkg_dir.mkdir()

        # Копируем файлы
        py_modules = []
        data_files = []
        scripts_list = []

        for f in files:
            dest = pkg_dir / f.name
            shutil.copy2(f, dest)

            if f.suffix == ".py":
                py_modules.append(f.stem)
            elif f.suffix in (".sh", ".bash"):
                # shell-скрипты — делаем исполняемыми и добавляем как scripts
                dest.chmod(0o755)
                scripts_list.append(f"bin/{f.name}")
                bin_dir = pkg_dir / "bin"
                bin_dir.mkdir(exist_ok=True)
                shutil.copy2(f, bin_dir / f.name)
                (bin_dir / f.name).chmod(0o755)
            else:
                data_files.append(f.name)

        # Создаём __init__.py
        (pkg_dir / "__init__.py").write_text(
            f'"""Package {pkg_name} v{pkg_version}"""\n__version__ = "{pkg_version}"\n',
            encoding="utf-8"
        )

        # setup.py
        py_mods_str = str(py_modules) if py_modules else "[]"
        scripts_str = str(scripts_list) if scripts_list else "[]"
        setup_py = textwrap.dedent(f"""
            from setuptools import setup, find_packages
            setup(
                name="{pkg_name}",
                version="{pkg_version}",
                author="{pkg_author}",
                description="{pkg_desc}",
                packages=find_packages(),
                py_modules={py_mods_str},
                scripts={scripts_str},
                python_requires=">=3.8",
            )
        """).strip()
        (tmp / "setup.py").write_text(setup_py, encoding="utf-8")

        # pyproject.toml (для совместимости)
        pyproject = textwrap.dedent(f"""
            [build-system]
            requires = ["setuptools>=61"]
            build-backend = "setuptools.backends.legacy:build"
        """).strip()
        (tmp / "pyproject.toml").write_text(pyproject, encoding="utf-8")

        rprint("[bold]Шаг 1/4:[/bold] Сборка wheel...")
        rc = subprocess.call(
            [sys.executable, "-m", "pip", "wheel", ".", "--no-deps",
             "-w", str(repo_dir / "packages")],
            cwd=tmp
        )
        if rc != 0:
            rprint("[red]✗ Ошибка сборки wheel[/red]")
            rprint("[dim]  Убедись что установлен setuptools: pip install setuptools wheel[/dim]")
            return

    rprint("\n[bold]Шаг 2-4/4:[/bold] Коммит и push...")
    ok = git_push_mirror(repo_dir, f"feat: build {pkg_name} v{pkg_version}")
    if ok:
        rprint(f"\n[green]✓[/green] Пакет [bold]{pkg_name} v{pkg_version}[/bold] собран и добавлен в зеркало.")
        rprint("[dim]  CI пересоберёт индекс через ~30 секунд[/dim]")
    else:
        rprint("[red]✗ Ошибка при push[/red]")

def cmd_update(args, conn):
    """Обновить все пакеты в зеркале до последних версий."""
    repo_dir = find_mirror_dir(args.repo_dir)
    if repo_dir is None:
        rprint("[red]Не найдена папка зеркала.[/red]"); return

    packages_dir = repo_dir / "packages"
    if not packages_dir.exists():
        rprint("[yellow]Папка packages/ пуста.[/yellow]"); return

    # Собираем уникальные имена пакетов из файлов
    pkg_names: dict[str, str] = {}  # name → current_version
    for f in packages_dir.iterdir():
        if f.suffix == ".whl":
            parts = f.stem.split("-")
            if len(parts) >= 2:
                pkg_names[parts[0].lower()] = parts[1]
        elif f.name.endswith(".tar.gz"):
            base = f.name[:-7]
            idx = base.rfind("-")
            if idx > 0:
                pkg_names[base[:idx].lower()] = base[idx+1:]

    if not pkg_names:
        rprint("[yellow]Нет пакетов в зеркале.[/yellow]"); return

    rprint(f"\n[bold cyan]piphub-repo update[/bold cyan] — проверяю {len(pkg_names)} пакетов...\n")

    to_update = []
    for name, cur_ver in sorted(pkg_names.items()):
        latest = pypi_latest_version(name)
        if latest and latest != cur_ver:
            to_update.append((name, cur_ver, latest))
            rprint(f"  [yellow]↑[/yellow] {name}: {cur_ver} → [green]{latest}[/green]")
        else:
            rprint(f"  [dim]✓ {name}: {cur_ver}[/dim]")

    if not to_update:
        rprint("\n[green]Все пакеты актуальны.[/green]"); return

    rprint(f"\n[bold]Обновляю {len(to_update)} пакетов...[/bold]")
    for name, cur_ver, latest in to_update:
        # Удаляем старые файлы этого пакета
        for f in packages_dir.iterdir():
            if f.stem.startswith(f"{name}-") or f.stem.lower().startswith(f"{name}-"):
                f.unlink()
        # Скачиваем новую версию
        subprocess.call([sys.executable, "-m", "pip", "download",
                         name, "--dest", str(packages_dir), "--no-deps"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    ok = git_push_mirror(repo_dir, f"chore: update {len(to_update)} packages")
    if ok:
        rprint(f"\n[green]✓[/green] Обновлено {len(to_update)} пакетов.")
    else:
        rprint("[red]✗ Ошибка при push[/red]")

def cmd_outdated(args, conn):
    """Показать устаревшие пакеты в зеркале."""
    repo_dir = find_mirror_dir(args.repo_dir)
    if repo_dir is None:
        rprint("[red]Не найдена папка зеркала.[/red]"); return

    packages_dir = repo_dir / "packages"
    if not packages_dir.exists():
        rprint("[yellow]Папка packages/ пуста.[/yellow]"); return
    pkg_names: dict[str, str] = {}
    for f in packages_dir.iterdir():
        if f.suffix == ".whl":
            parts = f.stem.split("-")
            if len(parts) >= 2:
                pkg_names[parts[0].lower()] = parts[1]

    if not pkg_names:
        rprint("[yellow]Нет пакетов в зеркале.[/yellow]"); return

    rprint(f"\n[bold cyan]piphub-repo outdated[/bold cyan] — проверяю {len(pkg_names)} пакетов...\n")

    outdated = []
    for name, cur_ver in sorted(pkg_names.items()):
        latest = pypi_latest_version(name)
        if latest and latest != cur_ver:
            outdated.append((name, cur_ver, latest))

    if not outdated:
        rprint("[green]Все пакеты актуальны.[/green]"); return

    if HAS_RICH:
        t = Table(title=f"Устаревшие пакеты ({len(outdated)})", header_style="bold cyan")
        t.add_column("Пакет"); t.add_column("В зеркале"); t.add_column("Последняя")
        for name, cur, latest in outdated:
            t.add_row(name, f"[yellow]{cur}[/yellow]", f"[green]{latest}[/green]")
        console.print(t)
        rprint("\nОбновить всё: [bold]piphub-repo update[/bold]")
    else:
        for name, cur, latest in outdated:
            print(f"  {name:25} {cur:15} → {latest}")

def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()

def cmd_verify(args, conn):
    """Проверяет целостность зеркала: sha256, отсутствующие файлы, дубликаты, файлы не в индексе."""
    repo_dir = find_mirror_dir(args.repo_dir)
    if repo_dir is None:
        rprint("[red]Не найдена папка зеркала.[/red]"); return

    packages_dir = repo_dir / "packages"
    meta_file = repo_dir / "mirror_meta.json"

    if not packages_dir.exists():
        rprint("[yellow]Папка packages/ не найдена.[/yellow]"); return

    rprint(f"\n[bold cyan]piphub-repo verify[/bold cyan] — проверка целостности\n")
    rprint(f"[dim]  зеркало: {repo_dir}[/dim]\n")

    # Реальные файлы на диске
    disk_files: dict[str, Path] = {}
    for f in packages_dir.rglob("*"):
        if f.is_file() and any(f.name.endswith(e) for e in (".whl", ".tar.gz", ".zip", ".tar.bz2")):
            disk_files[f.name] = f

    if not disk_files:
        rprint("[yellow]В packages/ нет файлов пакетов.[/yellow]"); return

    rprint(f"Найдено файлов на диске: [bold]{len(disk_files)}[/bold]")

    problems = []
    checked = 0
    indexed_names = set()

    if meta_file.exists():
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            packages_meta = meta.get("packages", {})
        except Exception as e:
            rprint(f"[red]✗ mirror_meta.json повреждён: {e}[/red]")
            packages_meta = {}

        # Сверяем sha256 для каждого файла из индекса
        for norm_name, files in packages_meta.items():
            for finfo in files:
                fname = finfo.get("filename")
                expected_sha = finfo.get("sha256")
                indexed_names.add(fname)

                if fname not in disk_files:
                    problems.append(f"[red]Отсутствует файл[/red]: {fname} (указан в индексе, но нет на диске)")
                    continue

                checked += 1
                actual_sha = _sha256_file(disk_files[fname])
                if actual_sha != expected_sha:
                    problems.append(
                        f"[red]SHA256 не совпадает[/red]: {fname}\n"
                        f"    ожидалось: {expected_sha[:16]}...\n"
                        f"    реально:   {actual_sha[:16]}..."
                    )
    else:
        problems.append("[yellow]mirror_meta.json не найден — индекс ещё не собран (запусти push/build, дождись CI)[/yellow]")

    # Файлы на диске, которых нет в индексе (новые, ещё не проиндексированы)
    orphan_files = set(disk_files.keys()) - indexed_names
    for fname in sorted(orphan_files):
        problems.append(f"[yellow]Не в индексе[/yellow]: {fname} (CI ещё не пересобрал индекс, или ошибка сборки)")

    # Проверка на дубликаты разных версий (просто информативно, не ошибка)
    rprint(f"Проверено sha256: [bold]{checked}[/bold] файлов\n")

    if not problems:
        rprint("[bold green]✓ Зеркало целостно, проблем не найдено.[/bold green]")
    else:
        rprint(f"[bold yellow]Найдено проблем: {len(problems)}[/bold yellow]\n")
        for p in problems:
            rprint(f"  {p}")
        rprint(f"\n[dim]Подсказка: для отсутствующих/повреждённых файлов запусти piphub-repo push <пакет> заново.[/dim]")
        rprint(f"[dim]Для файлов 'не в индексе' — подожди ~30 сек после push, CI ещё пересобирает индекс.[/dim]")

def cmd_sync(args, conn):
    """Синхронизировать кеш метаданных зеркала."""
    active = get_active(conn)
    name = args.repo or (active["name"] if active else None)
    if not name:
        rprint("[yellow]Нет активного репозитория.[/yellow]"); return
    row = conn.execute("SELECT * FROM repos WHERE name=?", (name,)).fetchone()
    if not row or not row["meta_url"]:
        rprint(f"[yellow]У репозитория '{name}' нет meta_url.[/yellow]"); return
    rprint(f"[dim]Синхронизирую метаданные {name}...[/dim]")
    meta = fetch_meta(row["meta_url"])
    if not meta:
        rprint("[red]Не удалось загрузить метаданные.[/red]"); return
    packages = meta.get("packages", {})
    generated = meta.get("generated", "—")
    rprint(f"[green]✓[/green] Синхронизировано [bold]{len(packages)}[/bold] пакетов.")
    rprint(f"[dim]  Обновлено на зеркале: {generated}[/dim]")

def cmd_add(args, conn):
    rprint("\n[bold cyan]Добавление репозитория[/bold cyan]")
    name     = args.name        or rinput("Короткое имя")
    url      = args.url         or rinput("URL /simple/")
    display  = args.display     or rinput("Название", default=name)
    fallback = args.fallback    or rinput("Fallback URL", default="https://pypi.org/simple/")
    meta_url = args.meta_url    or rinput("URL mirror_meta.json (Enter — нет)", default="")
    desc     = args.description or rinput("Описание", default="")
    if not url.endswith("/"):
        url += "/"
    existing = conn.execute("SELECT id FROM repos WHERE name=?", (name,)).fetchone()
    if existing:
        if not rconfirm(f"[yellow]'{name}' уже существует. Перезаписать?[/yellow]"):
            return
        conn.execute("DELETE FROM repos WHERE name=?", (name,))
    conn.execute("""
        INSERT INTO repos (name, display, url, meta_url, fallback_url, description)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (name, display, url, meta_url or None, fallback or None, desc))
    conn.commit()
    rprint(f"\n[green]✓[/green] Добавлен: [bold]{name}[/bold]")
    if rconfirm("Активировать сейчас?"):
        row = conn.execute("SELECT * FROM repos WHERE name=?", (name,)).fetchone()
        apply_repo(row)
        set_active(conn, name)
        rprint("[green]✓[/green] Активирован, pip.conf обновлён.")

def cmd_remove(args, conn):
    row = conn.execute("SELECT * FROM repos WHERE name=?", (args.name,)).fetchone()
    if not row:
        rprint(f"[red]'{args.name}' не найден.[/red]"); return
    if row["builtin"] and not args.force:
        rprint(f"[yellow]'{args.name}' встроенный. Используй --force.[/yellow]"); return
    if not rconfirm(f"Удалить [bold]{args.name}[/bold]?"):
        return
    conn.execute("DELETE FROM repos WHERE name=?", (args.name,))
    conn.commit()
    rprint("[green]✓[/green] Удалён.")

def cmd_edit(args, conn):
    row = conn.execute("SELECT * FROM repos WHERE name=?", (args.name,)).fetchone()
    if not row:
        rprint(f"[red]'{args.name}' не найден.[/red]"); return
    rprint(f"\n[bold cyan]Редактирование: {args.name}[/bold cyan]")
    rprint("[dim]Enter — оставить текущее значение[/dim]\n")
    display  = rinput("Название",    default=row["display"])
    url      = rinput("URL",         default=row["url"])
    fallback = rinput("Fallback URL",default=row["fallback_url"] or "")
    meta_url = rinput("Meta URL",    default=row["meta_url"] or "")
    desc     = rinput("Описание",    default=row["description"] or "")
    if not url.endswith("/"):
        url += "/"
    conn.execute("""
        UPDATE repos SET display=?, url=?, fallback_url=?, meta_url=?, description=?
        WHERE name=?
    """, (display, url, fallback or None, meta_url or None, desc, args.name))
    conn.commit()
    rprint(f"\n[green]✓[/green] [bold]{args.name}[/bold] обновлён.")
    if row["active"] and rconfirm("Применить к pip.conf?"):
        updated = conn.execute("SELECT * FROM repos WHERE name=?", (args.name,)).fetchone()
        apply_repo(updated)
        rprint("[green]✓[/green] pip.conf обновлён.")

def cmd_info(args, conn):
    row = conn.execute("SELECT * FROM repos WHERE name=?", (args.name,)).fetchone()
    if not row:
        rprint(f"[red]'{args.name}' не найден.[/red]"); return
    if HAS_RICH:
        lines = [
            f"[bold]Имя:[/bold]         {row['name']}",
            f"[bold]Название:[/bold]    {row['display']}",
            f"[bold]URL:[/bold]         {row['url']}",
            f"[bold]Fallback:[/bold]    {row['fallback_url'] or '—'}",
            f"[bold]Meta URL:[/bold]    {row['meta_url'] or '—'}",
            f"[bold]Описание:[/bold]    {row['description'] or '—'}",
            f"[bold]Тип:[/bold]         {'встроенный' if row['builtin'] else 'пользовательский'}",
            f"[bold]Активный:[/bold]    {'[green]да[/green]' if row['active'] else 'нет'}",
            f"[bold]Задержка:[/bold]    {str(row['ping_ms']) + ' мс' if row['ping_ms'] else '—'}",
        ]
        console.print(Panel("\n".join(lines), title=f"[cyan]{row['name']}[/cyan]", border_style="cyan"))
    else:
        for k, v in [("Имя", row["name"]), ("URL", row["url"]),
                     ("Активный", "да" if row["active"] else "нет")]:
            print(f"  {k}: {v}")

def cmd_ping(args, conn):
    targets = ([conn.execute("SELECT * FROM repos WHERE name=?", (args.name,)).fetchone()]
               if args.name else all_repos(conn))
    if args.name and not targets[0]:
        rprint(f"[red]'{args.name}' не найден.[/red]"); return
    rprint("\n[bold cyan]Проверка репозиториев...[/bold cyan]\n")
    results = []
    for r in targets:
        ms = ping_url(r["url"])
        if ms is not None:
            conn.execute("UPDATE repos SET ping_ms=?, last_ping=datetime('now') WHERE name=?",
                         (ms, r["name"]))
        results.append((r["name"], r["display"], ms))
    conn.commit()
    if HAS_RICH:
        t = Table(header_style="bold cyan")
        t.add_column("Имя"); t.add_column("Название"); t.add_column("Результат", justify="right")
        for name, disp, ms in results:
            if ms is None:     res = "[red]недоступен[/red]"
            elif ms < 500:     res = f"[green]{ms} мс[/green]"
            else:              res = f"[yellow]{ms} мс[/yellow]"
            t.add_row(name, disp, res)
        console.print(t)
    else:
        for name, disp, ms in results:
            print(f"  {name:15} {ms} мс" if ms else f"  {name:15} недоступен")

def cmd_search(args, conn):
    active = get_active(conn)
    name = args.repo or (active["name"] if active else None)
    if not name:
        rprint("[yellow]Нет активного репозитория.[/yellow]"); return
    row = conn.execute("SELECT * FROM repos WHERE name=?", (name,)).fetchone()
    if not row or not row["meta_url"]:
        rprint(f"[yellow]У репозитория '{name}' нет meta_url.[/yellow]")
        rprint("[dim]Поиск работает только для зеркал с mirror_meta.json[/dim]")
        return
    rprint(f"[dim]Загружаю метаданные {name}...[/dim]")
    meta = fetch_meta(row["meta_url"])
    if not meta:
        rprint("[red]Не удалось загрузить метаданные.[/red]"); return
    query = args.query.lower()
    found = [(k, v) for k, v in meta.get("packages", {}).items() if query in k]
    if not found:
        rprint(f"[yellow]Ничего по запросу '{args.query}'[/yellow]"); return
    rprint(f"\n[bold]Найдено {len(found)} пакетов в [{name}]:[/bold]\n")
    if HAS_RICH:
        t = Table(header_style="bold cyan")
        t.add_column("Пакет"); t.add_column("Файлов", justify="right"); t.add_column("Размер", justify="right")
        for pkg, files in sorted(found):
            size = sum(f.get("size", 0) for f in files)
            t.add_row(pkg, str(len(files)), f"{size/1048576:.2f} MB" if size else "—")
        console.print(t)
    else:
        for pkg, files in sorted(found):
            print(f"  {pkg:30} {len(files)} файл(ов)")

def cmd_config(args, conn):
    path = find_pip_conf()
    if args.show:
        if path.exists():
            content = path.read_text(encoding="utf-8")
            if HAS_RICH:
                console.print(Panel(Syntax(content, "ini", theme="monokai"),
                                    title=str(path), border_style="cyan"))
            else:
                print(f"─── {path} ───\n{content}")
        else:
            rprint(f"[yellow]pip.conf не найден: {path}[/yellow]")
        return
    if args.reset:
        p = reset_pip(conn)
        rprint(f"[green]✓[/green] pip.conf сброшен ({p})")
        return
    if args.set_key and args.set_value:
        cfg = read_pip_conf(path)
        if not cfg.has_section("global"):
            cfg.add_section("global")
        cfg.set("global", args.set_key, args.set_value)
        write_pip_conf(path, cfg)
        rprint(f"[green]✓[/green] {args.set_key} = {args.set_value}")
        return
    rprint(f"[bold cyan]piphub-repo config[/bold cyan]  |  pip.conf: [dim]{path}[/dim]")
    rprint("  --show              показать pip.conf")
    rprint("  --reset             сбросить index-url")
    rprint("  --set KEY VALUE     установить параметр")

def _disk_usage(path: Path) -> tuple[int, int, int] | None:
    """Возвращает (total, used, free) в байтах, или None если недоступно."""
    try:
        usage = shutil.disk_usage(path)
        return usage.total, usage.used, usage.free
    except Exception:
        return None

def _check_cmd_version(cmd: list[str]) -> str | None:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        out = (result.stdout or result.stderr).strip().splitlines()
        return out[0] if out else None
    except Exception:
        return None

def cmd_bootstrap(args, conn):
    """
    Полная автоматическая настройка окружения с нуля:
    git, credential.helper, клонирование зеркала, активация репозитория.
    Полезно после сброса Termux.
    """
    rprint(f"\n[bold cyan]piphub-repo bootstrap[/bold cyan] — автоматическая настройка окружения\n")

    steps_done = []
    steps_failed = []

    # 1. Проверка/установка git
    rprint("[bold]Шаг 1/5:[/bold] Проверка git...")
    if _check_cmd_version(["git", "--version"]):
        rprint("[green]  ✓ git уже установлен[/green]")
        steps_done.append("git")
    else:
        rprint("[dim]  Устанавливаю git через pkg...[/dim]")
        rc = subprocess.call(["pkg", "install", "-y", "git"])
        if rc == 0:
            steps_done.append("git (установлен)")
        else:
            steps_failed.append("git — не удалось установить, установи вручную: pkg install git")

    # 2. git config (имя и email)
    rprint("\n[bold]Шаг 2/5:[/bold] Настройка git identity...")
    name = _check_cmd_version(["git", "config", "--global", "user.name"])
    email = _check_cmd_version(["git", "config", "--global", "user.email"])
    if name and email:
        rprint(f"[green]  ✓ Уже настроено: {name} <{email}>[/green]")
        steps_done.append("git identity")
    else:
        git_name = args.git_name or rinput("Имя для git (user.name)")
        git_email = args.git_email or rinput("Email для git (user.email)")
        subprocess.call(["git", "config", "--global", "user.name", git_name])
        subprocess.call(["git", "config", "--global", "user.email", git_email])
        steps_done.append(f"git identity ({git_name})")

    # 3. credential.helper
    rprint("\n[bold]Шаг 3/5:[/bold] Настройка credential.helper...")
    cred = _check_cmd_version(["git", "config", "--global", "credential.helper"])
    if cred:
        rprint(f"[green]  ✓ Уже настроено: {cred}[/green]")
        steps_done.append("credential.helper")
    else:
        subprocess.call(["git", "config", "--global", "credential.helper", "store"])
        rprint("[green]  ✓ credential.helper = store[/green]")
        steps_done.append("credential.helper")

    # 4. Клонирование зеркала
    rprint("\n[bold]Шаг 4/5:[/bold] Клонирование зеркала...")
    mirror_dir = find_mirror_dir(args.repo_dir)
    if mirror_dir:
        rprint(f"[green]  ✓ Зеркало уже есть: {mirror_dir}[/green]")
        steps_done.append("зеркало (уже клонировано)")
    else:
        target = Path(args.repo_dir).expanduser() if args.repo_dir else Path.home() / "piphub-repo"
        repo_url = args.repo_url or "https://github.com/Tastade/piphub-repo"
        if target.exists() and any(target.iterdir()):
            rprint(f"[yellow]  ⚠ Папка {target} существует и не пуста, пропускаю клонирование[/yellow]")
            steps_failed.append(f"клонирование — папка {target} занята")
        else:
            rc = subprocess.call(["git", "clone", repo_url, str(target)])
            if rc == 0:
                rprint(f"[green]  ✓ Склонировано в {target}[/green]")
                steps_done.append(f"зеркало склонировано в {target}")
            else:
                steps_failed.append(f"клонирование зеркала из {repo_url}")

    # 5. Активация репозитория
    rprint("\n[bold]Шаг 5/5:[/bold] Активация репозитория...")
    repo_name = args.use_repo or "tastade"
    row = conn.execute("SELECT * FROM repos WHERE name=?", (repo_name,)).fetchone()
    if row:
        apply_repo(row, with_fallback=True)
        set_active(conn, repo_name)
        rprint(f"[green]  ✓ Активирован репозиторий '{repo_name}'[/green]")
        steps_done.append(f"репозиторий '{repo_name}' активирован")
    else:
        steps_failed.append(f"репозиторий '{repo_name}' не найден")

    # Итог
    rprint(f"\n[bold cyan]{'─'*50}[/bold cyan]")
    if HAS_RICH:
        console.print(Panel(
            "\n".join(f"[green]✓[/green] {s}" for s in steps_done),
            title="Готово", border_style="green"
        ))
        if steps_failed:
            console.print(Panel(
                "\n".join(f"[red]✗[/red] {s}" for s in steps_failed),
                title="Требует внимания", border_style="red"
            ))
    else:
        print("Готово:")
        for s in steps_done:
            print(f"  [OK] {s}")
        if steps_failed:
            print("Требует внимания:")
            for s in steps_failed:
                print(f"  [!] {s}")

    if not steps_failed:
        rprint("\n[bold green]Окружение полностью готово к работе![/bold green]")
        rprint("[dim]Проверь: piphub-repo doctor[/dim]")

def cmd_self_update(args, conn):
    """Проверяет новую версию piphub-repo на GitHub Releases и обновляется."""
    rprint(f"\n[bold cyan]piphub-repo self-update[/bold cyan] — текущая версия: {VERSION}\n")

    repo_slug = args.repo_slug or "Tastade/piphub-repo"
    api_url = f"https://api.github.com/repos/{repo_slug}/releases/latest"

    rprint("[dim]Проверяю последнюю версию на GitHub Releases...[/dim]")
    data = http_get(api_url, timeout=10)
    if not data:
        rprint("[red]✗ Не удалось связаться с GitHub Releases.[/red]")
        rprint(f"[dim]  Проверь подключение или укажи репозиторий: --repo владелец/репо[/dim]")
        return

    try:
        release = json.loads(data.decode())
    except Exception:
        rprint("[red]✗ Некорректный ответ от GitHub API.[/red]")
        return

    latest_tag = release.get("tag_name", "").lstrip("v")
    if not latest_tag:
        rprint("[yellow]Релизы не найдены в репозитории.[/yellow]")
        rprint(f"[dim]  Репозиторий: github.com/{repo_slug}[/dim]")
        return

    rprint(f"[dim]  Последняя версия на GitHub: {latest_tag}[/dim]")

    if latest_tag == VERSION:
        rprint(f"[green]✓ У тебя уже последняя версия ({VERSION}).[/green]")
        return

    rprint(f"\n[yellow]Доступна новая версия: [bold]{latest_tag}[/bold] (у тебя {VERSION})[/yellow]")

    # Приоритет: .whl (py3-none-any) → .tar.gz → zipball
    assets = release.get("assets", [])
    whl_url = None
    sdist_url = None
    for a in assets:
        name = a.get("name", "")
        url  = a.get("browser_download_url", "")
        if name.endswith(".whl") and "py3-none-any" in name:
            whl_url = url
        elif name.endswith(".tar.gz") and not sdist_url:
            sdist_url = url

    download_url  = whl_url or sdist_url or release.get("zipball_url")
    download_type = "wheel" if whl_url else ("sdist" if sdist_url else "zip")

    if not download_url:
        rprint("[red]✗ Не найдено файлов для скачивания в релизе.[/red]")
        rprint(f"[dim]  Проверь github.com/{repo_slug}/releases[/dim]")
        return

    rprint(f"[dim]  Тип: {download_type}, URL: {download_url}[/dim]")

    if not args.yes and not rconfirm(f"\nОбновиться до {latest_tag}?"):
        rprint("[dim]Отменено.[/dim]")
        return

    rprint(f"\n[dim]Устанавливаю...[/dim]")
    rc = subprocess.call([
        sys.executable, "-m", "pip", "install",
        "--upgrade", "--no-cache-dir", download_url
    ])
    if rc == 0:
        rprint(f"\n[green]✓[/green] Обновлено до версии [bold]{latest_tag}[/bold]!")
        rprint("[dim]  Перезапусти piphub-repo чтобы изменения вступили в силу.[/dim]")
    else:
        rprint(f"\n[red]✗[/red] Ошибка обновления (код {rc})")
        rprint(f"[dim]  Попробуй вручную: pip install --upgrade piphub-repo[/dim]")

def cmd_doctor(args, conn):
    """Диагностика окружения Termux: место на диске, версии, конфиги, доступность зеркал."""
    rprint(f"\n[bold cyan]piphub-repo doctor[/bold cyan] — проверка окружения\n")

    issues = []
    oks = []

    # 1. Диск
    home = Path.home()
    du = _disk_usage(home)
    if du:
        total, used, free = du
        free_mb = free / 1_048_576
        used_pct = (used / total * 100) if total else 0
        if free_mb < 100:
            issues.append(f"Диск почти заполнен: свободно всего {free_mb:.0f} MB ({used_pct:.0f}% занято)")
        else:
            oks.append(f"Диск: свободно {free_mb:.0f} MB ({used_pct:.0f}% занято)")
    else:
        issues.append("Не удалось проверить место на диске")

    # 2. Python
    py_ver = sys.version.split()[0]
    oks.append(f"Python {py_ver}")

    # 3. git
    git_ver = _check_cmd_version(["git", "--version"])
    if git_ver:
        oks.append(git_ver)
    else:
        issues.append("git не найден — установи: pkg install git")

    # 4. pip
    pip_ver = _check_cmd_version([sys.executable, "-m", "pip", "--version"])
    if pip_ver:
        oks.append(pip_ver.split(" from ")[0])
    else:
        issues.append("pip недоступен")

    # 5. rich
    if HAS_RICH:
        oks.append("rich установлен (цветной интерфейс активен)")
    else:
        issues.append("rich не установлен — pip install rich для красивого интерфейса")

    # 6. pip.conf
    pip_conf_path = find_pip_conf()
    if pip_conf_path.exists():
        cfg = read_pip_conf(pip_conf_path)
        index_url = cfg.get("global", "index-url", fallback=None)
        if index_url:
            oks.append(f"pip.conf настроен: {index_url}")
        else:
            issues.append(f"pip.conf существует, но index-url не задан ({pip_conf_path})")
    else:
        issues.append(f"pip.conf не найден ({pip_conf_path}) — используй piphub-repo use")

    # 7. Активный репозиторий
    active = get_active(conn)
    if active:
        oks.append(f"Активный репозиторий: {active['name']}")
    else:
        issues.append("Нет активного репозитория — piphub-repo use tastade")

    # 8. Доступность активного зеркала
    if active:
        ms = ping_url(active["url"])
        if ms is not None:
            oks.append(f"Зеркало '{active['name']}' доступно ({ms} мс)")
        else:
            issues.append(f"Зеркало '{active['name']}' недоступно — проверь интернет")

    # 9. credential.helper (чтобы git push не спрашивал пароль каждый раз)
    cred_helper = _check_cmd_version(["git", "config", "--global", "credential.helper"])
    if cred_helper:
        oks.append(f"git credential.helper настроен ({cred_helper})")
    else:
        issues.append("git credential.helper не настроен — придётся вводить токен каждый push")

    # 10. Найдена ли папка зеркала локально
    mirror_dir = find_mirror_dir()
    if mirror_dir:
        oks.append(f"Локальное зеркало найдено: {mirror_dir}")
        if not (mirror_dir / "packages").exists():
            issues.append(f"В {mirror_dir} нет папки packages/")
    else:
        issues.append("Локальная папка зеркала не найдена (~/piphub-repo) — не критично, если используешь только install")

    # Вывод
    if HAS_RICH:
        if oks:
            console.print(Panel(
                "\n".join(f"[green]✓[/green] {o}" for o in oks),
                title="В порядке", border_style="green"
            ))
        if issues:
            console.print(Panel(
                "\n".join(f"[yellow]⚠[/yellow] {i}" for i in issues),
                title="Внимание", border_style="yellow"
            ))
        if not issues:
            rprint("\n[bold green]Всё в порядке, проблем не найдено.[/bold green]")
        else:
            rprint(f"\n[bold yellow]Найдено проблем: {len(issues)}[/bold yellow]")
    else:
        print("\n=== В порядке ===")
        for o in oks:
            print(f"  [OK] {o}")
        if issues:
            print("\n=== Внимание ===")
            for i in issues:
                print(f"  [!] {i}")
        else:
            print("\nВсё в порядке, проблем не найдено.")

def cmd_status(args, conn):
    active = get_active(conn)
    path   = find_pip_conf()
    cfg    = read_pip_conf(path)
    index  = cfg.get("global", "index-url", fallback="(не задан)")
    extra  = cfg.get("global", "extra-index-url", fallback="—")
    if HAS_RICH:
        header = (f"[green]●[/green] Активный: [bold]{active['name']}[/bold]"
                  if active else "[yellow]●[/yellow] Репозиторий не выбран")
        console.print(Panel(
            f"{header}\n[dim]{'─'*45}[/dim]\n"
            f"[bold]pip.conf:[/bold]    {path}\n"
            f"[bold]index-url:[/bold]   {index}\n"
            f"[bold]extra-index:[/bold] {extra}",
            title=f"piphub-repo v{VERSION}", border_style="cyan"))
    else:
        print(f"Активный:    {active['name'] if active else 'не выбран'}")
        print(f"index-url:   {index}")

def cmd_history(args, conn):
    rows = conn.execute("""
        SELECT package, repo_name, status, installed_at
        FROM install_log ORDER BY id DESC LIMIT ?
    """, (args.limit,)).fetchall()
    if not rows:
        rprint("[dim]История пуста.[/dim]"); return
    if HAS_RICH:
        t = Table(title="История установок", header_style="bold cyan")
        t.add_column("Пакет"); t.add_column("Репозиторий")
        t.add_column("Статус"); t.add_column("Дата")
        for r in rows:
            color = "green" if r["status"] == "ok" else "red"
            t.add_row(r["package"], r["repo_name"] or "—",
                      f"[{color}]{r['status']}[/{color}]", r["installed_at"])
        console.print(t)
    else:
        for r in rows:
            print(f"  {r['package']:25} {r['status']:6} {r['installed_at']}")

def cmd_export(args, conn):
    repos = conn.execute("SELECT * FROM repos WHERE builtin=0").fetchall()
    data  = [dict(r) for r in repos]
    out   = Path(args.output) if args.output else Path("piphub_repos.json")
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    rprint(f"[green]✓[/green] Экспортировано {len(data)} репозиториев → {out}")

def cmd_import(args, conn):
    src = Path(args.input)
    if not src.exists():
        rprint(f"[red]Файл не найден: {src}[/red]"); return
    data = json.loads(src.read_text(encoding="utf-8"))
    count = 0
    for r in data:
        try:
            conn.execute("""
                INSERT OR REPLACE INTO repos
                  (name, display, url, meta_url, fallback_url, description)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (r["name"], r.get("display", r["name"]), r["url"],
                  r.get("meta_url"), r.get("fallback_url"), r.get("description", "")))
            count += 1
        except Exception as e:
            rprint(f"[yellow]Пропущен {r.get('name')}: {e}[/yellow]")
    conn.commit()
    rprint(f"[green]✓[/green] Импортировано {count} репозиториев.")

def cmd_freeze(args, conn):
    """Сохраняет список всех установленных pip-пакетов в файл для восстановления."""
    output = Path(args.output) if args.output else Path.home() / "piphub-freeze.json"

    rprint(f"\n[bold cyan]piphub-repo freeze[/bold cyan]\n")

    # Получаем список всех установленных пакетов через pip list
    result = subprocess.run(
        [sys.executable, "-m", "pip", "list", "--format=json"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        rprint("[red]✗ Не удалось получить список пакетов.[/red]"); return

    try:
        installed = json.loads(result.stdout)
    except Exception:
        rprint("[red]✗ Ошибка парсинга вывода pip.[/red]"); return

    # Активный репозиторий
    active = get_active(conn)
    active_name = active["name"] if active else None
    active_url  = active["url"]  if active else None

    # История установок через piphub-repo
    history = conn.execute(
        "SELECT package, repo_name FROM install_log WHERE status='ok'"
    ).fetchall()
    piphub_pkgs = {r["package"]: r["repo_name"] for r in history}

    # Все репозитории пользователя
    repos = [dict(r) for r in conn.execute("SELECT * FROM repos WHERE builtin=0").fetchall()]

    data = {
        "created_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        "piphub_repo_version": VERSION,
        "active_repo": active_name,
        "active_url": active_url,
        "packages": installed,
        "piphub_installed": piphub_pkgs,
        "custom_repos": repos,
    }

    output.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    rprint(f"[green]✓[/green] Сохранено [bold]{len(installed)}[/bold] пакетов → [dim]{output}[/dim]")
    rprint(f"[dim]  Из них через piphub-repo: {len(piphub_pkgs)}[/dim]")
    rprint(f"[dim]  Пользовательских репозиториев: {len(repos)}[/dim]")
    rprint(f"\n[dim]Для восстановления: piphub-repo restore {output}[/dim]")


def cmd_restore(args, conn):
    """Восстанавливает пакеты из freeze-файла (после сброса Termux)."""
    src = Path(args.input)
    if not src.exists():
        rprint(f"[red]Файл не найден: {src}[/red]"); return

    try:
        data = json.loads(src.read_text(encoding="utf-8"))
    except Exception as e:
        rprint(f"[red]Ошибка чтения файла: {e}[/red]"); return

    rprint(f"\n[bold cyan]piphub-repo restore[/bold cyan] ← {src}\n")
    rprint(f"[dim]  Создан: {data.get('created_at', '—')}[/dim]")
    rprint(f"[dim]  Версия piphub-repo: {data.get('piphub_repo_version', '—')}[/dim]\n")

    packages    = data.get("packages", [])
    custom_repos = data.get("custom_repos", [])
    active_name  = data.get("active_repo")
    piphub_pkgs  = data.get("piphub_installed", {})

    # 1. Восстановить пользовательские репозитории
    if custom_repos:
        rprint(f"[bold]Шаг 1/3:[/bold] Восстанавливаю {len(custom_repos)} репозиториев...")
        for r in custom_repos:
            try:
                conn.execute("""
                    INSERT OR REPLACE INTO repos
                      (name, display, url, meta_url, fallback_url, description)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (r["name"], r.get("display", r["name"]), r["url"],
                      r.get("meta_url"), r.get("fallback_url"), r.get("description", "")))
                rprint(f"  [green]✓[/green] {r['name']}")
            except Exception as e:
                rprint(f"  [yellow]⚠ {r['name']}: {e}[/yellow]")
        conn.commit()
    else:
        rprint("[bold]Шаг 1/3:[/bold] Нет пользовательских репозиториев.")

    # 2. Активировать репозиторий
    rprint(f"\n[bold]Шаг 2/3:[/bold] Активирую репозиторий...")
    if active_name:
        row = conn.execute("SELECT * FROM repos WHERE name=?", (active_name,)).fetchone()
        if row:
            apply_repo(row)
            set_active(conn, active_name)
            rprint(f"  [green]✓[/green] Активирован: {active_name}")
        else:
            rprint(f"  [yellow]⚠ Репозиторий '{active_name}' не найден, пропускаю[/yellow]")
    else:
        rprint("  [dim]Нет активного репозитория в freeze-файле[/dim]")

    # 3. Установить пакеты
    rprint(f"\n[bold]Шаг 3/3:[/bold] Устанавливаю {len(packages)} пакетов...")

    # Системные пакеты которые не нужно устанавливать (уже есть в Termux)
    skip = {"pip", "setuptools", "wheel", "pkg-resources"}

    active = get_active(conn)
    install_cmd = [sys.executable, "-m", "pip", "install"]
    if active:
        install_cmd += ["--index-url", active["url"]]
        if active["fallback_url"]:
            install_cmd += ["--extra-index-url", active["fallback_url"]]

    to_install = [
        f"{p['name']}=={p['version']}"
        for p in packages
        if p["name"].lower() not in skip
    ]

    if not args.dry_run:
        if to_install:
            rc = subprocess.call(install_cmd + to_install)
            if rc == 0:
                rprint(f"\n[green]✓[/green] Восстановлено {len(to_install)} пакетов.")
            else:
                rprint(f"\n[yellow]⚠[/yellow] Часть пакетов могла не установиться (код {rc}).")
                rprint("[dim]  Попробуй запустить restore ещё раз или установи вручную.[/dim]")
    else:
        rprint("\n[dim]Dry-run режим — реальная установка пропущена.[/dim]")
        rprint(f"[dim]Будет установлено: {', '.join(to_install[:5])}{'...' if len(to_install)>5 else ''}[/dim]")


def cmd_clean(args, conn):
    """Очистка pip-кеша, git-кеша зеркала и временных файлов."""
    rprint(f"\n[bold cyan]piphub-repo clean[/bold cyan]\n")

    freed_total = 0

    # 1. pip cache
    rprint("[bold]pip cache:[/bold]")
    pip_cache = subprocess.run(
        [sys.executable, "-m", "pip", "cache", "info"],
        capture_output=True, text=True
    )
    if args.all or args.pip:
        rc = subprocess.call([sys.executable, "-m", "pip", "cache", "purge"])
        if rc == 0:
            rprint("  [green]✓[/green] pip cache очищен")
    else:
        # Показываем размер без очистки
        for line in pip_cache.stdout.splitlines():
            if "size" in line.lower() or "location" in line.lower():
                rprint(f"  [dim]{line.strip()}[/dim]")
        rprint("  [dim]Добавь --pip или --all чтобы очистить[/dim]")

    # 2. git objects в папке зеркала
    rprint("\n[bold]git репозиторий зеркала:[/bold]")
    mirror_dir = find_mirror_dir(args.repo_dir)
    if mirror_dir:
        if args.all or args.git:
            rc = subprocess.call(
                ["git", "gc", "--prune=now", "--aggressive"],
                cwd=mirror_dir,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            if rc == 0:
                rprint(f"  [green]✓[/green] git gc выполнен ({mirror_dir})")
        else:
            # Размер .git
            git_dir = mirror_dir / ".git"
            if git_dir.exists():
                size = sum(f.stat().st_size for f in git_dir.rglob("*") if f.is_file())
                rprint(f"  [dim].git размер: {size/1048576:.1f} MB[/dim]")
                rprint("  [dim]Добавь --git или --all чтобы запустить git gc[/dim]")
    else:
        rprint("  [dim]Папка зеркала не найдена, пропускаю[/dim]")

    # 3. История установок в БД (опционально)
    rprint("\n[bold]История установок (БД):[/bold]")
    count = conn.execute("SELECT COUNT(*) FROM install_log").fetchone()[0]
    if args.all or args.history:
        conn.execute("DELETE FROM install_log")
        conn.commit()
        rprint(f"  [green]✓[/green] Очищено {count} записей истории")
    else:
        rprint(f"  [dim]Записей в истории: {count}[/dim]")
        rprint("  [dim]Добавь --history или --all чтобы очистить[/dim]")

    # 4. Без флагов — просто показать что можно почистить
    if not any([args.all, args.pip, args.git, args.history]):
        rprint("\n[bold cyan]Доступные опции:[/bold cyan]")
        rprint("  [bold]--pip[/bold]      очистить pip cache")
        rprint("  [bold]--git[/bold]      запустить git gc в зеркале")
        rprint("  [bold]--history[/bold]  очистить историю установок")
        rprint("  [bold]--all[/bold]      всё сразу")


def cmd_stats(args, conn):
    """Статистика зеркала: количество пакетов, размер, история установок."""
    rprint(f"\n[bold cyan]piphub-repo stats[/bold cyan]\n")

    # История из БД
    total_installs = conn.execute("SELECT COUNT(*) FROM install_log").fetchone()[0]
    ok_installs    = conn.execute("SELECT COUNT(*) FROM install_log WHERE status='ok'").fetchone()[0]
    fail_installs  = conn.execute("SELECT COUNT(*) FROM install_log WHERE status!='ok'").fetchone()[0]

    # Самые популярные пакеты
    top_pkgs = conn.execute("""
        SELECT package, COUNT(*) as cnt
        FROM install_log WHERE status='ok'
        GROUP BY package ORDER BY cnt DESC LIMIT 5
    """).fetchall()

    # Зеркало на диске
    mirror_dir = find_mirror_dir(args.repo_dir)
    pkg_count = 0
    total_size = 0
    if mirror_dir:
        packages_dir = mirror_dir / "packages"
        if packages_dir.exists():
            for f in packages_dir.iterdir():
                if f.is_file() and any(f.name.endswith(e) for e in (".whl", ".tar.gz", ".zip")):
                    pkg_count += 1
                    total_size += f.stat().st_size

    # Репозитории
    repo_count   = conn.execute("SELECT COUNT(*) FROM repos").fetchone()[0]
    custom_count = conn.execute("SELECT COUNT(*) FROM repos WHERE builtin=0").fetchone()[0]
    active       = get_active(conn)

    if HAS_RICH:
        from rich.columns import Columns

        stats_lines = [
            f"[bold]Пакетов в зеркале:[/bold]  {pkg_count}",
            f"[bold]Размер зеркала:[/bold]     {total_size/1048576:.2f} MB",
            f"[dim]{'─'*38}[/dim]",
            f"[bold]Установок всего:[/bold]    {total_installs}",
            f"[bold]Успешных:[/bold]           [green]{ok_installs}[/green]",
            f"[bold]Ошибок:[/bold]             [red]{fail_installs}[/red]",
            f"[dim]{'─'*38}[/dim]",
            f"[bold]Репозиториев:[/bold]       {repo_count} ({custom_count} польз.)",
            f"[bold]Активный:[/bold]           {active['name'] if active else '—'}",
        ]

        console.print(Panel("\n".join(stats_lines), title="Статистика", border_style="cyan"))

        if top_pkgs:
            rprint("\n[bold]Топ пакетов по установкам:[/bold]")
            for i, r in enumerate(top_pkgs, 1):
                rprint(f"  [bold]{i}.[/bold] {r['package']:20} — {r['cnt']} раз")
    else:
        print(f"Пакетов в зеркале: {pkg_count}")
        print(f"Размер зеркала:    {total_size/1048576:.2f} MB")
        print(f"Установок всего:   {total_installs} (успешных: {ok_installs})")
        print(f"Репозиториев:      {repo_count}")
        if top_pkgs:
            print("Топ пакетов:")
            for i, r in enumerate(top_pkgs, 1):
                print(f"  {i}. {r['package']} — {r['cnt']} раз")

def cmd_completion(args, conn):
    print(BASH_COMPLETION)
    rprint("\n[dim]Добавь в ~/.bashrc:[/dim]")
    rprint('[dim]  eval "$(piphub-repo completion)"[/dim]')

# ─── Парсер ───────────────────────────────────────────────────────────────────

def build_parser():
    p = argparse.ArgumentParser(
        prog=APP_NAME,
        description=f"piphub-repo v{VERSION} — pip-репозитории для Termux",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Примеры:
  piphub-repo install requests rich
  piphub-repo push requests click          # скачать + запушить в зеркало
  piphub-repo push requests --with-deps    # с зависимостями
  piphub-repo build myscript.py            # собрать .whl и запушить
  piphub-repo build tool.sh --name mytool  # собрать из shell-скрипта
  piphub-repo update                       # обновить все пакеты в зеркале
  piphub-repo outdated                     # показать устаревшие
  piphub-repo verify                       # проверить целостность зеркала (sha256)
  piphub-repo sync                         # синхронизировать метаданные
  piphub-repo uninstall requests           # удалить из pip
  piphub-repo list
  piphub-repo use tastade
  piphub-repo ping
  piphub-repo search requests
  piphub-repo config --show
  piphub-repo status
  piphub-repo doctor                       # диагностика окружения Termux
  piphub-repo bootstrap                    # полная настройка с нуля (после сброса Termux)
  piphub-repo self-update                  # обновить сам piphub-repo
  piphub-repo freeze                       # сохранить список пакетов
  piphub-repo restore piphub-freeze.json  # восстановить после сброса Termux
  piphub-repo clean --all                  # очистить кеши
  piphub-repo stats                        # статистика зеркала
  piphub-repo history
  eval "$(piphub-repo completion)"         # включить tab completion
""",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    sub = p.add_subparsers(dest="command", metavar="<команда>")

    # install
    i = sub.add_parser("install", aliases=["i"], help="Установить пакет(ы)")
    i.add_argument("packages", nargs="+", metavar="пакет")
    i.add_argument("--repo", "-r", metavar="ИМЯ")
    i.add_argument("--no-fallback", action="store_true")
    i.add_argument("--upgrade", "-U", action="store_true")
    i.add_argument("--no-deps", action="store_true")
    i.add_argument("--quiet", "-q", action="store_true")

    # uninstall
    u2 = sub.add_parser("uninstall", help="Удалить пакет из pip")
    u2.add_argument("packages", nargs="+", metavar="пакет")

    # push
    pu = sub.add_parser("push", help="Скачать пакет(ы) с PyPI и запушить в зеркало")
    pu.add_argument("packages", nargs="+", metavar="пакет")
    pu.add_argument("--dir", dest="repo_dir", metavar="ПУТЬ")
    pu.add_argument("--with-deps", action="store_true", help="Скачать вместе с зависимостями")

    # build
    b = sub.add_parser("build", help="Собрать .whl из .py/.sh/файлов и запушить")
    b.add_argument("files", nargs="+", metavar="файл")
    b.add_argument("--name", "-n", help="Имя пакета")
    b.add_argument("--version", "-v", dest="version", help="Версия (по умолч. 1.0.0)")
    b.add_argument("--author", "-a", help="Автор")
    b.add_argument("--description", "-d", help="Описание")
    b.add_argument("--dir", dest="repo_dir", metavar="ПУТЬ")

    # update
    upd = sub.add_parser("update", help="Обновить все пакеты в зеркале до последних версий")
    upd.add_argument("--dir", dest="repo_dir", metavar="ПУТЬ")

    # outdated
    out = sub.add_parser("outdated", help="Показать устаревшие пакеты в зеркале")
    out.add_argument("--dir", dest="repo_dir", metavar="ПУТЬ")

    # verify
    vf = sub.add_parser("verify", help="Проверить целостность файлов зеркала (sha256, отсутствующие файлы)")
    vf.add_argument("--dir", dest="repo_dir", metavar="ПУТЬ")

    # sync
    sy = sub.add_parser("sync", help="Синхронизировать кеш метаданных зеркала")
    sy.add_argument("--repo", "-r")

    # list
    sub.add_parser("list", aliases=["ls"], help="Список репозиториев")

    # use
    u = sub.add_parser("use", help="Активировать репозиторий")
    u.add_argument("name", nargs="?")
    u.add_argument("--no-fallback", action="store_true")

    # add
    a = sub.add_parser("add", help="Добавить репозиторий")
    a.add_argument("--name"); a.add_argument("--url")
    a.add_argument("--display"); a.add_argument("--fallback")
    a.add_argument("--meta-url"); a.add_argument("--description")

    # remove
    r = sub.add_parser("remove", aliases=["rm"], help="Удалить репозиторий")
    r.add_argument("name"); r.add_argument("--force", action="store_true")

    # edit
    e = sub.add_parser("edit", help="Редактировать репозиторий")
    e.add_argument("name")

    # info
    inf = sub.add_parser("info", help="Подробности о репозитории")
    inf.add_argument("name")

    # ping
    pg = sub.add_parser("ping", help="Проверить доступность")
    pg.add_argument("name", nargs="?")

    # search
    s = sub.add_parser("search", help="Поиск пакета")
    s.add_argument("query"); s.add_argument("--repo", "-r")

    # config
    c = sub.add_parser("config", help="Управление pip.conf")
    c.add_argument("--show", action="store_true")
    c.add_argument("--reset", action="store_true")
    c.add_argument("--set-key", metavar="KEY")
    c.add_argument("--set-value", metavar="VALUE")

    # status
    sub.add_parser("status", help="Текущий статус")

    # doctor
    sub.add_parser("doctor", help="Диагностика окружения Termux")

    # bootstrap
    bs = sub.add_parser("bootstrap", help="Автоматическая настройка окружения с нуля (после сброса Termux)")
    bs.add_argument("--repo-url", help="URL зеркала для клонирования (по умолч. github.com/Tastade/piphub-repo)")
    bs.add_argument("--repo-dir", metavar="ПУТЬ", help="Куда клонировать (по умолч. ~/piphub-repo)")
    bs.add_argument("--use-repo", metavar="ИМЯ", help="Какой репозиторий активировать (по умолч. tastade)")
    bs.add_argument("--git-name", help="git user.name (если не настроен)")
    bs.add_argument("--git-email", help="git user.email (если не настроен)")

    # self-update
    su = sub.add_parser("self-update", help="Обновить сам piphub-repo до последней версии с GitHub Releases")
    su.add_argument("--repo", dest="repo_slug", metavar="владелец/репо",
                    help="GitHub репозиторий с релизами (по умолч. Tastade/piphub-repo)")
    su.add_argument("--yes", "-y", action="store_true", help="Не спрашивать подтверждение")

    # history
    h = sub.add_parser("history", help="История установок")
    h.add_argument("--limit", type=int, default=30)

    # export / import
    ex = sub.add_parser("export", help="Экспорт в JSON")
    ex.add_argument("--output", "-o")
    im = sub.add_parser("import", help="Импорт из JSON")
    im.add_argument("input")

    # freeze
    fz = sub.add_parser("freeze", help="Сохранить список пакетов для восстановления")
    fz.add_argument("--output", "-o", help="Путь к файлу (по умолч. ~/piphub-freeze.json)")

    # restore
    rs = sub.add_parser("restore", help="Восстановить пакеты из freeze-файла")
    rs.add_argument("input", help="Путь к freeze-файлу")
    rs.add_argument("--dry-run", action="store_true", help="Показать что будет установлено, не устанавливая")

    # clean
    cl = sub.add_parser("clean", help="Очистка кешей и временных файлов")
    cl.add_argument("--pip",     action="store_true", help="Очистить pip cache")
    cl.add_argument("--git",     action="store_true", help="Запустить git gc в зеркале")
    cl.add_argument("--history", action="store_true", help="Очистить историю установок")
    cl.add_argument("--all",     action="store_true", help="Всё сразу")
    cl.add_argument("--dir", dest="repo_dir", metavar="ПУТЬ")

    # stats
    st = sub.add_parser("stats", help="Статистика зеркала")
    st.add_argument("--dir", dest="repo_dir", metavar="ПУТЬ")

    # completion
    sub.add_parser("completion", help="Bash tab completion")

    return p

# ─── Точка входа ──────────────────────────────────────────────────────────────

def main():
    if not HAS_RICH:
        print(f"[{APP_NAME}] Совет: pip install rich — для красивого интерфейса")

    parser = build_parser()
    args   = parser.parse_args()
    conn   = db_connect()
    seed_builtins(conn)

    COMMANDS = {
        "install":    cmd_install,    "i":   cmd_install,
        "uninstall":  cmd_uninstall,
        "push":       cmd_push,
        "build":      cmd_build,
        "update":     cmd_update,
        "outdated":   cmd_outdated,
        "verify":     cmd_verify,
        "sync":       cmd_sync,
        "list":       cmd_list,       "ls":  cmd_list,
        "use":        cmd_use,
        "add":        cmd_add,
        "remove":     cmd_remove,     "rm":  cmd_remove,
        "edit":       cmd_edit,
        "info":       cmd_info,
        "ping":       cmd_ping,
        "search":     cmd_search,
        "config":     cmd_config,
        "status":     cmd_status,
        "doctor":     cmd_doctor,
        "bootstrap":  cmd_bootstrap,
        "self-update": cmd_self_update,
        "history":    cmd_history,
        "export":     cmd_export,
        "import":     cmd_import,
        "freeze":     cmd_freeze,
        "restore":    cmd_restore,
        "clean":      cmd_clean,
        "stats":      cmd_stats,
        "completion": cmd_completion,
    }

    if not args.command:
        cmd_status(args, conn)
        rprint("\nИспользуй [bold]piphub-repo --help[/bold] для справки.")
    else:
        fn = COMMANDS.get(args.command)
        if fn:
            fn(args, conn)
        else:
            parser.print_help()

    conn.close()

if __name__ == "__main__":
    main()
