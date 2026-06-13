#!/usr/bin/env python3
"""
Agente Licitador — IMAGINE COMUNICACIÓN ANDALUZA S.L.U
Monitoriza licitaciones públicas en PLACSP (feed ATOM oficial) por CPVs.
"""

import json
import smtplib
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

# ── Configuración ────────────────────────────────────────────────────────────

EMPRESA = "IMAGINE COMUNICACIÓN ANDALUZA S.L.U"
EMAIL_DESTINO = "vrdesign87@gmail.com"

CPVS = {
    "79340000": "Publicidad y marketing",
    "79341000": "Publicidad",
    "79342000": "Marketing",
    "79341200": "Gestión de publicidad",
    "79341400": "Campañas de publicidad",
    "79342200": "Promoción",
    "79341100": "Colocación de publicidad",
    "92210000": "Servicios de radio",
    "92220000": "Servicios de televisión",
    "79822500": "Diseño gráfico",
    "74812240": "Producción vídeos publicidad",
    "92111250": "Producción vídeos información",
    "79800000": "Impresión",
    "72413000": "Diseño web",
    "72415000": "Alojamiento web",
    "79413000": "Consultoría marketing",
    "79416000": "Relaciones públicas",
    "79416200": "Asesoramiento RRPP",
    "79952000": "Organización de eventos",
    "79956000": "Ferias y exposiciones",
}

# Feed ATOM oficial de PLACSP — licitaciones en plazo (actualización diaria)
# Formato: página 1..N, cada página hasta 500 entradas
PLACSP_FEED_BASE = (
    "https://contrataciondelsectorpublico.gob.es"
    "/sindicacion/sindicacion_643/licitacionesPerfilesContratanteCompleto3.atom"
)

CACHE_FILE    = Path(__file__).parent / "licitaciones_vistas.json"
RESULTS_FILE  = Path(__file__).parent / "resultados.json"

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "cac":  "urn:dgpe:names:draft:codice:schema:xsd:CommonAggregateComponents-2",
    "cbc":  "urn:dgpe:names:draft:codice:schema:xsd:CommonBasicComponents-2",
    "con":  "urn:dgpe:names:draft:codice-place-ext:schema:xsd:ContractFolderStatus-2",
}

# ── Cache ─────────────────────────────────────────────────────────────────────

def cargar_cache() -> set:
    if CACHE_FILE.exists():
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        return set(data.get("ids", []))
    return set()


