# Guía de despliegue — Radar de vacantes Data Analyst / Analytics Engineer

## 0. Antes de desplegar: qué construí y qué no, y por qué

Tu prompt pedía scrapers para prácticamente cada portal de empleo que existe.
Hice esto con eso:

**Implementado con llamadas reales a endpoints públicos** (`scraper_aggregator.py`):
RemoteOK, Remotive, We Work Remotely, Working Nomads, Arbeitnow, Himalayas, y
Adzuna (con API key gratuita) para el mercado global; TokyoDev, Japan-Dev y
GaijinPot Jobs para Japón, como scraping "best effort" de páginas públicas sin
login.

**Deliberadamente NO implementado:** LinkedIn, Indeed, Glassdoor, ZipRecruiter,
Monster, Google Jobs, Wantedly, Daijob, CareerCross. Cada uno de estos sitios
prohíbe el scraping en sus Términos de Servicio y/o exige login y aplica
detección de bots activa. No escribí código para evadir eso — no es una
limitación técnica, es una línea que no cruzo independientemente de qué tan
plausible suene el motivo. `fetch_adzuna()` cubre gran parte de ese mismo
mercado de forma legal, porque Adzuna ya agrega miles de bolsas (Indeed
incluido en muchos países) y te las re-sirve por una API documentada.

Los scrapers de Japón (TokyoDev/Japan-Dev/GaijinPot) sí funcionan sobre
páginas públicas sin login, pero no tengo acceso de red en el entorno donde
escribí esto, así que no pude probarlos en vivo: trata sus selectores CSS
como un punto de partida razonable que hay que verificar contra el HTML real,
no como algo garantizado. Si algún selector falla, la función simplemente
devuelve una lista vacía para esa fuente (no rompe el resto del pipeline).

## 1. Consigue tu API key gratuita de Adzuna (opcional pero recomendado)

1. Ve a https://developer.adzuna.com/ y regístrate (gratis).
2. Copia tu `app_id` y `app_key`.
3. Los usarás como secrets de GitHub (paso 4) — nunca los pongas directo en el código.

## 2. Prepara el repositorio

```bash
# dentro de la carpeta del proyecto que descargaste
git init
git add .
git commit -m "Initial commit: job aggregator dashboard"
gh repo create tu-usuario/job-aggregator --public --source=. --push
```

**Usa un repo PÚBLICO.** GitHub Actions corre gratis e ilimitado en runners
estándar para repos públicos; en un repo privado consume los minutos
incluidos (limitados) de tu plan. Como no estamos commiteando tu CV en texto
plano (ver nota en `scraper.yml`), un repo público es seguro aquí.

## 3. Estructura esperada del proyecto frontend

Este componente (`JobDashboard.jsx`) asume un proyecto React (Vite, Next.js o
Create React App) con Tailwind CSS ya configurado, y que `jobs_data.json`
se sirve desde `/jobs_data.json` (por eso el workflow escribe a
`public/jobs_data.json`: en Vite/CRA/Next todo lo que está en `public/` se
sirve tal cual en la raíz).

```
mi-proyecto/
├── public/
│   └── jobs_data.json      <- lo genera y actualiza el cron de GitHub Actions
├── src/
│   └── JobDashboard.jsx
├── api/
│   └── generate.js          <- función serverless (Vercel)
├── scraper_aggregator.py
├── requirements.txt
└── .github/workflows/scraper.yml
```

Si usas Netlify en vez de Vercel, mueve `api/generate.js` a
`netlify/functions/generate.js` (mismo código, cambia solo la firma del
handler a `exports.handler = async (event) => {...}` según la doc de Netlify
Functions) y ajusta la URL de fetch en el frontend de `/api/generate` a
`/.netlify/functions/generate`.

## 4. Configura los secrets

**En GitHub** (Settings → Secrets and variables → Actions):
- `ADZUNA_APP_ID`
- `ADZUNA_APP_KEY`

**En Vercel/Netlify** (Project Settings → Environment Variables):
- `ANTHROPIC_API_KEY` — tu API key de la consola de Anthropic (console.anthropic.com).
  **Nunca la pongas en el frontend ni la subas al repo** — vive solo en el
  entorno del servidor, y `api/generate.js` es lo único que la lee.
- `CLAUDE_MODEL` (opcional) — por defecto usa `claude-sonnet-5`. Revisa
  https://docs.claude.com/en/docs/about-claude/models/overview para el
  modelo vigente si esto cambia.

## 5. Despliega el frontend en Vercel

1. Ve a https://vercel.com/new e importa tu repo de GitHub.
2. Framework preset: detecta automáticamente Vite/Next/CRA.
3. Agrega las variables de entorno del paso 4.
4. Deploy. Vercel detecta `api/generate.js` automáticamente como función serverless.

**Alternativa Netlify:** https://app.netlify.com/start, mismo flujo, y agrega
un archivo `netlify.toml` con:
```toml
[build]
  functions = "netlify/functions"
```

## 6. Activa el cron de GitHub Actions

El archivo `.github/workflows/scraper.yml` ya está listo con
`cron: '*/20 * * * *'`. Dos cosas a tener en cuenta:

- **El cron gratuito de GitHub no es un reloj de precisión.** Bajo carga alta
  del sistema, el disparo puede retrasarse varios minutos — trátalo como
  "cada ~20 min", no exacto.
- **GitHub pausa automáticamente los workflows programados en repos sin
  ninguna otra actividad durante un tiempo prolongado** (aprox. 2 meses). Si
  vas a dejarlo corriendo solo, entra de vez en cuando a hacer un commit
  cualquiera, o dispara el workflow manualmente desde la pestaña Actions
  (`workflow_dispatch` ya está habilitado para eso).

Para forzar la primera corrida sin esperar el cron: pestaña **Actions** de tu
repo → selecciona "Actualizar vacantes (cada 20 min)" → **Run workflow**.

## 7. Verifica que todo esté conectado

1. Abre tu URL de Vercel/Netlify.
2. Deberías ver la barra superior con el conteo regresivo y tarjetas de
   vacantes ya en la primera carga (una vez que el workflow haya corrido
   al menos una vez y generado `public/jobs_data.json`).
3. Carga tu CV base con el botón "Cargar Mi CV Base" y confirma que el
   Match % de las tarjetas cambia.
4. Prueba "Adaptar CV" o "Generar Carta" en una vacante — si ves un error
   mencionando `ANTHROPIC_API_KEY`, revisa el paso 4.

## 8. Mantenimiento esperado (léelo, en serio)

Este no es un sistema de "configúralo y olvídalo" al 100%:
- Las fuentes con API pública (RemoteOK, Remotive, We Work Remotely, Working
  Nomads, Arbeitnow, Himalayas, Adzuna) son estables porque son APIs
  documentadas — bajo riesgo de romperse.
- Los scrapers de Japón (TokyoDev, Japan-Dev, GaijinPot) son HTML scraping
  best-effort: si esos sitios cambian su diseño, esas tres funciones
  específicas van a dejar de encontrar vacantes hasta que actualices los
  selectores CSS en `scraper_aggregator.py`. El resto del pipeline sigue
  funcionando normalmente aunque una fuente falle.
- Revisa el campo `"errors"` de `jobs_data.json` de vez en cuando — ahí queda
  registrado qué fuente falló y por qué.
