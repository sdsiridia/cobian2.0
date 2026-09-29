# Monitor de errores de respaldo — versión web

Versión web (Flask) del script `leer_archivos_cobian_determinar_errores_v3_2_1.py`.

## Qué hace

1. Pide **usuario y contraseña** para conectar a la cuenta de Gmail.
2. Lee los correos **no leídos** de la bandeja de entrada (IMAP).
3. En el cuerpo de cada correo busca **"Número de errores"** o **"Errores"**
   (también detecta *"El respaldo ha terminado sin errores"* = 0 errores).
4. Si **no** encuentra nada → el mensaje queda **como no leído**.
5. Si lo encuentra:
   - **0 errores** → se marca como **leído**.
   - **distinto de 0** → se deja como **no leído** (igual que tu script de
     Python). Hay una casilla en el formulario para cambiar este
     comportamiento si prefieres marcarlos todos como leídos.
6. Muestra una **tabla** con el **Origen (Asunto)** y la **cantidad de
   errores**, con colores: verde = 0, amarillo = 1–3, rojo = más de 3,
   más un resumen y el gráfico de barras "Bien / Mal" de tu script.

## ⚠️ Requisito importante: contraseña de aplicación

Gmail **ya no acepta la contraseña normal de la cuenta** para IMAP.
Para que funcione:

1. Activa la **verificación en dos pasos** en tu cuenta de Google:
   <https://myaccount.google.com/security>
2. Crea una **contraseña de aplicación** de 16 caracteres:
   <https://myaccount.google.com/apppasswords>
3. Usa esa contraseña de 16 caracteres en el formulario (no la normal).
4. Verifica que **IMAP esté activado** en Gmail:
   Gmail → ⚙️ Configuración → *Reenvío y correo POP/IMAP* → *Habilitar IMAP*.

## Instalación y ejecución

```bash
pip install -r requirements.txt
python app.py
```

Abre <http://localhost:5000> en el navegador.

## Estructura

```
monitor-errores-gmail/
├── app.py                     # Servidor Flask + lógica IMAP
├── requirements.txt
├── templates/
│   ├── index.html             # Formulario de acceso
│   └── resultados.html        # Tabla y resumen de errores
└── static/
    └── estilos.css
```

## Seguridad

Las credenciales **no se guardan** en disco, sesiones ni logs: se usan solo
para la conexión IMAP de esa petición y luego la conexión se cierra.
