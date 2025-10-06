from flask import Flask, render_template, request, redirect, url_for, flash, session
from flask_mysqldb import MySQL
from MySQLdb.cursors import DictCursor
import qrcode
import io
from flask import send_file

app = Flask(__name__)
app.secret_key = "clave_secreta_madi"

# Configuración MySQL (ajusta user/password si hace falta)
app.config['MYSQL_HOST'] = 'localhost'
app.config['MYSQL_USER'] = 'root'
app.config['MYSQL_PASSWORD'] = ''   # tu contraseña
app.config['MYSQL_DB'] = 'bdtest'
app.config['MYSQL_CURSORCLASS'] = 'DictCursor'

mysql = MySQL(app)

# ----------------------------------------------------
# RUTA: Inicio (bienvenida)
# ----------------------------------------------------
@app.route('/')
def index():
    return render_template('index.html')

# ----------------------------------------------------
# RUTA: iniciar test -> crea usuario y guarda id en session
# ----------------------------------------------------
@app.route('/start', methods=['GET', 'POST'])
def start_test():
    if request.method == 'POST':
        nombre = request.form.get('nombre').strip()
        correo = request.form.get('correo').strip()

        if not nombre or not correo:
            flash('Debes ingresar tu nombre y correo.', 'warning')
            return redirect(url_for('start_test'))

        cur = mysql.connection.cursor()
        # Verificar si ya existe un usuario con ese correo
        cur.execute("SELECT id_usuario, nombre FROM usuarios WHERE correo = %s", (correo,))
        usuario = cur.fetchone()

        if usuario:
            # Si ya existe, actualizamos el nombre por si lo cambió
            cur.execute("UPDATE usuarios SET nombre = %s WHERE id_usuario = %s", (nombre, usuario['id_usuario']))
            mysql.connection.commit()

            user_id = usuario['id_usuario']
            session['user_id'] = user_id
            session['user_name'] = nombre  # Usamos el nuevo nombre
            flash('Ya existe un usuario con ese correo. Continuarás con ese perfil.', 'info')
        else:
            # Insertar nuevo usuario
            cur.execute("INSERT INTO usuarios (nombre, correo) VALUES (%s, %s)", (nombre, correo))
            mysql.connection.commit()
            user_id = cur.lastrowid
            session['user_id'] = user_id
            session['user_name'] = nombre
            flash('Usuario registrado con éxito.', 'success')

        cur.close()
        return redirect(url_for('preguntas'))

    return render_template('start.html')

