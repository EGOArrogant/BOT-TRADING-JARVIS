"""Revisión de la copia del bot: ¿están todos sus archivos y son de la misma versión?

    python -m bot.integridad

Una copia mezclada (un ZIP nuevo descomprimido encima de uno viejo, una descarga cortada)
hace que el bot se caiga al arrancar, y el reinicio automático lo repetiría para siempre.
Esto lo detecta antes: importa cada módulo del bot y comprueba los archivos que no son código.
Sale con código 3 si la copia está dañada (iniciar_demo.bat entonces se detiene y lo explica).
"""

from __future__ import annotations

import importlib
import pkgutil
import sys
from pathlib import Path

import bot

EXIT_BROKEN = 3
ROOT = Path(bot.__file__).resolve().parent
DATA_FILES = ("config.yaml", "macro_events.yaml", "strategy_overrides.yaml", "dashboard/page.html",
              "dashboard/vendor/lightweight-charts.js")
# Lo que el arranque necesita sí o sí: si falta, el ZIP es de otra versión
REQUIRED_NAMES = {
    "bot.engine": ("run_live_like", "run_record", "Engine"),
    "bot.main": ("main", "run"),
    "bot.dashboard.server": ("serve",),
}


def check() -> list[str]:
    problems: list[str] = []
    for name in DATA_FILES:
        if not (ROOT / name).is_file():
            problems.append(f"falta el archivo bot/{name}")
    for mod in pkgutil.walk_packages([str(ROOT)], prefix="bot."):
        if mod.name.startswith("bot.tests") or mod.name in ("bot.main", "bot.revisar"):
            continue
        try:
            importlib.import_module(mod.name)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{mod.name}: {exc.__class__.__name__}: {exc}")
    for mod_name, names in REQUIRED_NAMES.items():
        try:
            m = importlib.import_module(mod_name)
        except Exception:  # noqa: BLE001
            continue  # ya reportado arriba
        missing = [n for n in names if not hasattr(m, n)]
        if missing:
            problems.append(f"{mod_name} es de otra versión (le falta {', '.join(missing)})")
    return problems


def main() -> int:
    problems = check()
    if not problems:
        print(" [OK] La copia del bot está completa.")
        return 0
    print()
    print(" [X] La copia del bot en esta carpeta está INCOMPLETA o mezclada con una versión vieja.")
    print("     El bot no arranca así; reiniciarlo no sirve. Problemas encontrados:")
    for p in problems[:10]:
        print("       -", p)
    print()
    print("     Cómo arreglarlo:")
    print("       1. Cierra esta ventana y cambia el nombre de esta carpeta (por ejemplo a BOT-VIEJO).")
    print("       2. Descarga el ZIP de nuevo y extráelo en una carpeta NUEVA con el nombre de antes.")
    print("       3. Copia de la carpeta vieja a la nueva: el archivo .env y las carpetas data y reports.")
    print("       4. Doble clic en instalar.bat y luego en iniciar_demo.bat.")
    return EXIT_BROKEN


if __name__ == "__main__":
    sys.exit(main())
