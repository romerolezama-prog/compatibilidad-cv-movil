# Compatibilidad CV–Postulación — versión móvil

Aplicación privada para teléfono que consolida varios CV, analiza ofertas y genera un CV adaptado de máximo dos páginas cuando la compatibilidad es suficiente.

## Cómo publicarla

Sigue `GUIA_INSTALACION_MOVIL.md`. Necesitarás cuentas de GitHub, Supabase, Streamlit Community Cloud y OpenAI.

La aplicación está preparada para:

- interfaz compacta y cámara directa del teléfono;
- CV, perfil maestro, historial y PDFs persistentes en un bucket privado de Supabase;
- claves de OpenAI y Supabase guardadas solo como secretos del servidor;
- repositorio y aplicación privados;
- compatibilidad de 80% o más: **Postular**;
- 75% a 79%: **Postular estratégicamente**;
- menos de 75%: **No postular**, sin generar CV.

## CV adaptado

El PDF conserva todas las empresas, usa solo datos comprobables de los CV y adapta el resumen, las funciones y las habilidades a cada oferta. Tiene encabezado centrado, no agrega un título profesional, integra términos ATS naturalmente y no incluye cifras no autorizadas.

## Privacidad

Nunca subas `secrets.toml`, CV personales, la carpeta `data` o PDFs generados a GitHub. El bucket `cv-postulacion` debe permanecer privado y la app debe configurarse para que solo usuarios invitados puedan verla.
