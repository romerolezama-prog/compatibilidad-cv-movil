from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import streamlit as st
from docx import Document
from openai import OpenAI
from PIL import Image
from pypdf import PdfReader
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

try:
    from supabase import create_client
except ImportError:
    create_client = None

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
UPLOADS = DATA / "uploads"
GENERATED = ROOT / "generated"
DB = DATA / "compatibilidad.db"
for folder in (DATA, UPLOADS, GENERATED):
    folder.mkdir(parents=True, exist_ok=True)

st.set_page_config(page_title="Compatibilidad CV–Postulación", page_icon="🎯", layout="centered", initial_sidebar_state="collapsed")
st.markdown("""
<style>
.block-container{max-width:760px;padding-top:1rem;padding-left:1rem;padding-right:1rem}.hero{padding:1rem 1.1rem;border-radius:18px;background:linear-gradient(120deg,#12263a,#255c74);color:white;margin-bottom:.75rem}
.hero h1{font-size:1.8rem;margin:0}.hero p{opacity:.85;margin:.35rem 0 0}.card{padding:1rem 1.1rem;border:1px solid #dfe7ec;border-radius:16px;background:#fff;margin:.4rem 0}.muted{color:#64748b}.good{color:#087f5b}.warn{color:#b26a00}.bad{color:#b42318}
div.stButton>button,div.stDownloadButton>button{min-height:48px;width:100%;font-weight:600}div[data-testid="stFileUploader"] section{min-height:92px}div[role="radiogroup"]{gap:.25rem}div[role="radiogroup"] label{padding:.45rem .2rem}
@media(max-width:640px){.hero h1{font-size:1.35rem}.hero p{font-size:.88rem}.block-container{padding-top:.55rem}.stMetric{padding:.15rem}.card{padding:.75rem}.stColumns{gap:.35rem}}
</style>""", unsafe_allow_html=True)


def server_secret(name: str, default: str = "") -> str:
    value = os.getenv(name, "")
    if value:
        return value.strip()
    try:
        return str(st.secrets.get(name, default)).strip()
    except Exception:
        return default


def require_private_access() -> None:
    expected = server_secret("APP_PASSWORD")
    if not expected:
        st.error("La publicación está bloqueada hasta configurar APP_PASSWORD en los secretos del servidor.")
        st.stop()
    if st.session_state.get("private_access"):
        return
    st.markdown("<div class='hero'><h1>Compatibilidad CV–Postulación</h1><p>Acceso privado</p></div>", unsafe_allow_html=True)
    with st.form("private_login", clear_on_submit=True):
        password = st.text_input("Contraseña de acceso", type="password")
        submitted = st.form_submit_button("Entrar", type="primary")
    if submitted:
        if hmac.compare_digest(password, expected):
            st.session_state.private_access = True
            st.rerun()
        else:
            st.error("Contraseña incorrecta.")
    st.stop()


require_private_access()


@st.cache_resource
def cloud_client():
    url = server_secret("SUPABASE_URL")
    key = server_secret("SUPABASE_SECRET_KEY") or server_secret("SUPABASE_SERVICE_ROLE_KEY")
    if not (url and key and create_client):
        return None
    return create_client(url, key)


def cloud_enabled() -> bool:
    return cloud_client() is not None


def cloud_download(remote: str, local: Path) -> bool:
    client = cloud_client()
    if not client:
        return False
    try:
        raw = client.storage.from_(server_secret("SUPABASE_BUCKET", "cv-postulacion")).download(remote)
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(raw)
        return True
    except Exception:
        return False


def cloud_upload(local: Path, remote: str, content_type: str = "application/octet-stream") -> None:
    client = cloud_client()
    if not client or not local.exists():
        return
    bucket = client.storage.from_(server_secret("SUPABASE_BUCKET", "cv-postulacion"))
    payload = local.read_bytes()
    options = {"content-type": content_type, "upsert": "true"}
    try:
        bucket.upload(path=remote, file=payload, file_options=options)
    except Exception:
        bucket.update(path=remote, file=payload, file_options={"content-type": content_type})


def cloud_backup_db() -> None:
    cloud_upload(DB, "state/compatibilidad.db", "application/x-sqlite3")


def cloud_document_path(path: Path) -> str:
    """Crea una ruta ASCII estable; Storage no admite tildes en object keys."""
    match = re.match(r"^([0-9a-f]{12})_", path.name, re.I)
    token = match.group(1).lower() if match else hashlib.sha256(path.name.encode("utf-8")).hexdigest()[:12]
    suffix = path.suffix.lower() if re.fullmatch(r"\.[a-z0-9]+", path.suffix.lower()) else ".bin"
    return f"uploads/{token}{suffix}"


if not DB.exists():
    cloud_download("state/compatibilidad.db", DB)


def conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def init_db() -> None:
    with conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS cv_documents(id INTEGER PRIMARY KEY, name TEXT, sha256 TEXT UNIQUE, path TEXT, extracted_text TEXT, added_at TEXT);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS applications(id INTEGER PRIMARY KEY, created_at TEXT, company TEXT, role TEXT, location TEXT, score INTEGER, recommendation TEXT, offer_text TEXT, analysis_json TEXT, status TEXT DEFAULT 'Por revisar', notes TEXT DEFAULT '', cv_path TEXT);
        CREATE TABLE IF NOT EXISTS answers(id INTEGER PRIMARY KEY, application_id INTEGER, question TEXT, answer TEXT, created_at TEXT);
        """)


def setting(key: str, default: str = "") -> str:
    with conn() as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def save_setting(key: str, value: str) -> None:
    with conn() as c:
        c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    cloud_backup_db()


def restore_cloud_files() -> None:
    if not cloud_enabled():
        return
    with conn() as c:
        docs = c.execute("SELECT id,path FROM cv_documents").fetchall()
        apps = c.execute("SELECT id,cv_path FROM applications WHERE cv_path IS NOT NULL AND cv_path<>''").fetchall()
    for row in docs:
        local = UPLOADS / Path(row["path"]).name
        if not local.exists(): cloud_download(cloud_document_path(local), local)
        with conn() as c: c.execute("UPDATE cv_documents SET path=? WHERE id=?", (str(local), row["id"]))
    for row in apps:
        local = GENERATED / Path(row["cv_path"]).name
        if not local.exists(): cloud_download(f"generated/{local.name}", local)
        with conn() as c: c.execute("UPDATE applications SET cv_path=? WHERE id=?", (str(local), row["id"]))


def recover_uploaded_documents() -> int:
    """Reindexa archivos persistentes si una actualización reemplazó solo la base SQLite."""
    recovered = 0
    for path in UPLOADS.iterdir():
        if not path.is_file() or path.suffix.lower() not in (".pdf", ".docx", ".txt"):
            continue
        raw = path.read_bytes()
        sha = hashlib.sha256(raw).hexdigest()
        text = file_text(path.name, raw)
        if not text.strip():
            continue
        display_name = re.sub(r"^[0-9a-f]{12}_", "", path.name, flags=re.I)
        with conn() as c:
            try:
                c.execute("INSERT INTO cv_documents(name,sha256,path,extracted_text,added_at) VALUES(?,?,?,?,?)", (display_name, sha, str(path), text, datetime.now().isoformat(timespec="seconds")))
                recovered += 1
            except sqlite3.IntegrityError:
                pass
    return recovered


def clean_json(text: str) -> dict[str, Any]:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I | re.S)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("La respuesta no contiene JSON válido.")
    return json.loads(text[start:end + 1])


def file_text(name: str, data: bytes) -> str:
    ext = Path(name).suffix.lower()
    if ext == ".pdf":
        return "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(data)).pages).strip()
    if ext == ".docx":
        doc = Document(io.BytesIO(data))
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
    if ext == ".txt":
        return data.decode("utf-8", errors="replace")
    return ""


def api_client() -> OpenAI | None:
    key = server_secret("OPENAI_API_KEY") or st.session_state.get("api_key", "").strip()
    return OpenAI(api_key=key) if key else None


def ask_json(prompt: str, images: list[tuple[str, bytes]] | None = None) -> dict[str, Any]:
    client = api_client()
    if not client:
        raise RuntimeError("Ingresa tu OpenAI API key en Configuración.")
    content: list[dict[str, Any]] = [{"type": "input_text", "text": prompt}]
    for mime, raw in images or []:
        encoded = base64.b64encode(raw).decode("ascii")
        content.append({"type": "input_image", "image_url": f"data:{mime};base64,{encoded}"})
    response = client.responses.create(
        model=setting("model", "gpt-5.6-sol"),
        input=[{"role": "user", "content": content}],
        store=False,
    )
    return clean_json(response.output_text)


def master_text() -> str:
    with conn() as c:
        rows = c.execute("SELECT name, extracted_text FROM cv_documents ORDER BY id").fetchall()
    return "\n\n".join(f"### {r['name']}\n{r['extracted_text']}" for r in rows)


def profile() -> dict[str, Any]:
    raw = setting("master_profile", "{}")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def personal_data() -> dict[str, str]:
    try:
        stored = json.loads(setting("personal_data", "{}"))
    except json.JSONDecodeError:
        stored = {}
    p = profile()
    contact = p.get("contact", {})
    return {
        "name": str(stored.get("name") or p.get("name") or ""),
        "location": str(stored.get("location") or contact.get("location") or ""),
        "phone": str(stored.get("phone") or contact.get("phone") or ""),
        "email": str(stored.get("email") or contact.get("email") or ""),
        "linkedin": str(stored.get("linkedin") or contact.get("linkedin") or ""),
    }


def profile_issues(p: dict[str, Any] | None = None) -> list[str]:
    p = p or profile()
    personal = personal_data()
    issues = []
    if not personal.get("name"): issues.append("nombre")
    if not personal.get("phone"): issues.append("teléfono")
    if not personal.get("email"): issues.append("correo")
    if not str(p.get("summary", "")).strip(): issues.append("resumen profesional")
    if not p.get("experience"): issues.append("experiencia laboral")
    return issues


PROFILE_SCHEMA = """Devuelve exclusivamente JSON válido con esta forma:
{"name":"","headline":"","contact":{"email":"","phone":"","linkedin":"","location":""},"summary":"","experience":[{"role":"","company":"","dates":"","achievements":[""]}],"education":[""],"skills":[""],"tools":[""],"languages":[""]}
No inventes, no completes vacíos por intuición y elimina duplicados."""


ANALYSIS_SCHEMA = """Devuelve exclusivamente JSON válido:
{"company":"","role":"","location":"","modality":"","summary":"","requirements":[""],"score":0,"match_explanation":"","strengths":[{"area":"","evidence":""}],"partial_matches":[{"area":"","detail":""}],"weaknesses":[{"area":"","impact":""}],"ats_keywords":[""],"cv_focus":[""],"suggested_questions":[""]}
El score debe ser entero 0-100 y basarse solo en evidencia real. Pondera requisitos obligatorios, experiencia, herramientas, formación e idioma. No infieras experiencia ausente."""


CV_SCHEMA = """Devuelve exclusivamente JSON válido con esta forma:
{"summary":"","experience":[{"company":"","location":"","role":"","dates":"","bullets":[""]}],"education":[{"credential":"","institution":"","dates":""}],"skills":[""],"tools":[""],"languages":[""]}
Reglas obligatorias:
- El CV busca ser visible para ATS y convincente para selección humana, sin inventar ni exagerar.
- Adapta el resumen, las funciones, habilidades y herramientas al cargo y vocabulario de la oferta.
- Usa exclusivamente hechos comprobables presentes en los CV base o el perfil maestro.
- Conserva TODAS las empresas, cargos y fechas, en el mismo orden cronológico del perfil.
- Resumen de 75 a 105 palabras. No incluyas título profesional separado ni instrucciones internas.
- Máximo 3 viñetas por empresa, cada una breve, concreta y relevante para la oferta.
- Máximo 8 habilidades y 7 herramientas; incluye solo las relevantes y con nivel real cuando exista.
- No incluyas porcentajes 20% o 15%, ni afirmaciones de 12 obras/proyectos.
- No incluyas una sección de palabras clave: intégralas naturalmente en el contenido.
- No uses frases como 'Enfoque para esta postulación', recomendaciones o notas para el candidato."""


def rebuild_profile() -> None:
    docs = master_text()
    if not docs.strip():
        raise RuntimeError("Primero carga al menos un CV.")
    result = ask_json("Consolida estos CV en un perfil maestro fiel. " + PROFILE_SCHEMA + "\n\nCV:\n" + docs[:120000])
    saved = personal_data()
    result["name"] = saved.get("name") or result.get("name", "")
    result.setdefault("contact", {})
    for key in ("email", "phone", "linkedin", "location"):
        if saved.get(key): result["contact"][key] = saved[key]
    save_setting("master_profile", json.dumps(result, ensure_ascii=False))
    save_setting("profile_updated", datetime.now().isoformat(timespec="seconds"))


def recommendation(score: int) -> str:
    if score >= 80:
        return "POSTULAR"
    if score >= 75:
        return "POSTULAR ESTRATÉGICAMENTE"
    return "NO POSTULAR"


def demo_analysis(text: str) -> dict[str, Any]:
    score_match = re.search(r"(?:score|compatibilidad)\s*[:=]?\s*(\d{1,3})", text, re.I)
    score = min(100, int(score_match.group(1))) if score_match else 82
    return {"_demo":True,"company":"Empresa de demostración","role":"Programador/a de Obras","location":"Chile","modality":"Presencial","summary":"Planificación y control del programa de obra, hitos, recursos y reportabilidad.","requirements":["Experiencia en planificación de obras","Manejo de cronogramas y reportes","Coordinación con equipos de terreno"],"score":score,"match_explanation":"Tu experiencia en planificación, seguimiento de avances y coordinación se relaciona con las funciones centrales del cargo.","strengths":[{"area":"Planificación y programación","evidence":"Alta"},{"area":"Control de avances","evidence":"Alta"}],"partial_matches":[],"weaknesses":[{"area":"Experiencia en el proyecto específico","impact":"Por confirmar"}],"ats_keywords":["cronograma","hitos","avance físico","planificación"],"cv_focus":["Priorizar planificación y control"],"suggested_questions":["Comente su experiencia en el cargo.","¿Cuenta con experiencia en proyectos similares?","Indique sus pretensiones de renta."]}


def analyze_offer(text: str, images: list[tuple[str, bytes]]) -> dict[str, Any]:
    p = profile()
    prompt = f"""Analiza la oferta laboral y compárala contra el perfil maestro. Extrae empresa, cargo, ubicación, modalidad y requisitos desde la oferta. {ANALYSIS_SCHEMA}

