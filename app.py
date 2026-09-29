"""
Monitor de errores de respaldo (Cobian) vía Gmail — versión web.

Flujo:
 1. El usuario inicia sesión con su correo y contraseña de aplicación de Gmail.
 2. Se leen los correos NO LEÍDOS de la bandeja de entrada (IMAP).
 3. En el cuerpo se busca "Número de errores" o "Errores" (y "sin errores" = 0).
 4. - Si NO se encuentra nada  -> el mensaje queda como no leído.
   - Si se encuentra y hay 0 errores   -> se marca como leído.
   - Si se encuentra y hay >0 errores  -> se deja como no leído (configurable).
 5. Se muestra una tabla: Origen (Asunto) | Cantidad de errores.

Nota de seguridad: las credenciales NO se guardan en disco ni en sesión;
se usan solo para la conexión IMAP de esa petición.
"""
import csv
import email
import imaplib
import io
import json
import os
import re
import unicodedata
from datetime import datetime
from email.header import decode_header
from html.parser import HTMLParser
from email.utils import parsedate_to_datetime

from flask import Flask, redirect, render_template, request, send_file, session, url_for

app = Flask(__name__)
app.secret_key = "cambia-esto-por-algo-aleatorio"

IMAP_HOST = "imap.gmail.com"

# Archivo donde se guarda el historial de todas las revisiones
HISTORIAL_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "historial.json")

# Patrones de búsqueda en el cuerpo
PATRON_NUMERO_ERRORES = re.compile(
    r"n[uú]mero\s+de\s+errores\s*:?\s*\.?\s*(\d+)", re.IGNORECASE)
PATRON_ERRORES = re.compile(r"errores\s*:?\s*\.?\s*(\d+)", re.IGNORECASE)
PATRON_SIN_ERRORES = re.compile(r"sin\s+errores", re.IGNORECASE)


# --------------------------------------------------------------------------
# Utilidades de correo
# --------------------------------------------------------------------------
def sin_tildes(texto):
    """Minúsculas sin tildes, para comparar textos sin preocuparse de acentos."""
    normalizado = unicodedata.normalize("NFD", texto or "")
    return "".join(c for c in normalizado if not unicodedata.combining(c)).lower()


def texto_normalizado(texto):
    """Minúsculas, sin tildes y con espacios/saltos de línea colapsados.

    Así una frase partida en varias líneas o con espacios dobles se detecta
    igual que escrita en una sola línea.
    """
    return re.sub(r"\s+", " ", sin_tildes(texto)).strip()


# Frase de éxito de Acronis, tolerante a variantes:
#   "La operación se ha efectuado correctamente"
#   "La operación se ha completado correctamente"
#   "La operación se ha realizado correctamente"
#   "... con éxito"
PATRON_OK_ACRONIS = re.compile(
    r"la\s+operaci[oó]n\s+se\s+ha\s+"
    r"(?:efectuado|completado|realizado|ejecutado)\s+"
    r"(?:correctamente|con\s+[eé]xito\b|sin\s+errores\b)",
    re.IGNORECASE)


def decodificar_cabecera(valor):
    """Decodifica una cabecera MIME (ej: el Asunto)."""
    if not valor:
        return ""
    partes = decode_header(valor)
    texto = ""
    for dato, codificacion in partes:
        if isinstance(dato, bytes):
            texto += dato.decode(codificacion or "utf-8", errors="replace")
        else:
            texto += dato
    return texto.strip()