def guardar_cache(ids: set):
    CACHE_FILE.write_text(
        json.dumps({"ids": list(ids), "actualizado": str(date.today())},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

# ── Parseo del feed ATOM ──────────────────────────────────────────────────────

def _texto(el, path: str) -> str:
    node = el.find(path, NS)
    return (node.text or "").strip() if node is not None else ""


def parsear_entrada(entry) -> dict | None:
    """Extrae campos relevantes de una entrada ATOM de PLACSP."""
    lid = _texto(entry, "atom:id") or _texto(entry, "atom:title")
    titulo = _texto(entry, "atom:title")
    enlace_node = entry.find("atom:link[@rel='alternate']", NS) or entry.find("atom:link", NS)
    enlace = enlace_node.get("href", "") if enlace_node is not None else ""

    # CPV (puede haber varios)
    cpvs_doc = [
        n.text.strip()[:8]
        for n in entry.findall(".//cbc:ItemClassificationCode", NS)
        if n.text
    ]

    importe = _texto(entry, ".//cbc:TaxExclusiveAmount")
    plazo   = _texto(entry, ".//cbc:EndDate")
    organo  = _texto(entry, ".//cac:PartyName/cbc:Name")

    return {
        "_id":    lid,
        "titulo": titulo,
        "organo": organo,
        "cpvs":   cpvs_doc,
        "importe": importe,
        "plazo":   plazo,
        "enlace":  enlace,
    }


def obtener_pagina(url: str) -> tuple[list[dict], str | None]:
    """Descarga una página del feed y devuelve (entradas, url_siguiente)."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "AgenteImagine/2.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            root = ET.fromstring(resp.read())
    except Exception as e:
        print(f"  [WARN] {e}")
        return [], None

    entradas = []
    for entry in root.findall("atom:entry", NS):
        parsed = parsear_entrada(entry)
        if parsed:
            entradas.append(parsed)

    # URL siguiente (paginación)
    siguiente = None
    for link in root.findall("atom:link", NS):
        if link.get("rel") == "next":
            siguiente = link.get("href")
            break

    return entradas, siguiente


def es_relevante(entrada: dict) -> bool:
    """True si algún CPV del documento coincide con los de IMAGINE."""
    for cpv in entrada.get("cpvs", []):
        cpv8 = cpv[:8]
        if cpv8 in CPVS:
            return True
    return False

# ── Formateo y email ──────────────────────────────────────────────────────────

def formatear(l: dict) -> str:
    cpv_desc = ", ".join(
        f"{c} ({CPVS.get(c[:8], '')})" for c in l.get("cpvs", []) if c[:8] in CPVS
    )
    lines = [
        f"  Título  : {l['titulo']}",
        f"  Órgano  : {l['organo'] or 'N/D'}",
        f"  CPV     : {cpv_desc or ', '.join(l.get('cpvs', []))}",
        f"  Importe : {l['importe'] or 'N/D'} €",
        f"  Plazo   : {l['plazo'] or 'N/D'}",
    ]
    if l["enlace"]:
        lines.append(f"  Enlace  : {l['enlace']}")
    return "\n".join(lines)


def enviar_email(licitaciones: list[dict], config: dict):
    smtp_host = config.get("smtp_host", "")
    smtp_user = config.get("smtp_user", "")
    smtp_pass = config.get("smtp_pass", "")
    if not all([smtp_host, smtp_user, smtp_pass]):
        print("[INFO] SMTP no configurado — sin envío de email.")
        return

    cuerpo = f"NUEVAS LICITACIONES — {EMPRESA}\n"
    cuerpo += f"Fecha: {datetime.now().strftime('%d/%m/%Y %H:%M')}\n"
    cuerpo += "=" * 60 + "\n\n"
    for l in licitaciones:
        cuerpo += formatear(l) + "\n\n"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[LICITADOR] {len(licitaciones)} nuevas licitaciones — {date.today()}"
    msg["From"]    = smtp_user
    msg["To"]      = EMAIL_DESTINO
    msg.attach(MIMEText(cuerpo, "plain", "utf-8"))

    try:
        with smtplib.SMTP_SSL(smtp_host, 465) as s:
            s.login(smtp_user, smtp_pass)
            s.sendmail(smtp_user, EMAIL_DESTINO, msg.as_string())
        print(f"[OK] Email enviado a {EMAIL_DESTINO}")
    except Exception as e:
        print(f"[ERROR] Email: {e}")


def guardar_resultados(nuevas: list[dict]):
    """Añade las nuevas licitaciones al historial completo de resultados."""
    existentes = []
    if RESULTS_FILE.exists():
        existentes = json.loads(RESULTS_FILE.read_text(encoding="utf-8")).get("licitaciones", [])
    ids_existentes = {l["_id"] for l in existentes}
    for l in nuevas:
        if l["_id"] not in ids_existentes:
            l["fecha_deteccion"] = str(date.today())
            existentes.insert(0, l)
    RESULTS_FILE.write_text(
        json.dumps({"licitaciones": existentes, "actualizado": str(datetime.now())},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def cargar_config() -> dict:
    cfg = Path(__file__).parent / "config.json"
    return json.loads(cfg.read_text(encoding="utf-8")) if cfg.exists() else {}

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"\n{'='*60}")
    print(f"  AGENTE LICITADOR — {EMPRESA}")
    print(f"  {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}")
    print(f"{'='*60}\n")

    config  = cargar_config()
    cache   = cargar_cache()
    nuevas  = []
    paginas = 0
    url     = PLACSP_FEED_BASE

    print("Descargando feed PLACSP", end="", flush=True)
    while url:
        entradas, url = obtener_pagina(url)
        paginas += 1
        print(".", end="", flush=True)

        for e in entradas:
            if e["_id"] in cache:
                continue
            if es_relevante(e):
                nuevas.append(e)
                cache.add(e["_id"])

        # Máx 3 páginas por ejecución (1 500 entradas, ~20s)
        if paginas >= 3:
            break

    print(f" {paginas} página(s) procesada(s)\n")

    print(f"{'─'*60}")
    print(f"LICITACIONES NUEVAS RELEVANTES: {len(nuevas)}")
    print(f"{'─'*60}\n")

    if nuevas:
        for l in nuevas:
            print(formatear(l))
            print()
        guardar_cache(cache)
        guardar_resultados(nuevas)
        enviar_email(nuevas, config)
    else:
        print("No hay licitaciones nuevas para IMAGINE.")

    print(f"\n[OK] Finalizado — {datetime.now().strftime('%H:%M:%S')}\n")


if __name__ == "__main__":
    main()