PERFIL MAESTRO:
{json.dumps(p, ensure_ascii=False)[:90000]}

TEXTO DE OFERTA:
{text[:60000]}
"""
    return ask_json(prompt, images)


def save_application(a: dict[str, Any], offer_text: str) -> int:
    score = max(0, min(100, int(a.get("score", 0))))
    rec = recommendation(score)
    with conn() as c:
        cur = c.execute("""INSERT INTO applications(created_at,company,role,location,score,recommendation,offer_text,analysis_json)
        VALUES(?,?,?,?,?,?,?,?)""", (datetime.now().isoformat(timespec="seconds"), a.get("company", ""), a.get("role", ""), a.get("location", ""), score, rec, offer_text, json.dumps(a, ensure_ascii=False)))
        app_id = int(cur.lastrowid)
    cloud_backup_db()
    return app_id


def safe(value: Any) -> str:
    return str(value or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _forbidden_claim(value: str) -> bool:
    return bool(re.search(r"(?:20\s*%|15\s*%|\b12\s+(?:obras|proyectos))", value, re.I))


def _words(value: str, limit: int) -> str:
    parts = str(value or "").split()
    return " ".join(parts[:limit]).rstrip(" ,;:-") + ("." if parts else "")


def _demo_cv_content(p: dict[str, Any]) -> dict[str, Any]:
    experiences = []
    for exp in p.get("experience", []):
        bullets = [str(x) for x in exp.get("achievements", []) if not _forbidden_claim(str(x))][:3]
        experiences.append({"company": exp.get("company", ""), "location": "Santiago", "role": exp.get("role", ""), "dates": exp.get("dates", ""), "bullets": bullets})
    education = [{"credential": str(x), "institution": "", "dates": ""} for x in p.get("education", [])]
    return {"summary": _words(p.get("summary", ""), 95), "experience": experiences, "education": education, "skills": p.get("skills", [])[:8], "tools": p.get("tools", [])[:7], "languages": p.get("languages", [])}


def adapt_cv_content(analysis: dict[str, Any]) -> dict[str, Any]:
    p = profile()
    if analysis.get("_demo"):
        result = _demo_cv_content(p)
    else:
        prompt = f"""Crea el contenido final de un CV adaptado a esta oferta. {CV_SCHEMA}