class _QuitaHTML(HTMLParser):
    """Convierte HTML en texto plano para poder buscar en él."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.texto = []

    def handle_data(self, data):
        self.texto.append(data)

    def handle_br(self):  # <br>
        self.texto.append("\n")

    def get_text(self):
        return "".join(self.texto)


def html_a_texto(contenido_html):
    quitador = _QuitaHTML()
    try:
        quitador.feed(contenido_html)
    except Exception:
        return contenido_html
    return quitador.get_text()


def obtener_cuerpo(msg):
    """Devuelve el cuerpo del mensaje como texto plano."""
    cuerpo = ""
    if msg.is_multipart():
        # Primero buscamos text/plain; si no hay, usamos text/html.
        for tipo in ("text/plain", "text/html"):
            for parte in msg.walk():
                if parte.get_content_type() == tipo:
                    try:
                        dato = parte.get_payload(decode=True)
                        charset = parte.get_content_charset() or "utf-8"
                        texto = dato.decode(charset, errors="replace")
                    except Exception:
                        continue
                    if tipo == "text/html":
                        texto = html_a_texto(texto)
                    cuerpo = texto
                    break
            if cuerpo:
                break
    else:
        dato = msg.get_payload(decode=True)
        charset = msg.get_content_charset() or "utf-8"
        cuerpo = dato.decode(charset, errors="replace")
        if msg.get_content_type() == "text/html":
            cuerpo = html_a_texto(cuerpo)
    return cuerpo


def extraer_errores(cuerpo):
    """
    Busca la cantidad de errores en el cuerpo.

    Devuelve (encontrado, cantidad):
      - encontrado: True si el cuerpo habla de errores ("Número de errores",
        "Errores:" o "sin errores").
      - cantidad: el número de errores (0 si no hay errores).
    """
    m = PATRON_NUMERO_ERRORES.search(cuerpo)
    if m:
        return True, int(m.group(1))
    m = PATRON_ERRORES.search(cuerpo)
    if m:
        return True, int(m.group(1))
    if PATRON_SIN_ERRORES.search(cuerpo):
        return True, 0
    return False, None


# --------------------------------------------------------------------------
# Procesamiento IMAP
# --------------------------------------------------------------------------
def procesar_correo(usuario, contrasena, marcar_leidos_con_errores=False):
    """
    Se conecta a Gmail y procesa los correos no leídos.

    Devuelve (resultados, acronis, estadisticas):
        resultados: Cobian -> lista de dicts {asunto, fecha, errores}
        acronis:    Acronis -> lista de dicts {asunto, fecha, correcto}
        estadisticas: dict con contadores para el resumen.
    """
    mail = imaplib.IMAP4_SSL(IMAP_HOST)
    try:
        mail.login(usuario, contrasena)
        mail.select("inbox")

        status, mensajes = mail.search(None, "UNSEEN")
        ids = mensajes[0].split() if mensajes and mensajes[0] else []

        resultados = []      # informes Cobian
        acronis = []         # notificaciones Acronis True Image
        leidos = 0
        encontrados = 0
        acronis_ok = 0
        acronis_error = 0

        for mail_id in ids:
            # BODY.PEEK[] descarga el mensaje SIN marcarlo como leído
            # (RFC822 a secas sí lo marcaría automáticamente).
            status, datos = mail.fetch(mail_id, "(BODY.PEEK[])")
            for parte in datos:
                if not isinstance(parte, tuple) or len(parte) < 2:
                    continue
                msg = email.message_from_bytes(parte[1])

                asunto = decodificar_cabecera(
                    msg.get("Subject")) or "(sin asunto)"
                fecha = msg.get("Date", "")
                try:
                    fecha = parsedate_to_datetime(
                        fecha).strftime("%d/%m/%Y %H:%M")
                except Exception:
                    pass

                cuerpo = obtener_cuerpo(msg)

                # ===== Notificaciones de Acronis True Image =====
                # Se detecta por el asunto o por el cuerpo del mensaje.
                cuerpo_norm = texto_normalizado(cuerpo)
                es_acronis = (
                    "acronis" in sin_tildes(asunto)
                    or "true image" in sin_tildes(asunto)
                    or "acronis" in cuerpo_norm)

                if es_acronis:
                    correcto = bool(PATRON_OK_ACRONIS.search(cuerpo_norm))
                    print(f"[ACRONIS] {asunto!r} -> "
                          f"{'OK' if correcto else 'ERROR'}")
                    acronis.append(
                        {"asunto": asunto, "fecha": fecha,
                         "correcto": correcto})
                    if correcto:
                        acronis_ok += 1
                        mail.store(mail_id, "+FLAGS", "\\Seen")
                        leidos += 1
                    else:
                        # Dice otra cosa -> error, queda como no leído.
                        acronis_error += 1
                        mail.store(mail_id, "-FLAGS", "\\Seen")
                    continue

                hay_informe, errores = extraer_errores(cuerpo)

                if not hay_informe:
                    # No habla de errores -> se deja como no leído.
                    # Quitamos \Seen por si el servidor lo hubiera marcado.
                    mail.store(mail_id, "-FLAGS", "\\Seen")
                    continue

                encontrados += 1
                resultados.append(
                    {"asunto": asunto, "fecha": fecha, "errores": errores})

                if errores == 0 or marcar_leidos_con_errores:
                    mail.store(mail_id, "+FLAGS", "\\Seen")
                    leidos += 1
                else:
                    # Con errores -> se deja como no leído (por seguridad).
                    mail.store(mail_id, "-FLAGS", "\\Seen")

        estadisticas = {
            "no_leidos": len(ids),
            "encontrados": encontrados,
            "leidos": leidos,
            "acronis_ok": acronis_ok,
            "acronis_error": acronis_error,
        }
        return resultados, acronis, estadisticas
    finally:
        try:
            mail.close()
        except Exception:
            pass
        try:
            mail.logout()
        except Exception:
            pass


# --------------------------------------------------------------------------
# Historial (se guarda en historial.json)
# --------------------------------------------------------------------------
# Los asuntos suelen terminar con la fecha, ej:
#   "CRUZILA TRABAJOS Mon, 28 Sep 2026 17:31:17 +0200"
#   "ESTUDIO BACKUP Mon, 28 Sep 2026 18:47:05 -0300"
PATRON_FECHA_EN_ASUNTO = re.compile(
    r"\s*\b(?:lun|mar|mi[eé]|jue|vie|s[aá]b|dom|"
    r"mon|tue|wed|thu|fri|sat|sun)\b\s*,?\s*\d{1,2}\b.*$",
    re.IGNORECASE)


def derivar_empresa(asunto):
    """Deduce la "empresa" a partir del asunto.

    Los asuntos tienen la forma "NOMBRE_EMPRESA <fecha opcional>". Se quita
    la fecha del final (ej: "Mon, 28 Sep 2026 17:31:17 +0200") y lo que
    queda es la empresa. Si aún quedara un separador (" - ", "|", ":"), se
    toma la parte de la izquierda.
    """
    asunto = (asunto or "").strip()
    # Quitamos la fecha del final, si la hay
    asunto = PATRON_FECHA_EN_ASUNTO.sub("", asunto).strip(" -–:|,")
    # Por si acaso, separamos también por separadores comunes
    for separador in (" - ", "–", "|"):
        if separador in asunto:
            parte = asunto.split(separador)[0].strip()
            if parte:
                return parte
    return asunto or "(sin asunto)"


def cargar_historial():
    try:
        with open(HISTORIAL_PATH, encoding="utf-8") as archivo:
            return json.load(archivo)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def guardar_historial(resultados, acronis):
    """Añade los resultados de esta revisión al historial.json."""
    historial = cargar_historial()
    ahora = datetime.now().strftime("%Y-%m-%d %H:%M")
    for r in resultados:
        historial.append({
            "procesado": ahora,
            "fecha_correo": r["fecha"],
            "asunto": r["asunto"],
            "empresa": derivar_empresa(r["asunto"]),
            "errores": r["errores"],
            "tipo": "cobian",
        })
    for r in acronis:
        historial.append({
            "procesado": ahora,
            "fecha_correo": r["fecha"],
            "asunto": r["asunto"],
            "empresa": derivar_empresa(r["asunto"]),
            "errores": 0 if r["correcto"] else 1,
            "tipo": "acronis",
            "correcto": r["correcto"],
        })
    with open(HISTORIAL_PATH, "w", encoding="utf-8") as archivo:
        json.dump(historial, archivo, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------
# Rutas
# --------------------------------------------------------------------------
@app.route("/", methods=["GET"])
def inicio():
    return render_template("index.html", error=None)


@app.route("/conectar", methods=["POST"])
def conectar():
    usuario = (request.form.get("usuario") or "").strip()
    contrasena = request.form.get("contrasena") or ""
    marcar_leidos_con_errores = request.form.get(
        "marcar_leidos_con_errores") == "on"

    if not usuario or not contrasena:
        return render_template(
            "index.html",
            error="Escribe tu correo y tu contraseña de aplicación.")

    # Guardamos las credenciales en la sesión (cookie firmada) para poder
    # repetir la revisión sin volver a escribirlas. No se guardan en disco.
    session["usuario"] = usuario
    session["contrasena"] = contrasena
    session["marcar_leidos_con_errores"] = marcar_leidos_con_errores

    return _ejecutar_revision(usuario, contrasena, marcar_leidos_con_errores)


@app.route("/revisar", methods=["GET"])
def revisar():
    """Repite la revisión con las credenciales ya guardadas en la sesión."""
    if "usuario" not in session or "contrasena" not in session:
        return redirect(url_for("inicio"))
    return _ejecutar_revision(
        session["usuario"],
        session["contrasena"],
        session.get("marcar_leidos_con_errores", False),
    )


@app.route("/nueva", methods=["GET"])
def nueva():
    """Borra las credenciales de la sesión y vuelve al formulario."""
    session.clear()
    return redirect(url_for("inicio"))


CABECERAS_EXPORT = ["Fecha procesado", "Fecha correo", "Tipo",
                    "Empresa", "Asunto", "Errores", "Estado"]


def _filas_historial():
    """Convierte el historial en filas listas para exportar."""
    filas = []
    for r in cargar_historial():
        tipo = r.get("tipo", "cobian")
        if tipo == "acronis":
            estado = "Correcto" if r.get("correcto") else "Error"
        else:
            estado = ""
        filas.append([r.get("procesado", ""),
                      r.get("fecha_correo", ""),
                      tipo,
                      r.get("empresa", ""),
                      r.get("asunto", ""),
                      r.get("errores", 0),
                      estado])
    return filas


@app.route("/exportar/csv", methods=["GET"])
def exportar_csv():
    """Descarga el historial completo como CSV (compatible con Excel)."""
    salida = io.StringIO()
    escritor = csv.writer(salida, delimiter=";")
    escritor.writerow(CABECERAS_EXPORT)
    escritor.writerows(_filas_historial())

    # utf-8-sig (BOM) para que Excel muestre bien las tildes y ñ
    datos = io.BytesIO(salida.getvalue().encode("utf-8-sig"))
    return send_file(
        datos,
        mimetype="text/csv; charset=utf-8",
        as_attachment=True,
        download_name="historial_errores.csv",
    )


@app.route("/exportar/excel", methods=["GET"])
def exportar_excel():
    """Descarga el historial completo como Excel (.xlsx)."""
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill
    except ImportError:
        return render_template(
            "index.html",
            error="Para exportar a Excel instala openpyxl: "
            "pip install openpyxl (o usa el export CSV).")

    historial = cargar_historial()

    libro = Workbook()
    hoja = libro.active
    hoja.title = "Historial de errores"

    cabecera_verde = PatternFill("solid", fgColor="2E7D32")
    for columna, titulo in enumerate(CABECERAS_EXPORT, start=1):
        celda = hoja.cell(row=1, column=columna, value=titulo)
        celda.font = Font(bold=True, color="FFFFFF")
        celda.fill = cabecera_verde

    for fila, datos_fila in enumerate(_filas_historial(), start=2):
        for columna, valor in enumerate(datos_fila, start=1):
            hoja.cell(row=fila, column=columna, value=valor)

    # Anchos de columna orientativos
    for columna, ancho in zip("ABCDEFG", (18, 18, 10, 28, 40, 10, 12)):
        hoja.column_dimensions[columna].width = ancho
    hoja.freeze_panes = "A2"

    # ===== Hoja resumen: totales por día y por empresa =====
    resumen = libro.create_sheet("Resumen")

    def escribir_tabla(fila_inicio, titulo, clave, etiqueta):
        celda = resumen.cell(row=fila_inicio, column=1, value=titulo)
        celda.font = Font(bold=True, size=12)
        agregados = {}
        for r in historial:
            if r.get("tipo", "cobian") == "acronis":
                continue  # los Acronis van en su propia tabla
            k = r.get(clave, "(sin dato)") or "(sin dato)"
            agregados.setdefault(k, {"correos": 0, "errores": 0})
            agregados[k]["correos"] += 1
            agregados[k]["errores"] += r.get("errores", 0) or 0

        fila = fila_inicio + 1
        for columna, titulo_col in enumerate(
                (etiqueta, "Correos", "Errores"), start=1):
            c = resumen.cell(row=fila, column=columna, value=titulo_col)
            c.font = Font(bold=True, color="FFFFFF")
            c.fill = cabecera_verde
        for k in sorted(agregados):
            fila += 1
            datos = agregados[k]
            resumen.cell(row=fila, column=1, value=k)
            resumen.cell(row=fila, column=2, value=datos["correos"])
            c_err = resumen.cell(row=fila, column=3, value=datos["errores"])
            if datos["errores"] > 0:
                c_err.font = Font(bold=True, color="C62828")
        return fila

    ultima_fila = escribir_tabla(1, "Totales por día", "fecha_correo", "Día")
    ultima_fila = escribir_tabla(
        ultima_fila + 3, "Totales por empresa", "empresa", "Empresa")

    # ===== Tabla Acronis True Image en la hoja Resumen =====
    acronis = [r for r in historial if r.get("tipo") == "acronis"]
    if acronis:
        fila_ini = ultima_fila + 3
        celda = resumen.cell(row=fila_ini, column=1,
                             value="Acronis True Image")
        celda.font = Font(bold=True, size=12)
        fila = fila_ini + 1
        for columna, titulo_col in enumerate(
                ("Día", "Correctos", "Con error"), start=1):
            c = resumen.cell(row=fila, column=columna, value=titulo_col)
            c.font = Font(bold=True, color="FFFFFF")
            c.fill = cabecera_verde
        por_dia = {}
        for r in acronis:
            dia = r.get("fecha_correo", "(sin dato)") or "(sin dato)"
            por_dia.setdefault(dia, {"ok": 0, "mal": 0})
            if r.get("correcto"):
                por_dia[dia]["ok"] += 1
            else:
                por_dia[dia]["mal"] += 1
        for dia in sorted(por_dia):
            fila += 1
            resumen.cell(row=fila, column=1, value=dia)
            resumen.cell(row=fila, column=2, value=por_dia[dia]["ok"])
            c_mal = resumen.cell(row=fila, column=3, value=por_dia[dia]["mal"])
            if por_dia[dia]["mal"]:
                c_mal.font = Font(bold=True, color="C62828")

    for columna, ancho in zip("ABC", (32, 12, 12)):
        resumen.column_dimensions[columna].width = ancho

    buffer = io.BytesIO()
    libro.save(buffer)
    buffer.seek(0)
    return send_file(
        buffer,
        mimetype="application/vnd.openxmlformats-officedocument"
        ".spreadsheetml.sheet",
        as_attachment=True,
        download_name="historial_errores.xlsx",
    )


@app.route("/borrar_historial", methods=["GET"])
def borrar_historial():
    """Vacía el archivo de historial."""
    try:
        os.remove(HISTORIAL_PATH)
    except FileNotFoundError:
        pass
    if "usuario" in session and "contrasena" in session:
        return redirect(url_for("revisar"))
    return redirect(url_for("inicio"))


def _ejecutar_revision(usuario, contrasena, marcar_leidos_con_errores):
    """Ejecuta la revisión y pinta la página de resultados (o el error)."""
    try:
        resultados, acronis, estadisticas = procesar_correo(
            usuario, contrasena, marcar_leidos_con_errores)
    except imaplib.IMAP4.error:
        session.clear()
        return render_template(
            "index.html",
            error="Usuario o contraseña incorrectos. Recuerda usar una "
            "contraseña de aplicación de Gmail (16 caracteres).")
    except Exception as exc:  # error de red, etc.
        return render_template(
            "index.html", error=f"No se pudo conectar: {exc}")

    guardar_historial(resultados, acronis)

    acronis_ordenados = sorted(
        acronis, key=lambda r: (not r["correcto"], r["asunto"]))
    ordenados = sorted(resultados, key=lambda r: r["errores"], reverse=True)
    total_errores = sum(r["errores"] for r in resultados)
    bien = sum(1 for r in resultados if r["errores"] == 0)
    mal = len(resultados) - bien
    total = bien + mal
    porc_bien = round(bien / total * 100, 1) if total else 0.0
    porc_mal = round(mal / total * 100, 1) if total else 0.0

    return render_template(
        "resultados.html",
        resultados=ordenados,
        acronis=acronis_ordenados,
        stats=estadisticas,
        total_errores=total_errores,
        bien=bien,
        mal=mal,
        porc_bien=porc_bien,
        porc_mal=porc_mal,
        historial=cargar_historial(),
    )


if __name__ == "__main__":
    app.run(debug=True)
