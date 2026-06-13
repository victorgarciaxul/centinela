#!/usr/bin/env python3
"""
Web app para el Agente Licitador — IMAGINE COMUNICACIÓN ANDALUZA S.L.U
"""

import json
import threading
from datetime import datetime, date
from pathlib import Path
from flask import Flask, jsonify, render_template_string, request
import psycopg2
import psycopg2.extras

BASE = Path(__file__).parent
AGENT_FILE = BASE / "agente_licitador.py"

app = Flask(__name__)
_agent_running = False
_agent_last_result = {"ok": None, "stdout": "", "stderr": "", "ts": None}


def cargar_config() -> dict:
    import os
    cfg_file = BASE / "config.json"
    cfg = json.loads(cfg_file.read_text(encoding="utf-8")) if cfg_file.exists() else {}
    # Variables de entorno tienen prioridad (Vercel)
    if os.environ.get("DATABASE_URL"):
        cfg["database_url"] = os.environ["DATABASE_URL"]
    if os.environ.get("GROK_API_KEY"):
        cfg["grok_api_key"] = os.environ["GROK_API_KEY"]
    return cfg

def get_conn():
    cfg = cargar_config()
    return psycopg2.connect(cfg["database_url"])

def load_results() -> dict:
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM licitaciones ORDER BY plazo ASC NULLS LAST")
            rows = cur.fetchall()
        conn.close()
        lics = []
        for r in rows:
            lics.append({
                "_id":    r["id"],
                "titulo": r["titulo"] or "",
                "organo": r["organo"] or "",
                "cpvs":   r["cpvs"] if isinstance(r["cpvs"], list) else json.loads(r["cpvs"] or "[]"),
                "importe": str(r["importe"]) if r["importe"] else "",
                "plazo":   str(r["plazo"]) if r["plazo"] else "",
                "enlace":  r["enlace"] or "",
                "fecha_deteccion": str(r["fecha_deteccion"]) if r["fecha_deteccion"] else "",
            })
        return {"licitaciones": lics, "actualizado": str(datetime.now())}
    except Exception as e:
        print(f"[DB ERROR] {e}")
        return {"licitaciones": [], "actualizado": None}


IRRELEVANT_KEYWORDS = [
    "agencia de viajes",
    "señalización de sendas", "sendas peatonales", "sendas ciclistas",
    "transporte permanente de señales",
    "azafatas/os en diferentes edificios", "azafatas en edificios",
    "certamen ganadero", "certámenes ganaderos",
    "coworking",
    "monitores deportivos",
    "festejos taurinos", "taurino",
    "contratación de orquestas", "grupos musicales para la caseta",
    "catalogación y digitalización de documentación",
    "ti y consultoría",
    "concesión administrativa demanial",
    "feria gastronómica de verano",
    "suministro temporal, instalación, puesta en servicio, control técnico",
    "arrendamiento, sin opción de compra, de equipos",
    "salidas gastronómicas",
    "peregrinos y visitantes",
    "rocódromo",
    "atención, información y asistencia",
]

def es_relevante_titulo(titulo: str) -> bool:
    t = titulo.lower()
    return not any(kw.lower() in t for kw in IRRELEVANT_KEYWORDS)

def es_activa(l: dict) -> bool:
    from datetime import date
    plazo = l.get("plazo", "N/D")
    if not plazo or plazo == "N/D":
        return False
    return plazo >= str(date.today())


# ── API ───────────────────────────────────────────────────────────────────────

@app.get("/api/licitaciones")
def api_licitaciones():
    data = load_results()
    lics = data.get("licitaciones", [])

    solo_activas    = request.args.get("activas", "1") == "1"
    solo_relevantes = request.args.get("relevantes", "1") == "1"

    if solo_activas:
        lics = [l for l in lics if es_activa(l)]
    if solo_relevantes:
        lics = [l for l in lics if es_relevante_titulo(l.get("titulo", ""))]

    q      = request.args.get("q", "").lower()
    cpv    = request.args.get("cpv", "")
    minimo = request.args.get("min", 0, type=float)

    if q:
        lics = [l for l in lics if q in l.get("titulo","").lower() or q in l.get("organo","").lower()]
    if cpv:
        lics = [l for l in lics if any(c.startswith(cpv) for c in l.get("cpvs", []))]
    if minimo:
        def importe_num(l):
            try: return float(l.get("importe") or 0)
            except: return 0
        lics = [l for l in lics if importe_num(l) >= minimo]

    lics.sort(key=lambda l: l.get("plazo") or "9999")

    return jsonify({
        "total": len(lics),
        "actualizado": data.get("actualizado"),
        "licitaciones": lics,
    })


@app.get("/api/stats")
def api_stats():
    data = load_results()
    lics = data.get("licitaciones", [])
    lics = [l for l in lics if es_activa(l) and es_relevante_titulo(l.get("titulo", ""))]
    from collections import Counter

    cpv_counter: Counter = Counter()
    for l in lics:
        for c in set(l.get("cpvs", [])):
            cpv_counter[c[:8]] += 1

    importes = []
    for l in lics:
        try:
            v = float(l.get("importe") or 0)
            if v > 0:
                importes.append(v)
        except:
            pass

    return jsonify({
        "total": len(lics),
        "top_cpvs": cpv_counter.most_common(8),
        "importe_total": sum(importes),
        "importe_medio": round(sum(importes)/len(importes), 2) if importes else 0,
        "importe_max": max(importes) if importes else 0,
        "actualizado": data.get("actualizado"),
    })


def _run_agent():
    global _agent_running, _agent_last_result
    import io, contextlib
    try:
        import agente_licitador
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            agente_licitador.main()
        _agent_last_result = {
            "ok": True,
            "stdout": buf.getvalue()[-4000:],
            "ts": datetime.now().strftime("%H:%M:%S"),
        }
    except Exception as e:
        _agent_last_result = {"ok": False, "msg": str(e), "ts": datetime.now().strftime("%H:%M:%S")}
    finally:
        _agent_running = False

@app.post("/api/ejecutar")
def api_ejecutar():
    global _agent_running
    if _agent_running:
        return jsonify({"ok": False, "msg": "El agente ya está en ejecución"})
    _agent_running = True
    _agent_last_result["ok"] = None  # reset
    threading.Thread(target=_run_agent, daemon=True).start()
    return jsonify({"ok": True, "msg": "Agente iniciado en background"})


