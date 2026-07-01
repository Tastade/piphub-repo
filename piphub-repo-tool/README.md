# piphub-repo

CLI для управления pip-репозиториями в **Termux**.

## Установка

```bash
pip install piphub-repo
```

Или с GitHub:
```bash
pip install git+https://github.com/Tastade/piphub-repo.git
```

## Использование

```bash
# Установить пакет через активный репозиторий
piphub-repo install requests rich

# Список репозиториев
piphub-repo list

# Активировать репозиторий (обновляет pip.conf)
piphub-repo use tastade

# Добавить свой репозиторий
piphub-repo add --name mymirror --url https://example.com/simple/

# Проверить доступность
piphub-repo ping

# Показать pip.conf
piphub-repo config --show

# Текущий статус
piphub-repo status
```

## Все команды

| Команда | Описание |
|---|---|
| `install <пакет>` | Установить через pip + репозиторий |
| `list` | Список всех репозиториев |
| `use [имя]` | Активировать репозиторий |
| `add` | Добавить репозиторий |
| `remove <имя>` | Удалить репозиторий |
| `edit <имя>` | Редактировать репозиторий |
| `info <имя>` | Подробности |
| `ping` | Проверить доступность |
| `search <запрос>` | Поиск пакета |
| `config --show` | Показать pip.conf |
| `config --reset` | Сбросить index-url |
| `config --set KEY VAL` | Установить параметр |
| `status` | Текущий статус |
| `history` | История установок |
| `export` | Экспорт в JSON |
| `import <файл>` | Импорт из JSON |

## Встроенные репозитории

- `tastade` — Личный репозиторий Tastade (GitHub Pages, aarch64)
- `pypi` — Официальный PyPI
- `tuna` — Зеркало Университета Цинхуа
