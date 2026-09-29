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
import email
import imaplib
import re
from email.header import decode_header
from html.parser import HTMLParser
from email.utils import parsedate_to_datetime

from flask import Flask, redirect, render_template, request, session, url_for

app = Flask(__name__)
app.secret_key = "cambia-esto-por-algo-aleatorio"

IMAP_HOST = "imap.gmail.com"

# Patrones de búsqueda en el cuerpo
PATRON_NUMERO_ERRORES = re.compile(
    r"n[uú]mero\s+de\s+errores\s*:?\s*\.?\s*(\d+)", re.IGNORECASE)
PATRON_ERRORES = re.compile(r"errores\s*:?\s*\.?\s*(\d+)", re.IGNORECASE)
PATRON_SIN_ERRORES = re.compile(r"sin\s+errores", re.IGNORECASE)


# --------------------------------------------------------------------------
# Utilidades de correo
# --------------------------------------------------------------------------
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

    Devuelve (resultados, estadisticas):
      resultados: lista de dicts {asunto, fecha, errores}
      estadisticas: dict con contadores para el resumen.
    """
    mail = imaplib.IMAP4_SSL(IMAP_HOST)
    try:
        mail.login(usuario, contrasena)
        mail.select("inbox")

        status, mensajes = mail.search(None, "UNSEEN")
        ids = mensajes[0].split() if mensajes and mensajes[0] else []

        resultados = []
        leidos = 0
        encontrados = 0

        for mail_id in ids:
            status, datos = mail.fetch(mail_id, "(RFC822)")
            for parte in datos:
                if not isinstance(parte, tuple) or len(parte) < 2:
                    continue
                msg = email.message_from_bytes(parte[1])

                asunto = decodificar_cabecera(msg.get("Subject")) or "(sin asunto)"
                fecha = msg.get("Date", "")
                try:
                    fecha = parsedate_to_datetime(fecha).strftime("%d/%m/%Y %H:%M")
                except Exception:
                    pass

                cuerpo = obtener_cuerpo(msg)
                hay_informe, errores = extraer_errores(cuerpo)

                if not hay_informe:
                    # No habla de errores -> se deja como no leído.
                    continue

                encontrados += 1
                resultados.append(
                    {"asunto": asunto, "fecha": fecha, "errores": errores})

                if errores == 0 or marcar_leidos_con_errores:
                    mail.store(mail_id, "+FLAGS", "\\Seen")
                    leidos += 1

        estadisticas = {
            "no_leidos": len(ids),
            "encontrados": encontrados,
            "leidos": leidos,
        }
        return resultados, estadisticas
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

    try:
        resultados, estadisticas = procesar_correo(
            usuario, contrasena, marcar_leidos_con_errores)
    except imaplib.IMAP4.error:
        return render_template(
            "index.html",
            error="Usuario o contraseña incorrectos. Recuerda usar una "
                  "contraseña de aplicación de Gmail (16 caracteres).")
    except Exception as exc:  # error de red, etc.
        return render_template(
            "index.html", error=f"No se pudo conectar: {exc}")

    # Guardamos las credenciales en la sesión (cookie firmada) para poder
    # repetir la revisión sin volver a escribirlas. No se guardan en disco.
    session["usuario"] = usuario
    session["contrasena"] = contrasena
    session["marcar_leidos_con_errores"] = marcar_leidos_con_errores

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
        stats=estadisticas,
        total_errores=total_errores,
        bien=bien,
        mal=mal,
        porc_bien=porc_bien,
        porc_mal=porc_mal,
    )


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


def _ejecutar_revision(usuario, contrasena, marcar_leidos_con_errores):
    """Ejecuta la revisión y pinta la página de resultados (o el error)."""
    try:
        resultados, estadisticas = procesar_correo(
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
        stats=estadisticas,
        total_errores=total_errores,
        bien=bien,
        mal=mal,
        porc_bien=porc_bien,
        porc_mal=porc_mal,
    )


if __name__ == "__main__":
    app.run(debug=True)