# ----------------------------------------------------
# RUTA: mostrar preguntas (GET) / procesar respuestas (POST)
# ----------------------------------------------------
@app.route('/preguntas', methods=['GET', 'POST'])
def preguntas():
    user_id = session.get('user_id')
    if not user_id:
        flash('Primero ingresa tus datos para iniciar el test.', 'info')
        return redirect(url_for('start_test'))

    cur = mysql.connection.cursor()
    # Traer todas las preguntas en orden aleatorio
    cur.execute("SELECT id_pregunta, texto FROM preguntas ORDER BY RAND()")
    preguntas = cur.fetchall()

    # Traer todas las respuestas (para no hacer N queries)
    cur.execute("SELECT id_respuesta, id_pregunta, texto FROM respuestas ORDER BY id_pregunta, id_respuesta")
    respuestas_all = cur.fetchall()

    # Organizar respuestas por pregunta
    respuestas_map = {}
    for r in respuestas_all:
        pid = r['id_pregunta']
        respuestas_map.setdefault(pid, []).append(r)

    if request.method == 'POST':
        # Limpiar respuestas previas del usuario (si repite el test)
        cur.execute("DELETE FROM usuario_respuestas WHERE id_usuario = %s", (user_id,))

        # Insertar las nuevas respuestas
        for p in preguntas:
            key = f"q_{p['id_pregunta']}"
            selected = request.form.get(key)
            if selected:
                cur.execute(
                    "INSERT INTO usuario_respuestas (id_usuario, id_pregunta, id_respuesta) VALUES (%s, %s, %s)",
                    (user_id, p['id_pregunta'], int(selected))
                )
        mysql.connection.commit()

        # Calcular puntajes sumados por carrera
        cur.execute("""
            SELECT 
              COALESCE(SUM(r.puntaje_sistemas),0) AS total_sistemas,
              COALESCE(SUM(r.puntaje_mecanica),0) AS total_mecanica,
              COALESCE(SUM(r.puntaje_electromecanica),0) AS total_electromecanica,
              COALESCE(SUM(r.puntaje_alimentos),0) AS total_alimentos
            FROM usuario_respuestas ur
            JOIN respuestas r ON ur.id_respuesta = r.id_respuesta
            WHERE ur.id_usuario = %s
        """, (user_id,))
        totals = cur.fetchone()

        # Determinar la carrera con mayor puntaje
        mapping = {
            'total_sistemas': 'Sistemas Informaticos',
            'total_mecanica': 'Mecanica Automotriz',
            'total_electromecanica': 'Electromecanica',
            'total_alimentos': 'Industria de Alimentos'
        }

        max_key = max(totals, key=lambda k: totals[k])
        puntaje_max = totals[max_key]
        carrera_recomendada = mapping.get(max_key, 'Sin recomendacion')

        cur.execute("SELECT id_carrera FROM carreras WHERE nombre = %s LIMIT 1", (carrera_recomendada,))
        row = cur.fetchone()
        id_carrera = row['id_carrera'] if row else None

        cur.execute("""
            INSERT INTO resultados (id_usuario, id_carrera, puntaje_total)
            VALUES (%s, %s, %s)
        """, (user_id, id_carrera, puntaje_max))

        mysql.connection.commit()
        cur.close()

        session['last_result'] = {'carrera': carrera_recomendada, 'puntaje': puntaje_max}
        return redirect(url_for('resultado'))

    cur.close()
    return render_template('preguntas.html', preguntas=preguntas, respuestas_map=respuestas_map)

# ----------------------------------------------------
# RUTA: mostrar resultado para el usuario en session
# ----------------------------------------------------
@app.route('/resultados')
def resultados():
    cur = mysql.connection.cursor()
    cur.execute("""
        SELECT u.id_usuario,
               u.nombre AS usuario,
               c.nombre AS carrera_recomendada,
               r.puntaje_total AS puntaje,
               r.fecha
        FROM resultados r
        JOIN usuarios u ON r.id_usuario = u.id_usuario
        JOIN carreras c ON r.id_carrera = c.id_carrera
        WHERE r.id_resultado = (
            SELECT MAX(r2.id_resultado)
            FROM resultados r2
            WHERE r2.id_usuario = u.id_usuario
        )
        ORDER BY r.fecha DESC
    """)
    resultados = cur.fetchall()
    cur.close()
    return render_template('resultados.html', resultados=resultados)

# ----------------------------------------------------
# RUTA:eliminar un usuario
# ----------------------------------------------------
@app.route('/eliminar_usuario/<int:id_usuario>', methods=['POST'])
def eliminar_usuario(id_usuario):
    cur = mysql.connection.cursor()
    # Primero elimina los resultados y respuestas relacionadas (por integridad)
    cur.execute("DELETE FROM usuario_respuestas WHERE id_usuario = %s", (id_usuario,))
    cur.execute("DELETE FROM resultados WHERE id_usuario = %s", (id_usuario,))
    cur.execute("DELETE FROM usuarios WHERE id_usuario = %s", (id_usuario,))
    mysql.connection.commit()
    cur.close()
    flash('Usuario eliminado correctamente.', 'success')
    return redirect(url_for('listar_usuarios'))  # vuelve a la lista de usuarios



# ----------------------------------------------------
# RUTA: mostrar historial del usuario
# ----------------------------------------------------

@app.route('/historial/<int:id_usuario>')
def historial(id_usuario):
    cur = mysql.connection.cursor(DictCursor)  # 👈 aquí el cambio
    cur.execute("""
        SELECT r.id_resultado,
               c.nombre AS carrera_recomendada,
               r.puntaje_total AS puntaje,
               r.fecha
        FROM resultados r
        JOIN carreras c ON r.id_carrera = c.id_carrera
        WHERE r.id_usuario = %s
        ORDER BY r.fecha DESC
    """, (id_usuario,))
    historial = cur.fetchall()
    cur.close()
    return render_template('historial.html', historial=historial)

