#!/usr/bin/env python3
"""Строит корневой index.html и /simple/index.html"""

import json
from pathlib import Path
from datetime import datetime, timezone

REPO_ROOT = Path(__file__).parent.parent
META_FILE = REPO_ROOT / "mirror_meta.json"
SIMPLE_DIR = REPO_ROOT / "simple"

if not META_FILE.exists():
    print("mirror_meta.json не найден — сначала запусти build_index.py")
    exit(1)

meta = json.loads(META_FILE.read_text(encoding="utf-8"))
packages = meta["packages"]
now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
total_files = sum(len(v) for v in packages.values())

# /simple/index.html
links = "\n".join(
    f'  <a href="{norm}/">{norm}</a><br>'
    for norm in sorted(packages)
)
(SIMPLE_DIR / "index.html").write_text(f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>Simple index</title></head>
<body>
  <h1>Simple index</h1>
{links}
</body>
</html>
""", encoding="utf-8")

# root index.html
pkg_rows = ""
for norm in sorted(packages):
    files = packages[norm]
    size_total = sum(f["size"] for f in files)
    size_mb = size_total / 1_048_576
    pkg_rows += f"""
      <tr>
        <td><a href="simple/{norm}/">{norm}</a></td>
        <td>{len(files)}</td>
        <td>{size_mb:.2f} MB</td>
      </tr>"""

root_html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Tastade PyPI Mirror</title>
  <style>
    :root {{
      --bg: #0d1117; --surface: #161b22; --border: #30363d;
      --accent: #58a6ff; --text: #c9d1d9; --muted: #8b949e; --green: #3fb950;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ background: var(--bg); color: var(--text); font-family: system-ui, sans-serif; padding: 2rem 1rem; }}
    header {{ max-width: 860px; margin: 0 auto 2rem; }}
    h1 {{ font-size: 1.8rem; color: var(--accent); }}
    .subtitle {{ color: var(--muted); margin-top: .4rem; font-size: .95rem; }}
    .stats {{ display: flex; gap: 1.5rem; margin: 1.2rem 0; flex-wrap: wrap; }}
    .stat {{ background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: .7rem 1.2rem; }}
    .stat-num {{ font-size: 1.4rem; font-weight: 700; color: var(--green); }}
    .stat-label {{ font-size: .8rem; color: var(--muted); }}
    .pip-box {{ background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 1rem 1.2rem; margin: 1.2rem 0; }}
    .pip-box h3 {{ font-size: .9rem; color: var(--muted); margin-bottom: .5rem; }}
    code {{ background: #21262d; color: var(--accent); padding: .25rem .5rem; border-radius: 4px; font-family: monospace; font-size: .88rem; display: block; }}
    .search-wrap {{ max-width: 860px; margin: 0 auto 1rem; }}
    #search {{ width: 100%; padding: .6rem 1rem; background: var(--surface); border: 1px solid var(--border); border-radius: 8px; color: var(--text); font-size: 1rem; outline: none; }}
    #search:focus {{ border-color: var(--accent); }}
    table {{ width: 100%; max-width: 860px; margin: 0 auto; border-collapse: collapse; }}
    th {{ text-align: left; padding: .6rem .8rem; color: var(--muted); font-size: .8rem; text-transform: uppercase; border-bottom: 1px solid var(--border); }}
    td {{ padding: .55rem .8rem; border-bottom: 1px solid var(--border); font-size: .9rem; }}
    td a {{ color: var(--accent); text-decoration: none; }}
    td a:hover {{ text-decoration: underline; }}
    tr:hover {{ background: var(--surface); }}
    .footer {{ text-align: center; color: var(--muted); font-size: .8rem; margin-top: 2rem; }}
    .hidden {{ display: none; }}
  </style>
</head>
<body>
  <header>
    <h1>📦 Tastade PyPI Mirror</h1>
    <p class="subtitle">Персональное PyPI-зеркало · Обновлено {now_str}</p>
    <div class="stats">
      <div class="stat"><div class="stat-num">{len(packages)}</div><div class="stat-label">Пакетов</div></div>
      <div class="stat"><div class="stat-num">{total_files}</div><div class="stat-label">Файлов</div></div>
    </div>
    <div class="pip-box">
      <h3>Установка через piphub-repo:</h3>
      <code>pip install piphub-repo &amp;&amp; piphub-repo use tastade &amp;&amp; piphub-repo install ПАКЕТ</code>
    </div>
  </header>
  <div class="search-wrap">
    <input id="search" type="search" placeholder="Поиск пакета..." autocomplete="off">
  </div>
  <table id="pkg-table">
    <thead><tr><th>Пакет</th><th>Файлов</th><th>Размер</th></tr></thead>
    <tbody>{pkg_rows}
    </tbody>
  </table>
  <div class="footer">Сгенерировано автоматически · <a href="simple/">Simple Index</a></div>
  <script>
    const inp = document.getElementById('search');
    const rows = document.querySelectorAll('#pkg-table tbody tr');
    inp.addEventListener('input', () => {{
      const q = inp.value.toLowerCase();
      rows.forEach(r => r.classList.toggle('hidden', !r.cells[0].textContent.includes(q)));
    }});
  </script>
</body>
</html>
"""
(REPO_ROOT / "index.html").write_text(root_html, encoding="utf-8")
print(f"index.html записан ({len(packages)} пакетов, {now_str}).")
