from pathlib import Path
from getpass import getpass

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"

print()
print("==============================================")
print(" LOS GOTISH - CONFIGURAR RIOT API KEY")
print("==============================================")
print()
print("Pega tu Riot API Key cuando se solicite.")
print("Por seguridad, mientras la pegas NO se vera en pantalla.")
print()

api_key = getpass("RIOT_API_KEY: ").strip()

if not api_key:
    print("\nNo se ingreso ninguna clave. No se realizaron cambios.")
    raise SystemExit(1)

if not api_key.startswith("RGAPI-"):
    print("\nAVISO: la clave no comienza con 'RGAPI-'.")
    confirm = input("Guardar de todas maneras? [s/N]: ").strip().lower()
    if confirm not in {"s", "si", "sí", "y", "yes"}:
        print("Operacion cancelada.")
        raise SystemExit(1)

content = f"""RIOT_API_KEY={api_key}
RIOT_PLATFORM=la1
RIOT_REGION=americas
CHALLENGE_YEAR=2026
CHALLENGE_MONTH=8
CHALLENGE_TIMEZONE=America/Guayaquil
CACHE_SECONDS=300
"""

ENV_FILE.write_text(content, encoding="utf-8")
print()
print("API key guardada correctamente en .env")
print("Ese archivo esta ignorado por Git y no debe compartirse.")
print()
input("Presiona ENTER para cerrar...")