# ----------------------------------------------------
# RUTAS UTILES: listar usuarios/resultados (para el docente)
# ----------------------------------------------------
@app.route('/usuarios')
def listar_usuarios():
    cur = mysql.connection.cursor()
    cur.execute("SELECT * FROM usuarios ORDER BY creado ASC")
    data = cur.fetchall()
    cur.close()
    return render_template('usuarios.html', usuarios=data)

@app.route('/resultados_admin')
def listar_resultados():
    cur = mysql.connection.cursor()
    cur.execute("""
        SELECT 
            r.id_resultado,
            u.id_usuario,
            u.nombre AS usuario_nombre,
            c.nombre AS carrera_recomendada,
            r.puntaje_total,
            r.fecha
        FROM resultados r
        JOIN usuarios u ON r.id_usuario = u.id_usuario
        JOIN carreras c ON r.id_carrera = c.id_carrera
        ORDER BY r.fecha ASC
    """)
    data = cur.fetchall()
    cur.close()
    return render_template('resultados.html', resultados=data)


#--------------------------------------------------------------
@app.route('/resultado')
def resultado():
    # Obtener el ID del usuario desde la sesión
    user_id = session.get('user_id')
    
    if not user_id:
        flash('No hay usuario activo. Inicia el test primero.', 'info')
        return redirect(url_for('start_test'))

    # Crear cursor y obtener el último resultado del usuario
    cur = mysql.connection.cursor()
    query = """
        SELECT r.id_usuario, r.puntaje_total, c.nombre AS carrera
        FROM resultados r
        JOIN carreras c ON r.id_carrera = c.id_carrera
        WHERE r.id_usuario = %s
        ORDER BY r.id_resultado DESC
        LIMIT 1
    """
    cur.execute(query, (user_id,))
    res = cur.fetchone()
    cur.close()

    # Verificar si hay resultados
    if not res:
        flash('No hay resultado todavía. Realiza el test.', 'warning')
        return redirect(url_for('preguntas'))

    # Renderizar plantilla con el resultado
    return render_template('resultado.html', resultado=res)

# ----------------------------------------------------
# estadisticas de los usuarios por carrera
# ----------------------------------------------------
@app.route('/estadisticas')
def estadisticas():
    cur = mysql.connection.cursor()

    # Consulta general: total de usuarios por carrera
    cur.execute("""
        SELECT c.nombre AS carrera, COUNT(u.id_usuario) AS total_usuarios
        FROM resultados r
        JOIN usuarios u ON r.id_usuario = u.id_usuario
        JOIN carreras c ON r.id_carrera = c.id_carrera
        WHERE r.id_resultado = (
            SELECT MAX(r2.id_resultado)
            FROM resultados r2
            WHERE r2.id_usuario = u.id_usuario
        )
        GROUP BY c.nombre
        ORDER BY total_usuarios DESC
    """)
    resumen = cur.fetchall()

    # Consulta detallada: usuarios agrupados por carrera
    cur.execute("""
        SELECT c.nombre AS carrera, u.nombre, u.correo, r.puntaje_total
        FROM resultados r
        JOIN usuarios u ON r.id_usuario = u.id_usuario
        JOIN carreras c ON r.id_carrera = c.id_carrera
        WHERE r.id_resultado = (
            SELECT MAX(r2.id_resultado)
            FROM resultados r2
            WHERE r2.id_usuario = u.id_usuario
        )
        ORDER BY c.nombre, r.puntaje_total DESC
    """)
    detalle = cur.fetchall()

    cur.close()
    return render_template('estadisticas.html', resumen=resumen, detalle=detalle)


@app.route('/codigo_qr')
def codigo_qr():
    # Aquí va la URL del test (ajústala si tu ruta es diferente)
    url_test = "http://127.0.0.1:5000"

    # Generar el código QR
    img = qrcode.make(url_test)
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)

    return send_file(buf, mimetype='image/png')

@app.route('/qr_test')
def qr_test():
    return render_template('qr_test.html')


# ----------------------------------------------------
# CORRER APP
# ----------------------------------------------------
if __name__ == '__main__':
    app.run(debug=True)