OFERTA Y ANÁLISIS INTERNO:
{json.dumps(analysis, ensure_ascii=False)[:60000]}

PERFIL MAESTRO ESTRUCTURADO:
{json.dumps(p, ensure_ascii=False)[:70000]}

EVIDENCIA TEXTUAL DE TODOS LOS CV BASE:
{master_text()[:110000]}
"""
        result = ask_json(prompt)

    clean_experiences = []
    adapted = result.get("experience", [])
    for base_exp in p.get("experience", []):
        company = str(base_exp.get("company", ""))
        match = next((x for x in adapted if company.lower() in str(x.get("company", "")).lower() or str(x.get("company", "")).lower() in company.lower()), None)
        source = match or base_exp
        bullets = source.get("bullets", source.get("achievements", []))
        bullets = [_words(x, 24) for x in bullets if str(x).strip() and not _forbidden_claim(str(x))][:3]
        if not bullets:
            bullets = [_words(x, 24) for x in base_exp.get("achievements", []) if not _forbidden_claim(str(x))][:2]
        clean_experiences.append({
            "company": company,
            "location": str(source.get("location", "Santiago")),
            "role": str(base_exp.get("role", source.get("role", ""))),
            "dates": str(base_exp.get("dates", source.get("dates", ""))),
            "bullets": bullets,
        })
    result["experience"] = clean_experiences
    result["summary"] = _words(result.get("summary", p.get("summary", "")), 105)
    result["skills"] = list(dict.fromkeys(str(x) for x in result.get("skills", []) if str(x).strip()))[:8]
    result["tools"] = list(dict.fromkeys(str(x) for x in result.get("tools", []) if str(x).strip()))[:7]
    result["languages"] = result.get("languages") or p.get("languages", [])
    result["education"] = result.get("education") or [{"credential": str(x), "institution": "", "dates": ""} for x in p.get("education", [])]
    return result


def _cv_styles(compact: bool = False) -> dict[str, ParagraphStyle]:
    body_size = 9.4 if compact else 9.8
    leading = 11.2 if compact else 11.8
    return {
        "name": ParagraphStyle("CVName", fontName="Helvetica-Bold", fontSize=18, leading=21, alignment=TA_CENTER, spaceAfter=1),
        "contact": ParagraphStyle("CVContact", fontName="Helvetica", fontSize=10.4, leading=12.2, alignment=TA_CENTER, spaceAfter=0),
        "section": ParagraphStyle("CVSection", fontName="Helvetica-Bold", fontSize=10.8, leading=13, alignment=TA_LEFT),
        "body": ParagraphStyle("CVBody", fontName="Helvetica", fontSize=body_size, leading=leading, alignment=TA_JUSTIFY, spaceAfter=1),
        "company": ParagraphStyle("CVCompany", fontName="Helvetica", fontSize=body_size, leading=leading, alignment=TA_LEFT),
        "date": ParagraphStyle("CVDate", fontName="Helvetica", fontSize=body_size, leading=leading, alignment=TA_LEFT),
        "role": ParagraphStyle("CVRole", fontName="Helvetica-Bold", fontSize=body_size, leading=leading, alignment=TA_LEFT, spaceAfter=1),
        "bullet": ParagraphStyle("CVBullet", fontName="Helvetica", fontSize=body_size, leading=leading, alignment=TA_JUSTIFY, leftIndent=12, firstLineIndent=0, bulletIndent=0, spaceAfter=1),
        "label": ParagraphStyle("CVLabel", fontName="Helvetica-Bold", fontSize=body_size, leading=leading, alignment=TA_LEFT),
    }


def _section(title: str, styles: dict[str, ParagraphStyle]) -> Table:
    table = Table([[Paragraph(safe(title), styles["section"])]], colWidths=[150*mm], hAlign="LEFT")
    table.setStyle(TableStyle([("LINEABOVE", (0,0), (-1,-1), 0.7, colors.black), ("LINEBELOW", (0,0), (-1,-1), 0.7, colors.black), ("LEFTPADDING", (0,0), (-1,-1), 0), ("RIGHTPADDING", (0,0), (-1,-1), 0), ("TOPPADDING", (0,0), (-1,-1), 1.5), ("BOTTOMPADDING", (0,0), (-1,-1), 1.5)]))
    return table


def _build_cv_pdf(target: Path, content: dict[str, Any], p: dict[str, Any], compact: bool = False, bullet_limit: int = 3) -> None:
    styles = _cv_styles(compact)
    personal = personal_data()
    contact = {**p.get("contact", {}), **{k: v for k, v in personal.items() if k != "name" and v}}
    email = str(contact.get("email", ""))
    linkedin = str(contact.get("linkedin", "") or "www.linkedin.com/in/rene-romero-lezama")
    if linkedin and not linkedin.startswith(("http://", "https://")):
        linkedin_href = "https://" + linkedin
    else:
        linkedin_href = linkedin
    story: list[Any] = [
        Paragraph(safe(personal.get("name") or p.get("name")), styles["name"]),
        Paragraph(safe(contact.get("location", "")), styles["contact"]),
        Paragraph(safe(contact.get("phone", "")), styles["contact"]),
    ]
    if email:
        story.append(Paragraph(f'<link href="mailto:{safe(email)}" color="blue"><u>{safe(email)}</u></link>', styles["contact"]))
    story += [Spacer(1, 6*mm), _section("Resumen Profesional", styles), Spacer(1, 1.2*mm), Paragraph(safe(content.get("summary", "")), styles["body"]), Spacer(1, 3*mm), _section("Antecedentes Laborales", styles), Spacer(1, 2.5*mm)]

    for exp in content.get("experience", []):
        company_line = f"<b>{safe(exp.get('company'))}</b>"
        if exp.get("location"): company_line += f". {safe(exp.get('location'))}"
        heading = Table([[Paragraph(company_line, styles["company"]), Paragraph(safe(exp.get("dates", "")), styles["date"])]], colWidths=[108*mm, 42*mm], hAlign="LEFT")
        heading.setStyle(TableStyle([("VALIGN", (0,0), (-1,-1), "TOP"), ("ALIGN", (1,0), (1,0), "RIGHT"), ("LEFTPADDING", (0,0), (-1,-1), 0), ("RIGHTPADDING", (0,0), (-1,-1), 0), ("TOPPADDING", (0,0), (-1,-1), 0), ("BOTTOMPADDING", (0,0), (-1,-1), 0)]))
        bullets = exp.get("bullets", [])[:bullet_limit]
        first = Paragraph(safe(bullets[0]), styles["bullet"], bulletText="•") if bullets else Spacer(1, 0)
        story.append(KeepTogether([heading, Paragraph(safe(exp.get("role", "")), styles["role"]), first]))
        for item in bullets[1:]:
            story.append(Paragraph(safe(item), styles["bullet"], bulletText="•"))
        story.append(Spacer(1, 2.2*mm if not compact else 1.4*mm))

    story += [_section("Antecedentes Académicos", styles), Spacer(1, 2*mm)]
    for edu in content.get("education", []):
        if isinstance(edu, str): edu = {"credential": edu, "institution": "", "dates": ""}
        row = Table([[Paragraph(f"<b>{safe(edu.get('credential'))}</b><br/>{safe(edu.get('institution',''))}", styles["body"]), Paragraph(safe(edu.get("dates", "")), styles["date"])]], colWidths=[118*mm, 32*mm], hAlign="LEFT")
        row.setStyle(TableStyle([("ALIGN", (1,0), (1,0), "RIGHT"), ("VALIGN", (0,0), (-1,-1), "TOP"), ("LEFTPADDING", (0,0), (-1,-1), 0), ("RIGHTPADDING", (0,0), (-1,-1), 0), ("TOPPADDING", (0,0), (-1,-1), 0), ("BOTTOMPADDING", (0,0), (-1,-1), 1)]))
        story.append(row)

    skill_text = "<br/>".join(f"• {safe(x)}" for x in content.get("skills", []))
    tool_text = "<br/>".join(f"• {safe(x)}" for x in content.get("tools", []))
    abilities = Table([[Paragraph("Habilidades", styles["label"]), Paragraph(skill_text, styles["body"])], [Paragraph("Software", styles["label"]), Paragraph(tool_text, styles["body"])]], colWidths=[40*mm, 110*mm], hAlign="LEFT")
    abilities.setStyle(TableStyle([("VALIGN", (0,0), (-1,-1), "TOP"), ("LEFTPADDING", (0,0), (-1,-1), 2), ("RIGHTPADDING", (0,0), (-1,-1), 2), ("TOPPADDING", (0,0), (-1,-1), 1), ("BOTTOMPADDING", (0,0), (-1,-1), 2)]))
    story += [Spacer(1, 2.5*mm), KeepTogether([_section("Habilidades", styles), Spacer(1, 2*mm), abilities]), Spacer(1, 2.5*mm), _section("Información Adicional", styles), Spacer(1, 2*mm)]
    language = ", ".join(str(x) for x in content.get("languages", []))
    if language: story.append(Paragraph(f"<b>Idiomas:</b>&nbsp;&nbsp;&nbsp;{safe(language)}", styles["body"]))
    if linkedin: story.append(Paragraph(f'<b>LinkedIn:</b>&nbsp;&nbsp;&nbsp;<link href="{safe(linkedin_href)}" color="blue"><u>{safe(linkedin.replace("https://", ""))}</u></link>', styles["body"]))

    doc = SimpleDocTemplate(str(target), pagesize=A4, rightMargin=30*mm, leftMargin=30*mm, topMargin=12.5*mm, bottomMargin=24.9*mm, title="CV adaptado", author=str(p.get("name", "")))
    doc.build(story)


def make_cv(app_id: int, analysis: dict[str, Any]) -> Path:
    p = profile()
    issues = profile_issues(p)
    if issues:
        raise RuntimeError("Completa o vuelve a consolidar el perfil maestro. Faltan: " + ", ".join(issues) + ".")
    content = analysis.get("adapted_cv") or adapt_cv_content(analysis)
    target = GENERATED / f"CV_adaptado_{app_id}.pdf"
    _build_cv_pdf(target, content, p, compact=False, bullet_limit=3)
    if len(PdfReader(str(target)).pages) > 2:
        content["summary"] = _words(content.get("summary", ""), 82)
        content["skills"] = content.get("skills", [])[:7]
        content["tools"] = content.get("tools", [])[:6]
        _build_cv_pdf(target, content, p, compact=True, bullet_limit=2)
    pages = len(PdfReader(str(target)).pages)
    if pages > 2:
        raise RuntimeError("No fue posible ajustar el CV a dos páginas sin eliminar empresas. Revisa y acorta el perfil maestro.")
    analysis["adapted_cv"] = content
    with conn() as c:
        c.execute("UPDATE applications SET cv_path=?, analysis_json=? WHERE id=?", (str(target), json.dumps(analysis, ensure_ascii=False), app_id))
    cloud_upload(target, f"generated/{target.name}", "application/pdf")
    cloud_backup_db()
    return target


def section_list(title: str, items: list[Any], first: str, second: str) -> None:
    st.subheader(title)
    if not items: st.caption("Sin elementos identificados.")
    for item in items[:3]:
        if isinstance(item, dict): st.markdown(f"- **{item.get(first,'')}**" + (f" — {item.get(second,'')}" if item.get(second) else ""))
        else: st.markdown(f"- {item}")


def render_result(app_id: int, a: dict[str, Any]) -> None:
    score = int(a.get("score", 0)); rec = recommendation(score)
    color = "good" if score >= 80 else "warn" if score >= 75 else "bad"
    c1, c2, c3 = st.columns(3)
    c1.metric("Compatibilidad", f"{score}%")
    c2.markdown(f"<div class='card'><span class='{color}'><b>{rec}</b></span><br><small>Regla automática</small></div>", unsafe_allow_html=True)
    c3.markdown(f"<div class='card'><b>{safe(a.get('role','Cargo no detectado'))}</b><br><span class='muted'>{safe(a.get('company',''))}</span></div>", unsafe_allow_html=True)
    st.info(a.get("match_explanation", ""))
    st.subheader("Resumen del cargo"); st.write(a.get("summary", ""))
    left, right = st.columns(2)
    with left: section_list("Áreas fuertes", a.get("strengths", []), "area", "")
    with right: section_list("Áreas débiles", a.get("weaknesses", []), "area", "")
    if score >= 75:
        if st.button("Generar CV PDF adaptado", type="primary", key=f"cv_{app_id}"):
            try:
                with st.spinner("Adaptando el contenido a la oferta y preparando el PDF..."):
                    path = make_cv(app_id, a)
                st.success("CV adaptado generado: máximo dos páginas, optimizado para lectura humana y ATS.")
                st.download_button("Descargar CV adaptado", path.read_bytes(), path.name, "application/pdf", key=f"dl_{app_id}")
            except Exception as e:
                st.error(f"No fue posible generar el CV: {e}")
        st.divider(); st.subheader("Preguntas de la postulación")
        suggestions = a.get("suggested_questions") or ["Comente su experiencia en el cargo.", "¿Cuenta con experiencia en proyectos similares?", "Indique sus pretensiones de renta."]
        q = st.selectbox("Pregunta sugerida", suggestions + ["Otra pregunta"] , key=f"qs_{app_id}")
        custom = st.text_input("Pregunta real", key=f"qc_{app_id}") if q == "Otra pregunta" else q
        limit = st.number_input("Máximo de caracteres", 100, 2000, 500, 50, key=f"lim_{app_id}")
        if st.button("Generar respuesta breve", key=f"ans_{app_id}"):
            try:
                answer_obj = ask_json(f"Responde esta pregunta de postulación en español, con máximo {limit} caracteres, usando solo evidencia del perfil y la oferta. Devuelve JSON {{\"answer\":\"\"}}. Pregunta: {custom}\nPerfil:{json.dumps(profile(),ensure_ascii=False)}\nOferta:{json.dumps(a,ensure_ascii=False)}")
                answer = str(answer_obj.get("answer", ""))[:limit]
                with conn() as c: c.execute("INSERT INTO answers(application_id,question,answer,created_at) VALUES(?,?,?,?)", (app_id, custom, answer, datetime.now().isoformat(timespec="seconds")))
                cloud_backup_db()
                st.text_area("Respuesta", answer, height=120)
            except Exception as e: st.error(str(e))
    else:
        st.warning("El flujo termina aquí: con menos de 75% no se habilita la generación de CV ni respuestas.")


init_db()
restore_cloud_files()
recovered_documents = recover_uploaded_documents()
if recovered_documents: cloud_backup_db()
st.markdown("<div class='hero'><h1>Compatibilidad CV–Postulación</h1><p>Tu perfil se carga una vez. Después, solo analiza nuevas ofertas.</p></div>", unsafe_allow_html=True)
nav = st.radio("Navegación", ["📸 Nueva", "👤 Perfil", "🗂 Historial", "⚙️ Ajustes"], horizontal=True, label_visibility="collapsed")
page = {"📸 Nueva":"Nueva postulación", "👤 Perfil":"Mi perfil maestro", "🗂 Historial":"Historial", "⚙️ Ajustes":"Configuración"}[nav]

if page == "Configuración":
    st.header("Configuración")
    if server_secret("OPENAI_API_KEY"):
        st.success("OpenAI está configurado de forma segura en el servidor.")
    else:
        st.warning("Modo local: falta configurar OpenAI en los secretos del servidor.")
        st.session_state.api_key = st.text_input("OpenAI API key temporal", value=st.session_state.get("api_key", ""), type="password", help="Solo para pruebas locales; no se guarda.")
    if cloud_enabled(): st.success("Almacenamiento privado conectado.")
    else: st.info("Almacenamiento local. Al publicar, conecta Supabase desde los secretos del servidor.")
    model = st.text_input("Modelo", value=setting("model", "gpt-5.6-sol"))
    if st.button("Guardar modelo"): save_setting("model", model.strip()); st.success("Configuración guardada.")
    st.caption("Las claves nunca deben pegarse en el teléfono ni guardarse en la base de datos.")

elif page == "Mi perfil maestro":
    st.header("Mi perfil maestro")
    with conn() as c: docs = c.execute("SELECT * FROM cv_documents ORDER BY id DESC").fetchall()
    st.metric("CV procesados", len(docs)); st.caption("Última consolidación: " + (setting("profile_updated", "Aún no realizada")))
    if recovered_documents:
        st.success(f"Se recuperaron automáticamente {recovered_documents} CV que ya estaban guardados en la carpeta de la aplicación.")
    st.subheader("Datos de contacto")
    current_personal = personal_data()
    with st.form("personal_data_form"):
        pc1, pc2 = st.columns(2)
        with pc1:
            personal_name = st.text_input("Nombre completo", value=current_personal.get("name", ""))
            personal_phone = st.text_input("Teléfono", value=current_personal.get("phone", ""))
            personal_email = st.text_input("Correo", value=current_personal.get("email", ""))
        with pc2:
            personal_location = st.text_input("Ubicación", value=current_personal.get("location", ""))
            personal_linkedin = st.text_input("LinkedIn", value=current_personal.get("linkedin", ""))
        if st.form_submit_button("Guardar datos de contacto"):
            saved_personal = {"name": personal_name.strip(), "phone": personal_phone.strip(), "email": personal_email.strip(), "location": personal_location.strip(), "linkedin": personal_linkedin.strip()}
            save_setting("personal_data", json.dumps(saved_personal, ensure_ascii=False))
            existing_profile = profile()
            if existing_profile:
                existing_profile["name"] = saved_personal["name"] or existing_profile.get("name", "")
                existing_profile.setdefault("contact", {}).update({k: saved_personal[k] for k in ("phone", "email", "location", "linkedin") if saved_personal[k]})
                save_setting("master_profile", json.dumps(existing_profile, ensure_ascii=False))
            st.success("Datos de contacto guardados.")
            st.rerun()
    files = st.file_uploader("Agregar CV (puedes seleccionar varios)", type=["pdf","docx","txt"], accept_multiple_files=True)
    if st.button("Guardar CV seleccionados", disabled=not files):
        added = 0
        for f in files:
            raw = f.getvalue(); sha = hashlib.sha256(raw).hexdigest(); text = file_text(f.name, raw)
            if not text.strip(): st.warning(f"{f.name}: no se pudo extraer texto; si es un PDF escaneado, conviértelo a DOCX/TXT."); continue
            target = UPLOADS / f"{sha[:12]}_{Path(f.name).name}"; target.write_bytes(raw)
            with conn() as c:
                try: c.execute("INSERT INTO cv_documents(name,sha256,path,extracted_text,added_at) VALUES(?,?,?,?,?)", (f.name, sha, str(target), text, datetime.now().isoformat(timespec="seconds"))); added += 1
                except sqlite3.IntegrityError: pass
            cloud_upload(target, cloud_document_path(target))
        cloud_backup_db()
        st.success(f"{added} CV nuevos guardados de forma persistente."); st.rerun()
    if docs:
        for d in docs: st.write(f"✓ {d['name']} — {d['added_at']}")
        if st.button("Consolidar / actualizar perfil con IA", type="primary"):
            try:
                with st.spinner("Consolidando experiencia sin duplicados..."): rebuild_profile()
                st.success("Perfil maestro actualizado."); st.rerun()
            except Exception as e: st.error(str(e))
    p = profile()
    if p:
        edited = st.text_area("Perfil consolidado (JSON editable)", json.dumps(p, ensure_ascii=False, indent=2), height=420)
        if st.button("Guardar correcciones manuales"):
            try: save_setting("master_profile", json.dumps(json.loads(edited), ensure_ascii=False)); st.success("Correcciones guardadas.")
            except json.JSONDecodeError: st.error("El contenido no es JSON válido.")

elif page == "Nueva postulación":
    st.header("Nueva postulación")
    current_issues = profile_issues()
    if current_issues: st.warning("Antes de analizar una oferta, completa y consolida el perfil maestro. Faltan: " + ", ".join(current_issues) + ".")
    camera_file = st.camera_input("Tomar foto de la oferta")
    offer_file = st.file_uploader("O subir captura, PDF, Word o TXT", type=["png","jpg","jpeg","webp","pdf","docx","txt"])
    selected_file = camera_file or offer_file
    pasted = st.text_area("O pega aquí la publicación completa", height=180)
    demo = st.checkbox("Modo demostración (sin API)", help="Usa un análisis de ejemplo. Escribe 'score 74', 'score 77' o 'score 85' para probar los tres umbrales.")
    if st.button("Analizar compatibilidad", type="primary", disabled=(not selected_file and not pasted) or bool(current_issues)):
        try:
            text = pasted; images: list[tuple[str, bytes]] = []
            if selected_file:
                raw = selected_file.getvalue(); ext = Path(selected_file.name).suffix.lower()
                if ext in (".png", ".jpg", ".jpeg", ".webp"):
                    Image.open(io.BytesIO(raw)).verify(); images.append((selected_file.type or "image/jpeg", raw))
                else: text += "\n" + file_text(selected_file.name, raw)
            with st.spinner("Extrayendo oferta y comparando con el perfil..."):
                a = demo_analysis(text) if demo else analyze_offer(text, images)
                app_id = save_application(a, text or f"Imagen: {selected_file.name}")
            st.session_state.last_result = (app_id, a)
        except Exception as e: st.error(f"No fue posible analizar la oferta: {e}")
    if "last_result" in st.session_state: render_result(*st.session_state.last_result)

else:
    st.header("Historial")
    with conn() as c: apps = c.execute("SELECT * FROM applications ORDER BY id DESC").fetchall()
    if not apps: st.info("Aún no hay postulaciones analizadas.")
    for row in apps:
        with st.expander(f"{row['created_at'][:10]} · {row['role'] or 'Cargo'} · {row['company'] or 'Empresa'} · {row['score']}%"):
            st.write(row["recommendation"])
            status = st.selectbox("Estado", ["Por revisar","Postulada","Entrevista","Descartada","Finalizada"], index=["Por revisar","Postulada","Entrevista","Descartada","Finalizada"].index(row["status"]) if row["status"] in ["Por revisar","Postulada","Entrevista","Descartada","Finalizada"] else 0, key=f"status_{row['id']}")
            notes = st.text_area("Notas", row["notes"], key=f"notes_{row['id']}")
            if st.button("Guardar seguimiento", key=f"save_{row['id']}"):
                with conn() as c: c.execute("UPDATE applications SET status=?, notes=? WHERE id=?", (status, notes, row["id"]))
                cloud_backup_db()
                st.success("Seguimiento guardado.")
            a = json.loads(row["analysis_json"]); st.write(a.get("match_explanation", ""))
            if row["cv_path"] and Path(row["cv_path"]).exists():
                path = Path(row["cv_path"]); st.download_button("Descargar CV utilizado", path.read_bytes(), path.name, "application/pdf", key=f"histdl_{row['id']}")
            with conn() as c: answers = c.execute("SELECT question,answer FROM answers WHERE application_id=?", (row["id"],)).fetchall()
            for ans in answers: st.markdown(f"**{ans['question']}**\n\n{ans['answer']}")
