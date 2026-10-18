"""Проверка CLI-CODE.md: каждый исходный файл должен присутствовать в документе дословно."""
import os

ROOT = "cli"
DOC = "CLI-CODE.md"
doc = open(DOC, encoding="utf-8").read()

ok = bad = 0
for dirpath, dirnames, filenames in os.walk(ROOT):
    dirnames[:] = [d for d in dirnames if d != "__pycache__"]
    for fn in sorted(filenames):
        p = os.path.join(dirpath, fn)
        src = open(p, encoding="utf-8").read().rstrip("\n")
        present = src in doc
        print(f"{'OK  ' if present else 'НЕТ '} {p:<40} {len(src.splitlines()):>5} строк")
        ok += present
        bad += not present

print(f"\nдословно перенесено: {ok}, отсутствует: {bad}")
print(f"размер документа: {len(doc) / 1024:.1f} КБ")

# дополнительно: не сломались ли ограждения блоков кода
fences = sum(1 for line in doc.splitlines() if line.startswith("```"))
print(f"строк-ограждений ```: {fences} (должно быть чётное: {fences % 2 == 0})")
