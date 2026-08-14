# Los Gotish — SoloQ Challenge

Versión final preparada para **Windows + VS Code + GitHub + Render**, sin Docker.

## Qué datos están escritos manualmente

Solo los Riot ID de los cinco participantes, en `players.py`:

- GTS NATSUKI#7656
- KaiserImpactX#Red
- GTS GABO11#GTS
- GTS BLAZER#GTS
- CarpaIndigena#LAN

Todos juegan en LAN.

## Qué obtiene automáticamente desde Riot

- Riot ID validado y PUUID.
- Icono y nivel del jugador.
- Rango actual Ranked Solo/Duo.
- LP actuales.
- Partidas de Ranked Solo/Duo (cola 420) jugadas durante agosto de 2026.
- Victorias y derrotas del reto.
- Win rate del reto.
- Top 3 campeones más jugados en agosto.
- Campeón con más victorias en agosto.
- Detección de partida activa para cada participante.
- Análisis de los diez jugadores de la partida: campeón, hechizos, runas, rango, LP y récord de temporada.
- Forma SoloQ reciente: KDA, CS/min, participación en asesinatos, visión, rol frecuente y etiquetas estadísticas.

## Actualización y caché

La página permite datos nuevos **como máximo cada 5 minutos** (`CACHE_SECONDS=300`).

Esto evita que varios amigos abriendo la web al mismo tiempo disparen consultas repetidas a Riot. El backend usa un bloqueo de actualización, caché en memoria y, mientras el proceso está activo, caché local de partidas.

El botón **Actualizar** no puede saltarse esos 5 minutos. La página también programa automáticamente la siguiente actualización cuando corresponde.

> En Render Free el disco es efímero. Por eso el proyecto no depende del caché local para funcionar: si Render reinicia o duerme la instancia, puede reconstruir los datos desde Riot. Mientras la instancia siga viva, el caché reduce mucho las llamadas repetidas.

## Seguridad

La API key **no está incluida en este repositorio**.

No subas nunca un archivo `.env` con una clave real. `.gitignore` ya protege:

- `.env`
- `.venv/`
- `cache/`
- `__pycache__/`
- respaldos locales

La API key solamente debe existir:

- en tu `.env` local; y
- en la variable `RIOT_API_KEY` del panel de Render.

## Ejecutar localmente en Windows

### Primera vez

1. Ejecuta `1_INSTALAR.bat`.
2. Ejecuta `2_CONFIGURAR_API.bat` y pega tu Riot API key.
3. Ejecuta `3_INICIAR_WEB.bat`.
4. Abre `http://127.0.0.1:8000`.

No abras el proyecto únicamente con **Live Server**: el puerto `5500` sólo
sirve los archivos HTML/CSS/JS y no contiene `/api/ranking`. Si quieres usar
Live Server para editar el frontend, deja también `3_INICIAR_WEB.bat` ejecutándose;
la web enviará automáticamente las consultas de Riot a `http://127.0.0.1:8000`.

También puedes usar la terminal de VS Code:

```powershell
.\.venv\Scripts\Activate.ps1
python app.py
```

### Cuando caduque la Development API Key

Ejecuta:

```powershell
.\ACTUALIZAR_API_KEY.bat
```

Pega la nueva clave y reinicia el servidor local.

## Archivos que sí se suben a GitHub

Sube el contenido de esta carpeta. En especial:

- `app.py`
- `players.py`
- `requirements.txt`
- `.gitignore`
- `.env.example`
- `.python-version`
- `render.yaml`
- `static/`
- los `.bat` si quieres conservar la ejecución sencilla en Windows
- `README.md`

No subas `.env`, `.venv` ni `cache`.

## Publicar en GitHub

Desde esta carpeta:

```powershell
git init
git add .
git status
```

Antes del commit verifica que **`.env` no aparezca**.

Luego:

```powershell
git commit -m "Los Gotish SoloQ Challenge"
git branch -M main
git remote add origin https://github.com/TU_USUARIO/los-gotish-soloq-challenge.git
git push -u origin main
```

## Publicar en Render sin Docker

El repositorio ya incluye `render.yaml`, así que puedes usar un **Blueprint** en Render.

1. Conecta tu cuenta de GitHub a Render.
2. Crea un Blueprint y selecciona este repositorio.
3. Render detectará `render.yaml`.
4. Cuando solicite `RIOT_API_KEY`, pega la clave actual de Riot.
5. Aplica el Blueprint y espera el deploy.

La configuración incluida utiliza:

- runtime Python;
- Python 3.11.9 mediante `.python-version`;
- `pip install -r requirements.txt`;
- `uvicorn app:app --host 0.0.0.0 --port $PORT`;
- `/health` como health check;
- plan Free;
- actualización máxima cada 300 segundos.

## Renovar la API key en Render

La Development API Key de Riot caduca periódicamente. Cuando la renueves:

1. Abre tu servicio en Render.
2. Entra a **Environment**.
3. Edita `RIOT_API_KEY`.
4. Pega la nueva clave.
5. Guarda los cambios y deja que Render redepliegue/reinicie el servicio con la variable nueva.

No necesitas modificar GitHub para cambiar la API key.

## Endpoints

- `/` — página web.
- `/api/ranking` — ranking y estadísticas.
- `/api/live/{puuid}` — partida activa enriquecida y análisis de ambos equipos.
- `/api/player/{puuid}/details` — resumen, evolución de LP e historial detallado.
- `/health` — comprobación del servicio sin consumir Riot API.

## Nota sobre Render Free

Si el servicio pasa tiempo sin tráfico, Render puede dormir la instancia. La primera visita después de ese periodo puede tardar más mientras el servicio vuelve a arrancar y reconstruye el caché necesario.
