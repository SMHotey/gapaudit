"""Собирает один markdown-файл с полным кодом всех файлов каталога (по умолчанию cli/).

Зачем: удобно читать и анализировать код одним документом — со сводкой, порядком файлов,
номерами строк-ориентирами и контрольными суммами.

Запуск:
  python tools/dump_code.py core --out docs/CORE-CODE.md   # core/ -> docs/CORE-CODE.md
  python tools/dump_code.py cli                            # cli/  -> CLI-CODE.md

Внимание: вызов без аргументов пересоберёт `CLI-CODE.md` — этот документ удалён 18.10.2026
как дубль кода (сам код живёт в `legacy/`, cli/ и в git). Не создавайте его заново без нужды.
"""
import argparse
import hashlib
import os
import sys
from datetime import datetime

LANG_BY_EXT = {".py": "python", ".js": "javascript", ".ts": "typescript", ".html": "html",
               ".css": "css", ".json": "json", ".sql": "sql", ".md": "markdown",
               ".toml": "toml", ".bat": "bat", ".sh": "bash", ".txt": "text"}


def sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def first_docline(text):
    """Первая содержательная строка докстринга — годится как краткое описание."""
    lines = text.splitlines()
    started = False
    for ln in lines[:40]:
        s = ln.strip()
        if not started and (s.startswith('"""') or s.startswith("'''")):
            started = True
            s = s.strip('"\'')
            if s:
                return s
            continue
        if started:
            if s:
                return s
            continue
    for ln in lines[:5]:
        if ln.strip().startswith("#"):
            return ln.strip().lstrip("#").strip()
    return ""


def collect(root):
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in (".venv", "__pycache__", ".git", "raw")]
        for fn in sorted(filenames):
            if fn.startswith("."):
                continue
            p = os.path.join(dirpath, fn)
            rel = os.path.relpath(p, root).replace("\\", "/")
            try:
                text = open(p, encoding="utf-8").read()
            except Exception as e:
                print(f"  пропускаю {rel}: {e}")
                continue
            files.append({"rel": rel, "text": text,
                          "lines": text.count("\n") + 1,
                          "bytes": len(text.encode("utf-8")),
                          "sum": sha256(text),
                          "doc": first_docline(text)})
    files.sort(key=lambda f: f["rel"])
    return files


def fence_for(text, base="`", want=3):
    """Ограждение длиннее любого ряда бэктиков в коде — иначе markdown сломается."""
    longest = 0
    cur = 0
    for ch in text:
        cur = cur + 1 if ch == base else 0
        longest = max(longest, cur)
    return base * max(want, longest + 1)


def main():
    ap = argparse.ArgumentParser(description="Собрать код каталога в один markdown")
    ap.add_argument("root", nargs="?", default="cli", help="каталог с кодом (по умолчанию cli)")
    ap.add_argument("--out", default=None, help="куда писать (по умолчанию <ROOT>-CODE.md)")
    ap.add_argument("--title", default=None)
    a = ap.parse_args()

    if not os.path.isdir(a.root):
        print(f"нет каталога {a.root}")
        return 1
    files = collect(a.root)
    if not files:
        print("нечего собирать")
        return 1

    name = os.path.basename(os.path.abspath(a.root))
    out = a.out or f"{name.upper()}-CODE.md"
    title = a.title or f"Полный код: {name}/"

    total_lines = sum(f["lines"] for f in files)
    total_bytes = sum(f["bytes"] for f in files)

    parts = [f"# {title}", "",
             f"Собрано: {datetime.now():%Y-%m-%d %H:%M} · файлов: {len(files)} · "
             f"строк: {total_lines} · размер: {total_bytes / 1024:.1f} КБ", "",
             "Документ сгенерирован (`tools/dump_code.py`) — правки вносить в исходные файлы, "
             "а не сюда. Контрольная сумма (sha256, 12 знаков) дана, чтобы можно было сверить "
             "версии при следующей сборке.", "",
             "## Сводка", "",
             "| Файл | Строк | Байт | sha256 | Назначение |", "|---|---:|---:|---|---|"]
    for f in files:
        parts.append(f"| `{f['rel']}` | {f['lines']} | {f['bytes']} | `{f['sum']}` | {f['doc']} |")
    parts += ["", "## Оглавление", ""]
    for i, f in enumerate(files, 1):
        parts.append(f"{i}. `{f['rel']}` — {f['doc'] or 'без описания'} ({f['lines']} строк)")
    parts.append("")

    for f in files:
        ext = os.path.splitext(f["rel"])[1].lower()
        lang = LANG_BY_EXT.get(ext, "")
        fence = fence_for(f["text"]) if ext not in (".md",) else "`" * 4
        parts += [f"## `{f['rel']}`", "",
                  f"{f['lines']} строк · {f['bytes']} байт · sha256 `{f['sum']}`", "",
                  fence + lang, f["text"].rstrip("\n"), fence, ""]

    open(out, "w", encoding="utf-8").write("\n".join(parts))
    print(f"СОХРАНЕНО: {out}")
    print(f"файлов {len(files)}, строк {total_lines}, размер {total_bytes / 1024:.1f} КБ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