CPV_NAMES_PY = {
    "79340000":"Publicidad y marketing","79341000":"Publicidad","79342000":"Marketing",
    "79341200":"Gestión de publicidad","79341400":"Campañas de publicidad","79342200":"Promoción",
    "79341100":"Colocación de publicidad","92210000":"Servicios de radio","92220000":"Servicios de televisión",
    "79822500":"Diseño gráfico","74812240":"Producción vídeos publicidad","92111250":"Producción vídeos información",
    "79800000":"Impresión","72413000":"Diseño web","72415000":"Alojamiento web",
    "79413000":"Consultoría marketing","79416000":"Relaciones públicas","79416200":"Asesoramiento RRPP",
    "79952000":"Organización de eventos","79956000":"Ferias y exposiciones",
}

@app.post("/api/analizar")
def api_analizar():
    import urllib.request as urlreq
    data = request.get_json()
    titulo  = data.get("titulo", "Sin título")
    organo  = data.get("organo", "Desconocido")
    importe = data.get("importe", "N/D")
    plazo   = data.get("plazo", "N/D")
    cpvs    = data.get("cpvs", [])
    enlace  = data.get("enlace", "")

    config  = cargar_config()
    api_key = config.get("grok_api_key", "").strip()
    if not api_key:
        return jsonify({"ok": False, "msg": "Falta la API key de Grok. Añádela en config.json → grok_api_key"})

    cpv_desc = ", ".join(CPV_NAMES_PY.get(c[:8], c) for c in dict.fromkeys(c[:8] for c in cpvs) if c)

    try:
        imp_fmt = f"{float(importe):,.0f} €" if importe and importe not in ("N/D","") else "No especificado"
    except:
        imp_fmt = importe or "No especificado"

    prompt = f"""Eres un experto en contratación pública española. Analiza esta licitación para IMAGINE COMUNICACIÓN ANDALUZA S.L.U, agencia especializada en publicidad, marketing, diseño, eventos, relaciones públicas y comunicación digital.

LICITACIÓN:
• Título: {titulo}
• Órgano contratante: {organo}
• Importe estimado: {imp_fmt}
• Plazo de presentación: {plazo}
• Categorías CPV: {cpv_desc or "No especificadas"}
• Enlace: {enlace or "No disponible"}

Proporciona un análisis estructurado con estas secciones exactas (usa los emojis indicados como cabecera):

🎯 RESUMEN
Qué se lícita en 2-3 frases concretas.

📋 SERVICIOS REQUERIDOS
Lista de puntos con lo que pide exactamente el contrato.

⚡ PUNTOS CRÍTICOS
Requisitos, condiciones o restricciones importantes para poder presentarse (solvencia, plazos, exclusividad, etc.).

💡 OPORTUNIDAD PARA IMAGINE
Puntuación del 1-10 y justificación de por qué encaja (o no) con el perfil de la agencia.

✅ RECOMENDACIÓN
Presentarse / No presentarse / Considerar, con motivo en 1-2 frases.

🚀 PRÓXIMOS PASOS
Lista de 3-5 acciones concretas para preparar la oferta si se decide concurrir.

Responde en español, de forma directa y práctica. Sin introducciones ni conclusiones genéricas."""

    payload = json.dumps({
        "model": "llama-3.3-70b-versatile",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 1200,
        "temperature": 0.2,
    }).encode("utf-8")

    import http.client, ssl
    try:
        ctx = ssl.create_default_context()
        conn = http.client.HTTPSConnection("api.groq.com", context=ctx, timeout=45)
        conn.request(
            "POST",
            "/openai/v1/chat/completions",
            body=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )
        resp = conn.getresponse()
        body = resp.read().decode("utf-8")
        conn.close()
        if resp.status != 200:
            return jsonify({"ok": False, "msg": f"Groq HTTP {resp.status}: {body[:200]}"})
        result   = json.loads(body)
        analysis = result["choices"][0]["message"]["content"]
        return jsonify({"ok": True, "analysis": analysis})
    except Exception as e:
        return jsonify({"ok": False, "msg": f"Error Groq: {e}"})


@app.get("/api/estado")
def api_estado():
    cache_ids = 0
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM cache_ids")
            cache_ids = cur.fetchone()[0]
        conn.close()
    except:
        pass
    return jsonify({
        "agente_corriendo": _agent_running,
        "licitaciones_en_cache": cache_ids,
        "ultimo_resultado": _agent_last_result,
    })


# ── Frontend ──────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Centinela — IMAGINE</title>
<style>
*{box-sizing:border-box;margin:0;padding:0;}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;background:#fff;min-height:100vh;display:flex;overflow:hidden;}

