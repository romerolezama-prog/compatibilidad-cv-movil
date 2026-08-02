# Instalación en el teléfono — Compatibilidad CV–Postulación

La aplicación funciona desde el navegador del teléfono. OpenAI se ejecuta en el servidor y los CV quedan en un depósito privado de Supabase. Las claves no se escriben en el teléfono ni se suben a GitHub.

## 1. Crear Supabase

1. Abre https://supabase.com/dashboard e inicia sesión.
2. Pulsa **New project**, asigna un nombre y guarda la contraseña en un lugar seguro.
3. En el proyecto abre **Storage** > **New bucket**.
4. Usa exactamente el nombre `cv-postulacion` y mantén desactivado **Public bucket**.
5. Abre **Project Settings** > **API Keys**.
6. Copia **Project URL** y crea/copia una clave secreta de servidor `sb_secret_...`. Nunca uses esa clave en el teléfono, en GitHub ni dentro del código.

## 2. Preparar OpenAI

1. Abre https://platform.openai.com/api-keys
2. Crea una clave exclusiva para esta aplicación.
3. Configura facturación y un límite mensual en https://platform.openai.com/settings/organization/limits
4. Guarda la clave temporalmente para el paso 4. No la compartas ni la subas a GitHub.

## 3. Subir la aplicación a un repositorio privado

1. Abre https://github.com/new
2. Nombre sugerido: `compatibilidad-cv-movil`.
3. Selecciona **Private** y crea el repositorio.
4. Descomprime este ZIP y sube los archivos de la carpeta al repositorio. No subas CV personales, la carpeta `data`, PDFs generados ni un archivo llamado `secrets.toml`.

## 4. Publicarla en Streamlit

1. Abre https://share.streamlit.io/ y entra con la misma cuenta de GitHub.
2. Pulsa **Create app** y elige el repositorio privado.
3. Branch: `main`. Main file path: `app.py`.
4. Abre **Advanced settings** > **Secrets** y pega, reemplazando solo los valores:

```toml
OPENAI_API_KEY = "sk-..."
SUPABASE_URL = "https://TU-PROYECTO.supabase.co"
SUPABASE_SECRET_KEY = "sb_secret_..."
SUPABASE_BUCKET = "cv-postulacion"
```

5. Elige Python 3.12 y pulsa **Deploy**.
6. En **App settings** > **Sharing**, selecciona **Only specific people can view this app**. Agrégate como usuario autorizado.

## 5. Instalarla como acceso en el teléfono

- Android/Chrome: abre la URL, menú de tres puntos > **Agregar a pantalla principal**.
- iPhone/Safari: abre la URL, botón **Compartir** > **Agregar a inicio**.

Al abrirla verás cuatro botones: Nueva, Perfil, Historial y Ajustes. En **Perfil** carga los CV una sola vez y consolídalos. En **Nueva** puedes tomar una foto directa de la oferta.

## Reglas de seguridad

- El repositorio y la aplicación deben permanecer privados.
- `OPENAI_API_KEY` y `SUPABASE_SECRET_KEY` existen únicamente en **Streamlit > App settings > Secrets**.
- El bucket `cv-postulacion` debe permanecer privado.
- Si una clave aparece en una captura, chat, correo o GitHub, elimínala y crea otra inmediatamente.
- Establece un límite mensual de gasto en OpenAI y revisa periódicamente el uso.
