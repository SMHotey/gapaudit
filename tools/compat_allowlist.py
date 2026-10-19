"""Известные и допустимые расхождения legacy vs new — реестр, а не свалка (шаг 1g).

Каждая запись объясняет, ПОЧЕМУ расхождение допустимо; ключ записи совпадает с
полем ``allow`` в diff'е ``tools.compat_check``. Правка allowlist разрешена только
вместе с записью в DEVPLAN §4 (AGENTS.md, gotcha 18): allowlist — часть метода,
а не дежурная правка гейта.

Записи, которых текущие L2-сценарии не задействуют (stdout, пути файлов,
троттлинг), сохранены как документация осознанных отклонений шагов 1f/1g.
"""

ALLOWLIST = {
    "check_predecessors": {
        "--from fix_toml=True": (
            "1f: CLI вызывает names_from_file(fix_toml=True), legacy — нет; "
            "имена из .toml могут отличаться"
        ),
        "summary place": "1f: summary печатается после таблицы, не под строкой",
    },
    "collect": {
        "default cursor": (
            "1g: исправленный metadata.nextCursor против legacy metadata.next_cursor"
        ),
    },
    "catalog_scan": {
        "json out path": "1f: <data_dir>/registry_scan.json vs ../data/...",
    },
    "coverage": {
        "missing files": "1f: отсутствующие входные файлы — пустые, не traceback",
    },
    "verify": {
        "sleep 2.5": "1f: троттлинг вместо sleep; вывод идентичен",
    },
}