/* App shell */
.app{display:flex;width:100%;height:100vh;background:#fff;overflow:hidden;}

/* Sidebar */
.sidebar{width:220px;background:#fff;border-right:1px solid #EBEBEB;display:flex;flex-direction:column;flex-shrink:0;height:100vh;overflow:hidden;}
.sidebar-head{padding:16px 16px 10px;display:flex;align-items:center;border-bottom:1px solid #F0F0F0;}
.sidebar-logo{font-size:14px;font-weight:600;color:#111;display:flex;align-items:center;gap:9px;}
.sidebar-logo-icon{width:26px;height:26px;display:flex;align-items:center;justify-content:center;}
.sb-section{padding:10px 10px 4px;overflow-y:auto;flex:1;min-height:0;}
.sb-label{font-size:10px;font-weight:600;color:#AAAAAA;letter-spacing:.8px;text-transform:uppercase;padding:0 6px;margin-bottom:4px;}
.sb-item{display:flex;align-items:center;gap:8px;padding:7px 8px;border-radius:7px;cursor:pointer;font-size:13px;color:#444;transition:background .12s,color .12s;user-select:none;}
.sb-item:hover{background:#F5F5F5;color:#111;}
.sb-item.active{background:#F0EFFD;color:#4A4AE8;font-weight:500;}
.sb-item .sb-icon{font-size:15px;width:18px;text-align:center;flex-shrink:0;}
.sb-item .sb-badge{margin-left:auto;background:#F0F0F0;color:#777;font-size:10px;font-weight:600;padding:1px 6px;border-radius:10px;}
.sb-item.active .sb-badge{background:#DDD8FC;color:#4A4AE8;}
.sb-item .sb-dot{width:7px;height:7px;border-radius:50%;flex-shrink:0;}
.sb-item .sb-dot.red{background:#F87171;}
.sb-item .sb-dot.amber{background:#FBBF24;}
.sb-item .sb-dot.green{background:#34D399;}
.sb-item .sb-dot.blue{background:#60A5FA;}
.sb-footer{flex-shrink:0;padding:12px;border-top:1px solid #F0F0F0;}
.sb-user{display:flex;align-items:center;gap:8px;}
.sb-user-av{width:28px;height:28px;border-radius:50%;background:#4A4AE8;color:#fff;display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:600;flex-shrink:0;}
.sb-user-name{font-size:12px;font-weight:500;color:#222;}
.sb-user-email{font-size:10px;color:#999;}

/* Main */
.main{flex:1;display:flex;flex-direction:column;overflow:hidden;background:#FAFAFA;}

/* Topbar */
.topbar{background:#fff;border-bottom:1px solid #EBEBEB;padding:0 24px;height:52px;display:flex;align-items:center;gap:12px;flex-shrink:0;}
.topbar-search{display:flex;align-items:center;gap:6px;background:#F5F5F5;border:1px solid #EBEBEB;border-radius:8px;padding:0 10px;height:32px;flex:1;max-width:300px;}
.topbar-search input{border:none;background:transparent;font-size:13px;color:#333;outline:none;width:100%;}
.topbar-search input::placeholder{color:#BBBBBB;}
.topbar-search .search-icon{color:#BBBBBB;font-size:13px;}
.topbar-sep{flex:1;}
.topbar-actions{display:flex;align-items:center;gap:8px;}
.btn-primary{background:#111;color:#fff;border:none;border-radius:8px;padding:0 16px;height:36px;font-size:13px;font-weight:500;cursor:pointer;display:flex;align-items:center;gap:6px;transition:background .15s;white-space:nowrap;flex-shrink:0;}
.btn-primary:hover{background:#333;}
.btn-primary:disabled{background:#BBB;cursor:not-allowed;}
.btn-primary .spinner{display:none;width:13px;height:13px;border:2px solid rgba(255,255,255,.3);border-top-color:#fff;border-radius:50%;animation:spin .7s linear infinite;}
.btn-primary.loading .spinner{display:block;}
.btn-primary.loading .btn-label{display:none;}
@keyframes spin{to{transform:rotate(360deg);}}

/* Page header */
.page-head{padding:20px 24px 0;}
.page-title-row{display:flex;align-items:center;gap:12px;margin-bottom:20px;}
.page-title{font-size:22px;font-weight:600;color:#111;}
.page-badge{background:#F0EFFD;color:#4A4AE8;font-size:12px;font-weight:500;padding:3px 10px;border-radius:20px;border:1px solid #DDD8FC;}

/* Stats row */
.stats-row{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;padding:0 24px 20px;}
.stat-card{background:#fff;border:1px solid #EBEBEB;border-radius:10px;padding:14px 16px;}
.stat-card .s-label{font-size:11px;color:#AAAAAA;text-transform:uppercase;letter-spacing:.6px;margin-bottom:6px;display:flex;align-items:center;gap:5px;}
.stat-card .s-val{font-size:16px;font-weight:600;color:#111;line-height:1.2;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.stat-card .s-sub{font-size:11px;color:#BBBBBB;margin-top:3px;}
.stat-card.blue .s-val{color:#4A4AE8;}
.stat-card.green .s-val{color:#059669;}
.stat-card.amber .s-val{color:#D97706;}
.stat-card.red .s-val{color:#DC2626;}

/* Toolbar */
.toolbar{padding:0 24px 14px;display:flex;align-items:center;gap:10px;}
.tabs{display:flex;gap:2px;background:#F0F0F0;border-radius:8px;padding:3px;overflow-x:auto;flex-shrink:0;}
.tab{padding:5px 14px;border-radius:6px;font-size:12px;font-weight:500;color:#666;cursor:pointer;transition:background .12s,color .12s;border:none;background:transparent;white-space:nowrap;}
.tab.active{background:#fff;color:#111;box-shadow:0 1px 3px rgba(0,0,0,.1);}
.tb-sep{flex:1;}
.tb-select{height:32px;border:1px solid #EBEBEB;border-radius:7px;background:#fff;padding:0 10px;font-size:12px;color:#444;outline:none;cursor:pointer;}
.filter-btn{height:32px;border:1px solid #EBEBEB;border-radius:7px;background:#fff;padding:0 12px;font-size:12px;color:#444;cursor:pointer;display:flex;align-items:center;gap:5px;}
.filter-btn:hover{background:#F5F5F5;}
.count-pill{background:#F0F0F0;color:#666;font-size:11px;font-weight:600;padding:2px 8px;border-radius:10px;}

/* Card grid */
.cards-area{flex:1;overflow-y:auto;padding:0 24px 24px;}
.card-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:12px;}

/* Licitacion card */
.lic-card{background:#fff;border:1px solid #EBEBEB;border-radius:10px;padding:0;cursor:pointer;transition:border-color .15s,box-shadow .15s;overflow:hidden;}
.lic-card:hover{border-color:#C7C7C7;box-shadow:0 2px 12px rgba(0,0,0,.07);}
.lc-top{padding:12px 14px 10px;border-bottom:1px solid #F5F5F5;}
.lc-due{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px;}
.due-chip{display:inline-flex;align-items:center;gap:4px;font-size:11px;font-weight:500;padding:3px 8px;border-radius:5px;}
.due-chip.urgente{background:#FEF2F2;color:#DC2626;}
.due-chip.pronto{background:#FFFBEB;color:#D97706;}
.due-chip.ok{background:#F0FDF4;color:#059669;}
.due-chip.lejano{background:#F5F5F5;color:#888;}
.lc-menu{color:#CCCCCC;font-size:16px;cursor:pointer;padding:2px;}
.lc-menu:hover{color:#888;}
.lc-title{font-size:13px;font-weight:600;color:#111;line-height:1.45;margin-bottom:4px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;}
.lc-organo{font-size:12px;color:#888;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.lc-bottom{padding:10px 14px;display:flex;align-items:center;justify-content:space-between;gap:8px;}
.lc-cpvs{display:flex;gap:5px;flex-wrap:wrap;flex:1;min-width:0;}
.cpv-chip{background:#F5F4FF;color:#4A4AE8;font-size:10px;font-weight:500;padding:2px 7px;border-radius:4px;white-space:nowrap;max-width:130px;overflow:hidden;text-overflow:ellipsis;}
.lc-importe{font-size:13px;font-weight:600;color:#111;white-space:nowrap;flex-shrink:0;}
.lc-importe.nd{color:#CCCCCC;font-weight:400;font-size:12px;}
.lc-actions{padding:0 14px 12px;display:flex;gap:7px;}
.btn-analizar{flex:1;height:30px;background:#111;color:#fff;border:none;border-radius:7px;font-size:12px;font-weight:500;cursor:pointer;display:flex;align-items:center;justify-content:center;gap:5px;transition:background .15s;}
.btn-analizar:hover{background:#333;}
.btn-analizar .a-spin{display:none;width:11px;height:11px;border:2px solid rgba(255,255,255,.3);border-top-color:#fff;border-radius:50%;animation:spin .6s linear infinite;}
.btn-analizar.loading .a-spin{display:block;}
.btn-analizar.loading .a-label{display:none;}
.btn-ver{height:30px;background:#fff;color:#444;border:1px solid #E0E0E0;border-radius:7px;font-size:12px;padding:0 10px;cursor:pointer;display:flex;align-items:center;gap:4px;white-space:nowrap;transition:background .12s;}
.btn-ver:hover{background:#F5F5F5;}

/* Empty state */
.empty-state{text-align:center;padding:80px 20px;color:#BBBBBB;}
.empty-state .e-icon{font-size:40px;margin-bottom:12px;}
.empty-state p{font-size:14px;}

/* Modal */
.modal-bg{display:none;position:fixed;inset:0;background:rgba(0,0,0,.4);z-index:100;align-items:center;justify-content:center;padding:20px;}
.modal-analisis{max-width:680px;}
.analysis-body{font-size:13px;color:#333;line-height:1.75;}
.analysis-body h3{font-size:13px;font-weight:600;color:#111;margin:16px 0 5px;display:flex;align-items:center;gap:5px;}
.analysis-body h3:first-child{margin-top:0;}
.analysis-body ul{padding-left:16px;margin:0 0 8px;}
.analysis-body li{margin-bottom:3px;}
.analysis-body p{margin:0 0 8px;}
.analysis-loading{display:flex;flex-direction:column;align-items:center;justify-content:center;padding:50px 20px;gap:14px;color:#999;}
.analysis-loading .big-spin{width:32px;height:32px;border:3px solid #F0EFFD;border-top-color:#4A4AE8;border-radius:50%;animation:spin .8s linear infinite;}
.no-api-warn{background:#FFFBEB;border:1px solid #FDE68A;border-radius:8px;padding:12px 14px;font-size:12px;color:#92400E;line-height:1.5;}
.no-api-warn code{background:#FEF3C7;padding:1px 5px;border-radius:3px;font-family:monospace;}
.modal-bg.open{display:flex;}
.modal{background:#fff;border-radius:14px;width:100%;max-width:600px;max-height:85vh;overflow-y:auto;box-shadow:0 20px 60px rgba(0,0,0,.2);}
.modal-head{padding:20px 22px 16px;border-bottom:1px solid #F0F0F0;display:flex;align-items:flex-start;gap:12px;}
.modal-head-icon{width:36px;height:36px;background:#F0EFFD;border-radius:8px;display:flex;align-items:center;justify-content:center;color:#4A4AE8;font-size:18px;flex-shrink:0;margin-top:2px;}
.modal-head h2{font-size:15px;font-weight:600;color:#111;line-height:1.4;flex:1;}
.modal-close{width:28px;height:28px;border-radius:6px;border:1px solid #EBEBEB;background:#fff;cursor:pointer;color:#888;font-size:16px;display:flex;align-items:center;justify-content:center;flex-shrink:0;}
.modal-close:hover{background:#F5F5F5;}
.modal-body{padding:18px 22px;}
.mf{margin-bottom:14px;}
.mf label{font-size:11px;font-weight:600;color:#AAAAAA;text-transform:uppercase;letter-spacing:.6px;display:block;margin-bottom:4px;}
.mf p{font-size:13px;color:#222;}
.mf .mf-val{font-size:18px;font-weight:600;}
.modal-footer{padding:14px 22px;border-top:1px solid #F0F0F0;}
.btn-link{display:inline-flex;align-items:center;gap:6px;background:#111;color:#fff;text-decoration:none;padding:9px 18px;border-radius:8px;font-size:13px;font-weight:500;}
.btn-link:hover{background:#333;}

/* Log modal */
.log-pre{background:#111;color:#a8ff78;font-family:'Courier New',monospace;font-size:11px;padding:14px;border-radius:8px;white-space:pre-wrap;max-height:380px;overflow-y:auto;margin-top:12px;}

/* Toast */
.toast{position:fixed;bottom:24px;right:24px;background:#fff;border:1px solid #EBEBEB;border-radius:10px;padding:12px 16px;font-size:13px;color:#333;box-shadow:0 4px 20px rgba(0,0,0,.12);z-index:200;transform:translateY(60px);opacity:0;transition:all .25s;max-width:320px;}
.toast.show{transform:translateY(0);opacity:1;}
.toast.ok{border-left:3px solid #059669;}
.toast.err{border-left:3px solid #DC2626;}
</style>
</head>
<body>
<div class="app">

  <!-- Sidebar -->
  <div class="sidebar">
    <div class="sidebar-head">
      <div class="sidebar-logo">
        <div class="sidebar-logo-icon">
          <svg width="26" height="26" viewBox="0 0 26 26" fill="none" xmlns="http://www.w3.org/2000/svg">
            <rect width="26" height="26" rx="7" fill="#111"/>
            <circle cx="13" cy="11" r="4.5" stroke="white" stroke-width="1.6" fill="none"/>
            <circle cx="13" cy="11" r="1.5" fill="white"/>
            <line x1="13" y1="16" x2="13" y2="20" stroke="white" stroke-width="1.6" stroke-linecap="round"/>
            <line x1="9.5" y1="20" x2="16.5" y2="20" stroke="white" stroke-width="1.6" stroke-linecap="round"/>
            <path d="M7 11 A6 6 0 0 1 19 11" stroke="#4A4AE8" stroke-width="1.4" stroke-linecap="round" fill="none"/>
            <path d="M4.5 11 A8.5 8.5 0 0 1 21.5 11" stroke="#4A4AE8" stroke-width="1.1" stroke-linecap="round" fill="none" opacity="0.45"/>
          </svg>
        </div>
        Centinela
      </div>
    </div>


    <div class="sb-section">
      <div class="sb-label">Por categoría</div>
      <div class="sb-item" onclick="setCpv('')">
        <span class="sb-dot blue"></span> Todos los CPVs
      </div>
      <div class="sb-item" onclick="setCpv('79341000')">
        <span class="sb-dot blue"></span> Publicidad
      </div>
      <div class="sb-item" onclick="setCpv('79340000')">
        <span class="sb-dot blue"></span> Marketing
      </div>
      <div class="sb-item" onclick="setCpv('79952000')">
        <span class="sb-dot green"></span> Eventos
      </div>
      <div class="sb-item" onclick="setCpv('79956000')">
        <span class="sb-dot green"></span> Ferias
      </div>
      <div class="sb-item" onclick="setCpv('79822500')">
        <span class="sb-dot amber"></span> Diseño gráfico
      </div>
      <div class="sb-item" onclick="setCpv('72413000')">
        <span class="sb-dot amber"></span> Web
      </div>
      <div class="sb-item" onclick="setCpv('79416000')">
        <span class="sb-dot red"></span> RRPP
      </div>
      <div class="sb-item" onclick="setCpv('79800000')">
        <span class="sb-dot" style="background:#9CA3AF"></span> Impresión
      </div>
    </div>

    <div class="sb-footer">
      <div class="sb-user">
        <div class="sb-user-av">IM</div>
        <div>
          <div class="sb-user-name">IMAGINE</div>
          <div class="sb-user-email">vrdesign87@gmail.com</div>
        </div>
      </div>
    </div>
  </div>

  <!-- Main -->
  <div class="main">

    <!-- Topbar -->
    <div class="topbar">
      <div class="topbar-search">
        <span class="search-icon">🔍</span>
        <input type="text" id="search" placeholder="Buscar licitación u órgano…" oninput="buscar()">
      </div>
      <div class="topbar-sep"></div>
      <div class="topbar-actions">
        <button class="btn-primary" id="btn-run" onclick="ejecutarAgente()">
          <span class="btn-label">▶ Buscar ahora</span>
          <div class="spinner"></div>
        </button>
      </div>
    </div>

    <!-- Page head -->
    <div class="page-head">
      <div class="page-title-row">
        <div class="page-title" id="page-title">Licitaciones activas</div>
        <div class="page-badge">IMAGINE COMUNICACIÓN ANDALUZA</div>
      </div>
    </div>

    <!-- Stats -->
    <div class="stats-row">
      <div class="stat-card blue">
        <div class="s-label">📋 Licitaciones</div>
        <div class="s-val" id="stat-total">—</div>
        <div class="s-sub" id="stat-update">Última actualización: —</div>
      </div>
      <div class="stat-card green">
        <div class="s-label">💰 Importe total</div>
        <div class="s-val" id="stat-importe">—</div>
        <div class="s-sub">mercado estimado</div>
      </div>
      <div class="stat-card amber">
        <div class="s-label">📊 Importe medio</div>
        <div class="s-val" id="stat-medio">—</div>
        <div class="s-sub">por licitación</div>
      </div>
      <div class="stat-card red">
        <div class="s-label">🏆 Mayor contrato</div>
        <div class="s-val" id="stat-max">—</div>
        <div class="s-sub">valor más alto</div>
      </div>
    </div>

    <!-- Toolbar -->
    <div class="toolbar">
      <div class="tabs">
        <button class="tab active" id="tab-todas" onclick="setTab('todas')">Todas</button>
        <button class="tab" id="tab-urgentes" onclick="setTab('urgentes')">⚡ Urgentes</button>
        <button class="tab" id="tab-esta-semana" onclick="setTab('esta-semana')">Esta semana</button>
        <button class="tab" id="tab-grandes" onclick="setTab('grandes')">+100k €</button>
      </div>
      <div class="tb-sep"></div>
      <select class="tb-select" id="filter-cpv" onchange="cargarTodo()">
        <option value="">Todos los CPVs</option>
        <option value="79340000">Publicidad y marketing</option>
        <option value="79341000">Publicidad</option>
        <option value="79342000">Marketing</option>
        <option value="79341200">Gestión publicidad</option>
        <option value="79341400">Campañas</option>
        <option value="79342200">Promoción</option>
        <option value="79341100">Colocación publicidad</option>
        <option value="92210000">Radio</option>
        <option value="92220000">Televisión</option>
        <option value="79822500">Diseño gráfico</option>
        <option value="74812240">Vídeos publicidad</option>
        <option value="92111250">Vídeos información</option>
        <option value="79800000">Impresión</option>
        <option value="72413000">Diseño web</option>
        <option value="72415000">Alojamiento web</option>
        <option value="79413000">Consultoría marketing</option>
        <option value="79416000">Relaciones públicas</option>
        <option value="79416200">Asesoramiento RRPP</option>
        <option value="79952000">Organización eventos</option>
        <option value="79956000">Ferias y exposiciones</option>
      </select>
      <select class="tb-select" id="filter-min" onchange="cargarTodo()">
        <option value="0">Cualquier importe</option>
        <option value="10000">+10.000 €</option>
        <option value="50000">+50.000 €</option>
        <option value="100000">+100.000 €</option>
        <option value="500000">+500.000 €</option>
      </select>
      <div class="count-pill" id="count-pill">0 resultados</div>
    </div>

    <!-- Cards -->
    <div class="cards-area">
      <div class="card-grid" id="card-grid">
        <div class="empty-state"><div class="e-icon">⏳</div><p>Cargando licitaciones…</p></div>
      </div>
    </div>

  </div><!-- /main -->
</div><!-- /app -->

<!-- Modal detalle -->
<div class="modal-bg" id="modal-det" onclick="if(event.target===this)this.classList.remove('open')">
  <div class="modal">
    <div class="modal-head">
      <div class="modal-head-icon">📋</div>
      <h2 id="m-titulo">—</h2>
      <button class="modal-close" onclick="document.getElementById('modal-det').classList.remove('open')">✕</button>
    </div>
    <div class="modal-body">
      <div class="mf"><label>Órgano contratante</label><p id="m-organo">—</p></div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px;">
        <div class="mf"><label>Importe estimado</label><p class="mf-val green" id="m-importe" style="color:#059669">—</p></div>
        <div class="mf"><label>Plazo presentación</label><p class="mf-val" id="m-plazo">—</p></div>
      </div>
      <div class="mf"><label>CPVs</label><p id="m-cpvs" style="white-space:pre-line;font-size:12px;color:#555;">—</p></div>
      <div class="mf"><label>Detectada el</label><p id="m-fecha">—</p></div>
    </div>
    <div class="modal-footer">
      <a id="m-enlace" class="btn-link" target="_blank">🔗 Ver en PLACSP</a>
    </div>
  </div>
</div>

<!-- Modal análisis Grok -->
<div class="modal-bg" id="modal-analisis" onclick="if(event.target===this)this.classList.remove('open')">
  <div class="modal modal-analisis">
    <div class="modal-head">
      <div class="modal-head-icon" style="background:#F0F0F0;color:#111;font-size:16px;">✦</div>
      <div style="flex:1;min-width:0;">
        <div style="font-size:10px;font-weight:600;color:#999;text-transform:uppercase;letter-spacing:.6px;margin-bottom:2px;">Análisis Grok</div>
        <h2 id="ma-titulo" style="font-size:13px;color:#555;font-weight:400;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">—</h2>
      </div>
      <button class="modal-close" onclick="document.getElementById('modal-analisis').classList.remove('open')">✕</button>
    </div>
    <div class="modal-body" id="ma-body">
      <div class="analysis-loading"><div class="big-spin"></div><p>Analizando con Grok…</p></div>
    </div>
    <div class="modal-footer" style="display:flex;gap:8px;align-items:center;" id="ma-footer">
      <a id="ma-enlace" class="btn-link" target="_blank" style="display:none;">🔗 Ver en PLACSP</a>
      <button onclick="document.getElementById('modal-analisis').classList.remove('open')" style="margin-left:auto;background:#fff;border:1px solid #EBEBEB;border-radius:8px;padding:8px 16px;font-size:13px;cursor:pointer;">Cerrar</button>
    </div>
  </div>
</div>

<!-- Modal log -->
<div class="modal-bg" id="modal-log" onclick="if(event.target===this)this.classList.remove('open')">
  <div class="modal" style="max-width:720px">
    <div class="modal-head">
      <div class="modal-head-icon">📄</div>
      <h2>Resultado última ejecución</h2>
      <button class="modal-close" onclick="document.getElementById('modal-log').classList.remove('open')">✕</button>
    </div>
    <div class="modal-body">
      <pre class="log-pre" id="log-content">Sin datos aún. Pulsa "Buscar ahora" para ejecutar el agente.</pre>
    </div>
  </div>
</div>

<!-- Modal config -->
<div class="modal-bg" id="modal-config" onclick="if(event.target===this)this.classList.remove('open')">
  <div class="modal" style="max-width:400px">
    <div class="modal-head">
      <div class="modal-head-icon">⚙️</div>
      <h2>Configuración</h2>
      <button class="modal-close" onclick="document.getElementById('modal-config').classList.remove('open')">✕</button>
    </div>
    <div class="modal-body">
      <div class="mf"><label>Estado del agente</label><p id="cfg-estado">—</p></div>
      <div class="mf"><label>Licitaciones en caché</label><p id="cfg-cache">—</p></div>
      <div class="mf" style="margin-top:16px;padding-top:16px;border-top:1px solid #F0F0F0;">
        <label>Filtros activos</label>
        <p style="font-size:12px;color:#555;margin-top:4px;">Solo activas y relevantes para IMAGINE. Puedes desactivarlos usando los parámetros de la URL: <code style="background:#F5F5F5;padding:2px 5px;border-radius:4px;">/api/licitaciones?activas=0&relevantes=0</code></p>
      </div>
    </div>
  </div>
</div>

<!-- Toast -->
<div class="toast" id="toast"></div>

<script>
const CPV_NAMES = {
  "79340000":"Publicidad y marketing","79341000":"Publicidad","79342000":"Marketing",
  "79341200":"Gestión publicidad","79341400":"Campañas","79342200":"Promoción",
  "79341100":"Colocación publicidad","92210000":"Radio","92220000":"Televisión",
  "79822500":"Diseño gráfico","74812240":"Vídeos publicidad","92111250":"Vídeos info",
  "79800000":"Impresión","72413000":"Diseño web","72415000":"Alojamiento web",
  "79413000":"Consultoría marketing","79416000":"Relaciones públicas","79416200":"Asesoramiento RRPP",
  "79952000":"Organización eventos","79956000":"Ferias y exposiciones",
  "79950000":"Eventos (gen.)","79955000":"Desfiles","92111200":"Producción audiovisual",
  "92111210":"Producción vídeo","92111220":"Audiovisual"
};

let allLics = [];
let activeTab = 'todas';

function fmt(n) {
  if (!n || n == 0) return 'N/D';
  return new Intl.NumberFormat('es-ES',{style:'currency',currency:'EUR',maximumFractionDigits:0}).format(n);
}

function fmtDate(d) {
  if (!d || d==='N/D') return 'N/D';
  try { return new Date(d).toLocaleDateString('es-ES',{day:'2-digit',month:'short',year:'numeric'}); }
  catch { return d; }
}

function plazoDays(d) {
  if (!d || d==='N/D') return null;
  try { return Math.ceil((new Date(d) - new Date()) / 86400000); }
  catch { return null; }
}

function dueChip(d) {
  const days = plazoDays(d);
  if (days === null) return '';
  if (days < 0)   return `<span class="due-chip lejano">Cerrado</span>`;
  if (days <= 7)  return `<span class="due-chip urgente">⚡ Vence en ${days}d</span>`;
  if (days <= 14) return `<span class="due-chip pronto">⏳ Vence en ${days}d</span>`;
  if (days <= 30) return `<span class="due-chip ok">📅 ${fmtDate(d)}</span>`;
  return `<span class="due-chip lejano">📅 ${fmtDate(d)}</span>`;
}

function renderCard(l) {
  const imp = parseFloat(l.importe) || 0;
  const cpvs = [...new Set((l.cpvs||[]).map(c=>c.slice(0,8)))]
    .filter(c => CPV_NAMES[c]).slice(0,2)
    .map(c => `<span class="cpv-chip">${CPV_NAMES[c]}</span>`).join('');
  const impHtml = imp > 0
    ? `<span class="lc-importe">${fmt(imp)}</span>`
    : `<span class="lc-importe nd">N/D</span>`;

  const ld = JSON.stringify(l).replace(/"/g,'&quot;');
  return `<div class="lic-card">
    <div class="lc-top" onclick="verDetalle(${ld})" style="cursor:pointer;">
      <div class="lc-due">${dueChip(l.plazo)}<span class="lc-menu">···</span></div>
      <div class="lc-title">${l.titulo||'Sin título'}</div>
      <div class="lc-organo">${l.organo||'Órgano desconocido'}</div>
    </div>
    <div class="lc-bottom">
      <div class="lc-cpvs">${cpvs||'<span class="cpv-chip" style="background:#F5F5F5;color:#999;">Sin CPV</span>'}</div>
      ${impHtml}
    </div>
    <div class="lc-actions">
      <button class="btn-analizar" id="btn-a-${l._id?.slice(-8)||Math.random().toString(36).slice(2)}" onclick="analizarLic(${ld}, this)">
        <span class="a-label">✦ Analizar con Grok</span>
        <div class="a-spin"></div>
      </button>
      ${l.enlace ? `<button class="btn-ver" onclick="window.open('${l.enlace}','_blank')">🔗 PLACSP</button>` : ''}
    </div>
  </div>`;
}

function applyTab(lics) {
  const today = new Date();
  if (activeTab === 'urgentes') return lics.filter(l => { const d=plazoDays(l.plazo); return d!==null && d>=0 && d<=7; });
  if (activeTab === 'esta-semana') return lics.filter(l => { const d=plazoDays(l.plazo); return d!==null && d>=0 && d<=14; });
  if (activeTab === 'grandes') return lics.filter(l => (parseFloat(l.importe)||0) >= 100000);
  return lics;
}

function renderGrid(lics) {
  const grid = document.getElementById('card-grid');
  const shown = applyTab(lics);
  document.getElementById('count-pill').innerHTML = `<strong>${shown.length}</strong> resultado${shown.length!==1?'s':''}`;
  if (shown.length === 0) {
    grid.innerHTML = '<div class="empty-state" style="grid-column:1/-1"><div class="e-icon">🔍</div><p>No hay licitaciones con ese filtro</p></div>';
    return;
  }
  grid.innerHTML = shown.map(renderCard).join('');
}

function setTab(t) {
  activeTab = t;
  document.querySelectorAll('.tab').forEach(el => el.classList.remove('active'));
  const el = document.getElementById('tab-'+t);
  if (el) el.classList.add('active');
  document.querySelectorAll('.sb-item').forEach(el => el.classList.remove('active'));
  const titles = {todas:'Licitaciones activas',urgentes:'Urgentes (≤7 días)','esta-semana':'Esta semana (≤14 días)',grandes:'+100.000 €'};
  document.getElementById('page-title').textContent = titles[t] || 'Licitaciones';
  document.getElementById('bc-current').textContent = titles[t] || 'Licitaciones';
  renderGrid(allLics);
}

function setCpv(cpv) {
  document.getElementById('filter-cpv').value = cpv;
  cargarTodo();
}

function buscar() { cargarTodo(); }

async function cargarTodo() {
  const q   = document.getElementById('search').value;
  const cpv = document.getElementById('filter-cpv').value;
  const min = document.getElementById('filter-min').value;
  let params = `activas=1&relevantes=1`;
  if (q)   params += `&q=${encodeURIComponent(q)}`;
  if (cpv) params += `&cpv=${cpv}`;
  if (min && min!='0') params += `&min=${min}`;

  const r = await fetch(`/api/licitaciones?${params}`);
  const d = await r.json();
  allLics = d.licitaciones;

  const urgentes    = allLics.filter(l => { const d=plazoDays(l.plazo); return d!==null&&d>=0&&d<=7; });
  const semana      = allLics.filter(l => { const d=plazoDays(l.plazo); return d!==null&&d>=0&&d<=14; });
  const grandes     = allLics.filter(l => (parseFloat(l.importe)||0)>=100000);

  renderGrid(allLics);
  cargarStats();
}

async function cargarStats() {
  const r = await fetch('/api/stats');
  const d = await r.json();
  document.getElementById('stat-total').textContent   = d.total;
  document.getElementById('stat-importe').textContent = fmt(d.importe_total);
  document.getElementById('stat-medio').textContent   = fmt(d.importe_medio);
  document.getElementById('stat-max').textContent     = fmt(d.importe_max);
  document.getElementById('stat-update').textContent  = d.actualizado
    ? 'Últ: ' + new Date(d.actualizado).toLocaleString('es-ES',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'})
    : '—';
}

function formatAnalysis(text) {
  const lines = text.split('\n');
  let html = '<div class="analysis-body">';
  let inUl = false;

  for (let line of lines) {
    line = line.trim();
    if (!line) { if (inUl) { html += '</ul>'; inUl = false; } continue; }

    if (/^(🎯|📋|⚡|💡|✅|🚀)\s/.test(line)) {
      if (inUl) { html += '</ul>'; inUl = false; }
      const emoji = line.match(/^(🎯|📋|⚡|💡|✅|🚀)/)[0];
      const rest  = line.replace(/^(🎯|📋|⚡|💡|✅|🚀)\s*\*?\*?/, '').replace(/\*\*$/, '').trim();
      html += `<h3>${emoji} ${rest}</h3>`;
    } else if (/^\*\*/.test(line) && line.endsWith('**')) {
      if (inUl) { html += '</ul>'; inUl = false; }
      html += `<h3>${line.replace(/\*\*/g,'')}</h3>`;
    } else if (/^[-•*]\s/.test(line)) {
      if (!inUl) { html += '<ul>'; inUl = true; }
      const content = line.replace(/^[-•*]\s/,'').replace(/\*\*(.*?)\*\*/g,'<strong>$1</strong>');
      html += `<li>${content}</li>`;
    } else if (/^\d+\./.test(line)) {
      if (!inUl) { html += '<ul>'; inUl = true; }
      const content = line.replace(/^\d+\.\s*/,'').replace(/\*\*(.*?)\*\*/g,'<strong>$1</strong>');
      html += `<li>${content}</li>`;
    } else {
      if (inUl) { html += '</ul>'; inUl = false; }
      const content = line.replace(/\*\*(.*?)\*\*/g,'<strong>$1</strong>');
      html += `<p>${content}</p>`;
    }
  }
  if (inUl) html += '</ul>';
  html += '</div>';
  return html;
}

async function analizarLic(l, btn) {
  document.getElementById('ma-titulo').textContent = l.titulo || '—';
  const enlaceEl = document.getElementById('ma-enlace');
  enlaceEl.href = l.enlace || '#';
  enlaceEl.style.display = l.enlace ? 'inline-flex' : 'none';
  document.getElementById('ma-body').innerHTML =
    '<div class="analysis-loading"><div class="big-spin"></div><p>Analizando con Grok…</p></div>';
  document.getElementById('modal-analisis').classList.add('open');

  if (btn) { btn.disabled = true; btn.classList.add('loading'); }

  try {
    const r = await fetch('/api/analizar', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({
        titulo: l.titulo, organo: l.organo,
        importe: l.importe, plazo: l.plazo,
        cpvs: l.cpvs, enlace: l.enlace
      })
    });
    const d = await r.json();
    if (d.ok) {
      document.getElementById('ma-body').innerHTML = formatAnalysis(d.analysis);
    } else {
      const isApiKey = d.msg && d.msg.includes('API key');
      document.getElementById('ma-body').innerHTML = isApiKey
        ? `<div class="no-api-warn">
            <strong>API key no configurada.</strong><br>
            Abre <code>config.json</code> y añade tu clave de xAI en el campo <code>grok_api_key</code>.<br>
            Consíguela en <a href="https://console.x.ai" target="_blank">console.x.ai</a> (hay créditos gratuitos).
           </div>`
        : `<p style="color:#DC2626;font-size:13px;">Error: ${d.msg}</p>`;
    }
  } catch(e) {
    document.getElementById('ma-body').innerHTML = `<p style="color:#DC2626;font-size:13px;">Error de conexión: ${e}</p>`;
  }
  if (btn) { btn.disabled = false; btn.classList.remove('loading'); }
}

function verDetalle(l) {
  document.getElementById('m-titulo').textContent  = l.titulo||'—';
  document.getElementById('m-organo').textContent  = l.organo||'—';
  document.getElementById('m-importe').textContent = fmt(parseFloat(l.importe)||0);
  document.getElementById('m-plazo').textContent   = fmtDate(l.plazo) + (plazoDays(l.plazo)!==null ? ` (${plazoDays(l.plazo)} días)` : '');
  document.getElementById('m-fecha').textContent   = l.fecha_deteccion||'—';
  const cpvLines = [...new Set((l.cpvs||[]).map(c=>c.slice(0,8)))]
    .map(c => `${c}  ${CPV_NAMES[c]||''}`.trim()).join('\n');
  document.getElementById('m-cpvs').textContent = cpvLines||'—';
  document.getElementById('m-enlace').href = l.enlace||'#';
  document.getElementById('modal-det').classList.add('open');
}

function toast(msg, type='') {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = 'toast show ' + type;
  setTimeout(()=>el.className='toast', 4000);
}

async function ejecutarAgente() {
  const btn = document.getElementById('btn-run');
  btn.disabled = true;
  btn.classList.add('loading');
  toast('⏳ Buscando licitaciones, puede tardar 1-2 min…');
  try {
    const r = await fetch('/api/ejecutar',{method:'POST'});
    const d = await r.json();
    if (!d.ok && d.msg) { toast('❌ ' + d.msg, 'err'); btn.disabled=false; btn.classList.remove('loading'); return; }
    // polling hasta que termine
    let intentos = 0;
    const poll = setInterval(async () => {
      intentos++;
      const s = await fetch('/api/estado').then(x=>x.json());
      if (!s.agente_corriendo) {
        clearInterval(poll);
        btn.disabled = false;
        btn.classList.remove('loading');
        const res = s.ultimo_resultado || {};
        if (res.stdout) document.getElementById('log-content').textContent = res.stdout;
        if (res.ok) { toast('✅ Búsqueda completada', 'ok'); await cargarTodo(); }
        else { toast('❌ ' + (res.msg || res.stderr || 'Error al ejecutar'), 'err'); }
      } else if (intentos > 60) {
        clearInterval(poll);
        btn.disabled = false;
        btn.classList.remove('loading');
        toast('⚠️ Tiempo de espera agotado', 'err');
      }
    }, 3000);
  } catch(e) { toast('❌ Error de conexión','err'); btn.disabled=false; btn.classList.remove('loading'); }
}

async function cargarEstado() {
  const r = await fetch('/api/estado');
  const d = await r.json();
  document.getElementById('cfg-estado').textContent   = d.agente_corriendo ? 'En ejecución…' : 'Inactivo';
  document.getElementById('cfg-cache').textContent    = d.licitaciones_en_cache + ' IDs guardados';
}

cargarTodo();
cargarEstado();
</script>
</body>
</html>"""

@app.get("/")
def index():
    return render_template_string(HTML)


if __name__ == "__main__":
    print("\n  Agente Licitador — IMAGINE COMUNICACIÓN ANDALUZA S.L.U")
    print("  → http://localhost:5000\n")
    app.run(debug=False, port=5000)
