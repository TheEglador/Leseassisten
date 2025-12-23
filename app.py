"""
LeseAssistent - Flask Backend mit Session-System
=================================================
Sicheres Session-basiertes System für den Unterricht.

- Lehrer erstellt Session mit seinen API-Keys
- Schüler treten mit Session-Code bei
- Keys bleiben NUR auf dem Server (im RAM)
- Session-Ende = Keys gelöscht
"""

from flask import Flask, request, jsonify, render_template, send_file
from flask_cors import CORS
from flask_socketio import SocketIO, emit, join_room, leave_room, rooms
import requests
import os
import json
import base64
import hashlib
import re
import random
import string
import qrcode
from io import BytesIO
from collections import OrderedDict
from datetime import datetime, timedelta
import threading
import time

# Für Datei-Verarbeitung
try:
    from docx import Document
    DOCX_AVAILABLE = True
except ImportError:
    DOCX_AVAILABLE = False

try:
    import pdfplumber
    PDF_AVAILABLE = True
except ImportError:
    PDF_AVAILABLE = False

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'leseassistent-secret-key-change-in-production')
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

# =============================================================================
# SESSION MANAGEMENT
# =============================================================================

# Session-Speicher (In-Memory - Keys sind NIE in einer Datenbank!)
sessions = {}
sessions_lock = threading.Lock()

# Session-Konfiguration
SESSION_CODE_LENGTH = 6
SESSION_TIMEOUT_HOURS = 3
CLEANUP_INTERVAL_SECONDS = 300  # Alle 5 Minuten aufräumen

def generate_session_code():
    """Generiert einen 6-stelligen alphanumerischen Code (ohne verwechselbare Zeichen)."""
    # Keine 0, O, I, l um Verwechslungen zu vermeiden
    chars = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
    while True:
        code = ''.join(random.choices(chars, k=SESSION_CODE_LENGTH))
        with sessions_lock:
            if code not in sessions:
                return code

def create_session(teacher_sid, keys, pin=''):
    """Erstellt eine neue Session für einen Lehrer."""
    code = generate_session_code()
    with sessions_lock:
        sessions[code] = {
            'keys': keys,
            'teacher_sid': teacher_sid,
            'created': datetime.now(),
            'expires': datetime.now() + timedelta(hours=SESSION_TIMEOUT_HOURS),
            'students': {},  # {sid: {'joined': datetime, 'name': optional}}
            'text': '',  # Geteilter Text für alle Schüler
            'pin': pin,  # Optionaler PIN-Schutz für Lehrer-Dashboard
            'tasks': [],  # Generierte Aufgaben
            'tasks_available': False,  # Ob Aufgaben freigegeben sind
        }
    return code

def get_session(code):
    """Holt Session-Daten (ohne Keys zu exponieren)."""
    with sessions_lock:
        if code in sessions:
            session = sessions[code]
            if datetime.now() < session['expires']:
                return session
            else:
                # Session abgelaufen, löschen
                del sessions[code]
    return None

def get_session_keys(code):
    """Holt die API-Keys für eine Session (nur für Server-interne Nutzung!)."""
    session = get_session(code)
    if session:
        return session['keys']
    return None

def end_session(code):
    """Beendet eine Session und löscht alle Keys."""
    with sessions_lock:
        if code in sessions:
            del sessions[code]
            return True
    return False

def add_student_to_session(code, student_sid, student_name=None):
    """Fügt einen Schüler zur Session hinzu."""
    with sessions_lock:
        if code in sessions:
            sessions[code]['students'][student_sid] = {
                'joined': datetime.now(),
                'name': student_name
            }
            return True
    return False

def remove_student_from_session(code, student_sid):
    """Entfernt einen Schüler aus der Session."""
    with sessions_lock:
        if code in sessions and student_sid in sessions[code]['students']:
            del sessions[code]['students'][student_sid]
            return True
    return False

def get_student_count(code):
    """Gibt die Anzahl der verbundenen Schüler zurück."""
    session = get_session(code)
    if session:
        return len(session['students'])
    return 0

def cleanup_expired_sessions():
    """Entfernt abgelaufene Sessions (wird periodisch aufgerufen)."""
    with sessions_lock:
        expired = [code for code, session in sessions.items() 
                   if datetime.now() >= session['expires']]
        for code in expired:
            del sessions[code]
            app.logger.info(f"Session {code} expired and cleaned up")

# Background-Thread für Session-Cleanup
def session_cleanup_thread():
    while True:
        time.sleep(CLEANUP_INTERVAL_SECONDS)
        cleanup_expired_sessions()

# Cleanup-Thread starten (nur wenn nicht im Import-Modus)
cleanup_thread = threading.Thread(target=session_cleanup_thread, daemon=True)

# =============================================================================
# TTS & TRANSLATION CACHE (In-Memory)
# =============================================================================

MAX_CACHE_SIZE = 500
MAX_TRANSLATION_CACHE_SIZE = 1000
tts_cache = OrderedDict()
translation_cache = OrderedDict()
cache_lock = threading.Lock()
translation_cache_lock = threading.Lock()

def get_cache_key(text, voice_id):
    content = f"{text}|{voice_id}"
    return hashlib.md5(content.encode('utf-8')).hexdigest()

def get_translation_cache_key(text, target_language):
    content = f"{text}|{target_language}"
    return hashlib.md5(content.encode('utf-8')).hexdigest()

def get_from_cache(cache_key):
    with cache_lock:
        if cache_key in tts_cache:
            tts_cache.move_to_end(cache_key)
            return tts_cache[cache_key]
    return None

def add_to_cache(cache_key, data):
    with cache_lock:
        if cache_key in tts_cache:
            tts_cache.move_to_end(cache_key)
        else:
            if len(tts_cache) >= MAX_CACHE_SIZE:
                tts_cache.popitem(last=False)
            tts_cache[cache_key] = data

def get_from_translation_cache(cache_key):
    with translation_cache_lock:
        if cache_key in translation_cache:
            translation_cache.move_to_end(cache_key)
            return translation_cache[cache_key]
    return None

def add_to_translation_cache(cache_key, translated_text):
    with translation_cache_lock:
        if cache_key in translation_cache:
            translation_cache.move_to_end(cache_key)
        else:
            if len(translation_cache) >= MAX_TRANSLATION_CACHE_SIZE:
                translation_cache.popitem(last=False)
            translation_cache[cache_key] = translated_text

# =============================================================================
# TEXT CLEANUP
# =============================================================================

def cleanup_extracted_text(text):
    """Bereinigt aus PDFs/DOCX extrahierten Text."""
    if not text:
        return text
    text = re.sub(r'(\w)-\s*\n\s*(\w)', r'\1\2', text)
    text = re.sub(r'(\w)-\s+(\w)', r'\1\2', text)
    text = re.sub(r'(?<!\n)\n(?!\n)', ' ', text)
    text = re.sub(r' +', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()

# =============================================================================
# FRONTEND ROUTES
# =============================================================================

@app.route('/')
def index():
    """Startseite - Auswahl Lehrer/Schüler."""
    return render_template('index.html')

@app.route('/teacher')
def teacher_dashboard():
    """Lehrer-Dashboard für Session-Management."""
    return render_template('teacher.html')

@app.route('/student')
def student_view():
    """Schüler-Ansicht (nach Session-Beitritt)."""
    return render_template('student.html')

@app.route('/aufgaben')
def aufgaben():
    """Aufgaben-Seite."""
    return render_template('aufgaben.html')

@app.route('/nachsprechen')
def nachsprechen():
    """Nachsprechen-Übung."""
    return render_template('nachsprechen.html')

# =============================================================================
# SESSION API ENDPOINTS
# =============================================================================

@app.route('/api/session/create', methods=['POST'])
def api_create_session():
    """
    Erstellt eine neue Session (nur für Lehrer).
    
    Erwartet JSON:
    {
        "elevenlabs_key": "sk_...",
        "ai_key": "sk-...",
        "ai_provider": "openai" | "anthropic" | "google",
        "voice_id": "21m00Tcm4TlvDq8ikWAM"
    }
    """
    try:
        data = request.json
        
        keys = {
            'elevenlabs': data.get('elevenlabs_key', ''),
            'ai': data.get('ai_key', ''),
            'ai_provider': data.get('ai_provider', 'openai'),
            'voice_id': data.get('voice_id', '21m00Tcm4TlvDq8ikWAM')
        }
        
        if not keys['elevenlabs']:
            return jsonify({'error': 'ElevenLabs API Key erforderlich'}), 400
        
        # Session erstellen (teacher_sid wird später via WebSocket gesetzt)
        code = create_session(None, keys)
        
        return jsonify({
            'success': True,
            'code': code,
            'expires': sessions[code]['expires'].isoformat()
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/session/join', methods=['POST'])
def api_join_session():
    """
    Prüft ob eine Session existiert (für Schüler).
    
    Erwartet JSON:
    {
        "code": "ABC123"
    }
    """
    try:
        data = request.json
        code = data.get('code', '').upper().strip()
        
        session = get_session(code)
        if not session:
            return jsonify({'error': 'Session nicht gefunden oder abgelaufen'}), 404
        
        return jsonify({
            'success': True,
            'code': code,
            'student_count': len(session['students'])
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/session/end', methods=['POST'])
def api_end_session():
    """
    Beendet eine Session (nur für Lehrer).
    
    Erwartet JSON:
    {
        "code": "ABC123"
    }
    """
    try:
        data = request.json
        code = data.get('code', '').upper().strip()
        
        if end_session(code):
            # Alle Clients in dieser Session benachrichtigen
            socketio.emit('session_ended', {'message': 'Die Session wurde beendet.'}, room=code)
            return jsonify({'success': True})
        else:
            return jsonify({'error': 'Session nicht gefunden'}), 404
            
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/session/status/<code>')
def api_session_status(code):
    """Gibt den Status einer Session zurück."""
    code = code.upper().strip()
    session = get_session(code)
    
    if not session:
        return jsonify({'error': 'Session nicht gefunden'}), 404
    
    return jsonify({
        'code': code,
        'student_count': len(session['students']),
        'created': session['created'].isoformat(),
        'expires': session['expires'].isoformat(),
        'has_text': bool(session.get('text'))
    })

@app.route('/api/session/qr/<code>')
def api_session_qr(code):
    """Generiert QR-Code für Session-Beitritt."""
    code = code.upper().strip()
    session = get_session(code)
    
    if not session:
        return jsonify({'error': 'Session nicht gefunden'}), 404
    
    # URL für Schüler-Beitritt
    base_url = request.host_url.rstrip('/')
    join_url = f"{base_url}/student?code={code}"
    
    # QR-Code generieren
    qr = qrcode.QRCode(version=1, box_size=10, border=5)
    qr.add_data(join_url)
    qr.make(fit=True)
    
    img = qr.make_image(fill_color="black", back_color="white")
    
    # Als PNG zurückgeben
    buffer = BytesIO()
    img.save(buffer, format='PNG')
    buffer.seek(0)
    
    return send_file(buffer, mimetype='image/png')

@app.route('/api/session/set-text', methods=['POST'])
def api_set_session_text():
    """
    Setzt den Text für eine Session (Lehrer teilt Text mit Schülern).
    
    Erwartet JSON:
    {
        "code": "ABC123",
        "text": "Der zu lesende Text..."
    }
    """
    try:
        data = request.json
        code = data.get('code', '').upper().strip()
        text = data.get('text', '')
        
        with sessions_lock:
            if code in sessions:
                sessions[code]['text'] = text
                # Alle Schüler benachrichtigen
                socketio.emit('text_updated', {'text': text}, room=code)
                return jsonify({'success': True})
        
        return jsonify({'error': 'Session nicht gefunden'}), 404
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/session/get-text/<code>')
def api_get_session_text(code):
    """Holt den Text einer Session."""
    code = code.upper().strip()
    session = get_session(code)
    
    if not session:
        return jsonify({'error': 'Session nicht gefunden'}), 404
    
    return jsonify({'text': session.get('text', '')})

# =============================================================================
# WEBSOCKET EVENTS
# =============================================================================

@socketio.on('connect')
def handle_connect():
    app.logger.info(f"Client connected: {request.sid}")

@socketio.on('disconnect')
def handle_disconnect():
    app.logger.info(f"Client disconnected: {request.sid}")
    # Schüler aus allen Sessions entfernen
    with sessions_lock:
        for code, session in sessions.items():
            if request.sid in session['students']:
                del session['students'][request.sid]
                # Lehrer über Schüler-Abgang informieren
                if session['teacher_sid']:
                    socketio.emit('student_left', {
                        'count': len(session['students'])
                    }, room=session['teacher_sid'])

@socketio.on('teacher_create_session')
def handle_teacher_create_session(data):
    """Lehrer erstellt Session via WebSocket."""
    keys = {
        'elevenlabs': data.get('elevenlabs_key', ''),
        'ai': data.get('ai_key', ''),
        'ai_provider': data.get('ai_provider', 'openai'),
        'voice_id': data.get('voice_id', '21m00Tcm4TlvDq8ikWAM')
    }
    
    if not keys['elevenlabs']:
        emit('session_error', {'error': 'ElevenLabs API Key erforderlich'})
        return
    
    pin = data.get('pin', '')
    code = create_session(request.sid, keys, pin)
    
    # Lehrer tritt seinem eigenen Room bei
    join_room(code)
    
    emit('session_created', {
        'code': code,
        'expires': sessions[code]['expires'].isoformat(),
        'has_pin': bool(pin)
    })

@socketio.on('student_join_session')
def handle_student_join_session(data):
    """Schüler tritt Session bei."""
    code = data.get('code', '').upper().strip()
    name = data.get('name', 'Anonym')
    
    session = get_session(code)
    if not session:
        emit('join_error', {'error': 'Session nicht gefunden oder abgelaufen'})
        return
    
    # Schüler zur Session hinzufügen
    add_student_to_session(code, request.sid, name)
    join_room(code)
    
    # Schüler bestätigen (inkl. vorhandener Einstellungen und Aufgaben)
    emit('join_success', {
        'code': code,
        'text': session.get('text', ''),
        'settings': session.get('settings', {}),
        'tasks_available': session.get('tasks_available', False),
        'tasks': session.get('tasks', []) if session.get('tasks_available', False) else []
    })
    
    # Lehrer über neuen Schüler informieren
    student_count = len(session['students'])
    if session['teacher_sid']:
        socketio.emit('student_joined', {
            'count': student_count,
            'name': name
        }, room=session['teacher_sid'])

@socketio.on('teacher_end_session')
def handle_teacher_end_session(data):
    """Lehrer beendet Session via WebSocket."""
    code = data.get('code', '').upper().strip()
    
    session = get_session(code)
    if session and session['teacher_sid'] == request.sid:
        # Alle Schüler benachrichtigen
        socketio.emit('session_ended', {'message': 'Die Session wurde vom Lehrer beendet.'}, room=code)
        
        # Session löschen
        end_session(code)
        
        emit('session_ended_confirmed', {'success': True})
    else:
        emit('session_error', {'error': 'Keine Berechtigung oder Session nicht gefunden'})

@socketio.on('teacher_update_settings')
def handle_teacher_update_settings(data):
    """Lehrer sendet Barrierefreiheits-Einstellungen an alle Schüler."""
    code = data.get('code', '').upper().strip()
    settings = data.get('settings', {})
    
    session = get_session(code)
    if session and session['teacher_sid'] == request.sid:
        # Einstellungen in Session speichern
        with sessions_lock:
            if code in sessions:
                sessions[code]['settings'] = settings
        
        # An alle Schüler im Raum senden
        socketio.emit('settings_updated', {'settings': settings}, room=code)
        app.logger.info(f"Settings updated for session {code}")
    else:
        emit('session_error', {'error': 'Keine Berechtigung oder Session nicht gefunden'})

@socketio.on('teacher_release_tasks')
def handle_teacher_release_tasks(data):
    """Lehrer gibt Aufgaben an alle Schüler frei."""
    code = data.get('code', '').upper().strip()
    tasks = data.get('tasks', [])
    
    session = get_session(code)
    if session and session['teacher_sid'] == request.sid:
        # Aufgaben in Session speichern und freigeben
        with sessions_lock:
            if code in sessions:
                sessions[code]['tasks'] = tasks
                sessions[code]['tasks_available'] = True
        
        # An alle Schüler im Raum senden
        socketio.emit('tasks_released', {'tasks': tasks}, room=code)
        app.logger.info(f"Tasks released for session {code}: {len(tasks)} tasks")
    else:
        emit('session_error', {'error': 'Keine Berechtigung oder Session nicht gefunden'})

# =============================================================================
# FILE UPLOAD & TEXT EXTRACTION
# =============================================================================

@app.route('/api/extract-text', methods=['POST'])
def extract_text_from_file():
    """Extrahiert Text aus hochgeladenen Dateien."""
    try:
        if 'file' not in request.files:
            return jsonify({'error': 'Keine Datei hochgeladen'}), 400
        
        file = request.files['file']
        filename = file.filename.lower()
        
        if filename.endswith('.docx'):
            if not DOCX_AVAILABLE:
                return jsonify({'error': 'DOCX-Verarbeitung nicht verfügbar'}), 500
            doc = Document(BytesIO(file.read()))
            paragraphs = [para.text for para in doc.paragraphs if para.text.strip()]
            text = '\n\n'.join(paragraphs)
            
        elif filename.endswith('.pdf'):
            if not PDF_AVAILABLE:
                return jsonify({'error': 'PDF-Verarbeitung nicht verfügbar'}), 500
            text_parts = []
            with pdfplumber.open(BytesIO(file.read())) as pdf:
                for page in pdf.pages:
                    page_text = page.extract_text()
                    if page_text:
                        text_parts.append(page_text)
            text = '\n\n'.join(text_parts)
            
        elif filename.endswith('.txt'):
            text = file.read().decode('utf-8')
        else:
            return jsonify({'error': 'Nicht unterstütztes Format. Erlaubt: .docx, .pdf, .txt'}), 400
        
        text = cleanup_extracted_text(text)
        
        if not text.strip():
            return jsonify({'error': 'Kein Text in der Datei gefunden'}), 400
        
        return jsonify({'text': text.strip()})
        
    except Exception as e:
        return jsonify({'error': f'Fehler: {str(e)}'}), 500

# =============================================================================
# TASKS GENERATION
# =============================================================================

@app.route('/api/generate-tasks', methods=['POST'])
def api_generate_tasks():
    """
    Generiert Aufgaben für einen Text via KI.
    
    Erwartet JSON:
    {
        "text": "Der zu lesende Text...",
        "session_code": "ABC123"
    }
    """
    try:
        data = request.json
        text = data.get('text', '')
        session_code = data.get('session_code', '').upper().strip()
        
        if not text:
            return jsonify({'error': 'Text fehlt'}), 400
        
        # Keys aus Session holen
        keys = get_session_keys(session_code)
        if not keys:
            return jsonify({'error': 'Session nicht gefunden'}), 404
        
        ai_key = keys.get('ai', '')
        ai_provider = keys.get('ai_provider', 'openai')
        
        if not ai_key:
            return jsonify({'error': 'KI API Key nicht konfiguriert'}), 400
        
        # Prompt für Aufgabengenerierung
        prompt = f"""Erstelle 5 Verständnisaufgaben zu folgendem Text. Die Aufgaben sollen für Schüler mit Leseschwierigkeiten geeignet sein.

TEXT:
{text}

Erstelle genau 5 Aufgaben in diesem JSON-Format (KEINE anderen Texte, NUR das JSON-Array):
[
  {{"type": "multiple_choice", "question": "Frage?", "options": ["A", "B", "C", "D"], "correct": 0}},
  {{"type": "true_false", "question": "Aussage?", "correct": true}},
  {{"type": "fill_blank", "question": "Satz mit ___ Lücke.", "correct": "Wort"}},
  {{"type": "short_answer", "question": "Offene Frage?", "hint": "Hinweis"}},
  {{"type": "order", "question": "Bringe in die richtige Reihenfolge:", "items": ["Erst", "Dann", "Zuletzt"], "correct_order": [0, 1, 2]}}
]

Verwende verschiedene Aufgabentypen. Antworte NUR mit dem JSON-Array."""

        tasks = []
        
        if ai_provider == 'openai':
            import requests as req
            response = req.post(
                'https://api.openai.com/v1/chat/completions',
                headers={
                    'Authorization': f'Bearer {ai_key}',
                    'Content-Type': 'application/json'
                },
                json={
                    'model': 'gpt-4o-mini',
                    'messages': [{'role': 'user', 'content': prompt}],
                    'temperature': 0.7
                }
            )
            
            if response.status_code == 200:
                result = response.json()
                content = result['choices'][0]['message']['content']
                # JSON extrahieren
                import json
                import re
                json_match = re.search(r'\[[\s\S]*\]', content)
                if json_match:
                    tasks = json.loads(json_match.group())
                    
        elif ai_provider == 'google':
            import requests as req
            response = req.post(
                f'https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={ai_key}',
                headers={'Content-Type': 'application/json'},
                json={
                    'contents': [{'parts': [{'text': prompt}]}],
                    'generationConfig': {'temperature': 0.7}
                }
            )
            
            if response.status_code == 200:
                result = response.json()
                content = result['candidates'][0]['content']['parts'][0]['text']
                import json
                import re
                json_match = re.search(r'\[[\s\S]*\]', content)
                if json_match:
                    tasks = json.loads(json_match.group())
                    
        elif ai_provider == 'anthropic':
            import requests as req
            response = req.post(
                'https://api.anthropic.com/v1/messages',
                headers={
                    'x-api-key': ai_key,
                    'anthropic-version': '2023-06-01',
                    'Content-Type': 'application/json'
                },
                json={
                    'model': 'claude-sonnet-4-20250514',
                    'max_tokens': 2000,
                    'messages': [{'role': 'user', 'content': prompt}]
                }
            )
            
            if response.status_code == 200:
                result = response.json()
                content = result['content'][0]['text']
                import json
                import re
                json_match = re.search(r'\[[\s\S]*\]', content)
                if json_match:
                    tasks = json.loads(json_match.group())
        
        if not tasks:
            return jsonify({'error': 'Aufgabengenerierung fehlgeschlagen'}), 500
        
        return jsonify({'tasks': tasks})
        
    except Exception as e:
        app.logger.error(f"Task generation error: {e}")
        return jsonify({'error': str(e)}), 500

# =============================================================================
# TTS PROXY (Session-basiert)
# =============================================================================

@app.route('/api/tts', methods=['POST'])
def proxy_tts():
    """
    TTS via Session-Code (Schüler) oder direkt mit Key (Lehrer).
    
    Erwartet JSON:
    {
        "session_code": "ABC123",  // ODER
        "api_key": "sk_...",       // Für Lehrer-Direktzugriff
        "text": "...",
        "voice_id": "..."  // optional
    }
    """
    try:
        data = request.json
        text = data.get('text')
        
        if not text:
            return jsonify({'error': 'Text fehlt'}), 400
        
        # Keys ermitteln (Session oder direkt)
        session_code = data.get('session_code', '').upper().strip()
        
        if session_code:
            keys = get_session_keys(session_code)
            if not keys:
                return jsonify({'error': 'Session nicht gefunden oder abgelaufen'}), 404
            api_key = keys['elevenlabs']
            voice_id = data.get('voice_id') or keys.get('voice_id', '21m00Tcm4TlvDq8ikWAM')
        else:
            api_key = data.get('api_key')
            voice_id = data.get('voice_id', '21m00Tcm4TlvDq8ikWAM')
            if not api_key:
                return jsonify({'error': 'API Key oder Session-Code erforderlich'}), 400
        
        # Cache prüfen
        cache_key = get_cache_key(text, voice_id)
        cached = get_from_cache(cache_key)
        if cached:
            app.logger.info(f"TTS Cache HIT")
            return jsonify(cached)
        
        # ElevenLabs API aufrufen
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/with-timestamps"
        
        headers = {
            'xi-api-key': api_key,
            'Content-Type': 'application/json'
        }
        
        payload = {
            'text': text,
            'model_id': data.get('model_id', 'eleven_multilingual_v2'),
            'voice_settings': {
                'stability': 0.5,
                'similarity_boost': 0.75
            }
        }
        
        response = requests.post(url, headers=headers, json=payload, timeout=60)
        
        if response.status_code != 200:
            error_detail = response.json().get('detail', {})
            error_msg = error_detail.get('message', 'ElevenLabs API Fehler')
            return jsonify({'error': error_msg}), response.status_code
        
        response_data = response.json()
        add_to_cache(cache_key, response_data)
        
        return jsonify(response_data)
        
    except requests.exceptions.Timeout:
        return jsonify({'error': 'Timeout'}), 504
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# =============================================================================
# OCR PROXY (Session-basiert)
# =============================================================================

@app.route('/api/ocr', methods=['POST'])
def ocr_image():
    """OCR via KI-API - Session-basiert oder mit direktem Key."""
    try:
        data = request.json
        image_base64 = data.get('image')
        mime_type = data.get('mime_type', 'image/jpeg')
        
        if not image_base64:
            return jsonify({'error': 'Kein Bild übermittelt'}), 400
        
        # Keys ermitteln
        session_code = data.get('session_code', '').upper().strip()
        
        if session_code:
            keys = get_session_keys(session_code)
            if not keys:
                return jsonify({'error': 'Session nicht gefunden'}), 404
            api_key = keys['ai']
            provider = keys['ai_provider']
        else:
            api_key = data.get('api_key')
            provider = data.get('provider', 'openai')
        
        if not api_key:
            return jsonify({'error': 'KI API Key erforderlich'}), 400
        
        ocr_prompt = """Extrahiere den gesamten Text aus diesem Bild. 
Gib NUR den erkannten Text zurück, ohne Erklärungen.
Behalte Absätze bei. Wenn kein Text erkennbar ist: [KEIN TEXT ERKANNT]"""
        
        if provider == 'openai':
            text = call_openai_vision(api_key, ocr_prompt, image_base64, mime_type)
        elif provider == 'anthropic':
            text = call_anthropic_vision(api_key, ocr_prompt, image_base64, mime_type)
        elif provider == 'google':
            text = call_google_vision(api_key, ocr_prompt, image_base64, mime_type)
        else:
            return jsonify({'error': f'Unbekannter Provider: {provider}'}), 400
        
        if '[KEIN TEXT ERKANNT]' in text:
            return jsonify({'error': 'Kein Text im Bild erkannt'}), 400
        
        return jsonify({'text': text.strip()})
        
    except Exception as e:
        return jsonify({'error': f'OCR-Fehler: {str(e)}'}), 500

# =============================================================================
# TRANSLATION PROXY (Session-basiert)
# =============================================================================

@app.route('/api/translate', methods=['POST'])
def proxy_translate():
    """Übersetzung via KI-API - Session-basiert oder mit direktem Key."""
    try:
        data = request.json
        text = data.get('text')
        target_language = data.get('target_language', 'de')
        
        if not text:
            return jsonify({'error': 'Text fehlt'}), 400
        
        if target_language == 'de':
            return jsonify({'translated_text': text})
        
        # Keys ermitteln
        session_code = data.get('session_code', '').upper().strip()
        
        if session_code:
            keys = get_session_keys(session_code)
            if not keys:
                return jsonify({'error': 'Session nicht gefunden'}), 404
            api_key = keys['ai']
            provider = keys['ai_provider']
        else:
            api_key = data.get('api_key')
            provider = data.get('provider', 'openai')
        
        if not api_key:
            return jsonify({'error': 'KI API Key erforderlich'}), 400
        
        # Cache prüfen
        cache_key = get_translation_cache_key(text, target_language)
        cached = get_from_translation_cache(cache_key)
        if cached:
            return jsonify({'translated_text': cached, 'cached': True})
        
        language_names = {
            'de': 'Deutsch', 'tr': 'Türkisch', 'bg': 'Bulgarisch',
            'ar': 'Arabisch', 'uk': 'Ukrainisch', 'en': 'Englisch'
        }
        target_name = language_names.get(target_language, 'Deutsch')
        
        system_prompt = f"""Du bist ein professioneller Übersetzer. Übersetze ins {target_name}.
Regeln: NUR die Übersetzung ausgeben, keine Erklärungen. Formatierung beibehalten."""
        
        if provider == 'openai':
            result = call_openai_text(api_key, system_prompt, text)
        elif provider == 'anthropic':
            result = call_anthropic_text(api_key, system_prompt, text)
        elif provider == 'google':
            result = call_google_text(api_key, system_prompt, text)
        else:
            return jsonify({'error': f'Unbekannter Provider'}), 400
        
        add_to_translation_cache(cache_key, result)
        return jsonify({'translated_text': result})
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# =============================================================================
# AI TASK GENERATION (Session-basiert)
# =============================================================================

@app.route('/api/generate-tasks', methods=['POST'])
def generate_tasks():
    """Generiert Aufgaben via KI - Session-basiert oder mit direktem Key."""
    try:
        data = request.json
        text = data.get('text')
        task_types = data.get('task_types', ['multiple_choice'])
        difficulty = data.get('difficulty', 'mittel')
        
        if not text:
            return jsonify({'error': 'Text fehlt'}), 400
        
        # Keys ermitteln
        session_code = data.get('session_code', '').upper().strip()
        
        if session_code:
            keys = get_session_keys(session_code)
            if not keys:
                return jsonify({'error': 'Session nicht gefunden'}), 404
            api_key = keys['ai']
            provider = keys['ai_provider']
        else:
            api_key = data.get('api_key')
            provider = data.get('provider', 'openai')
        
        if not api_key:
            return jsonify({'error': 'KI API Key erforderlich'}), 400
        
        # Task-Generation Prompt
        system_prompt = """Du bist ein erfahrener Deutschlehrer, der Leseverständnis-Aufgaben erstellt.
        
Erstelle Aufgaben zum gegebenen Text. Antworte NUR mit validem JSON in diesem Format:
{
  "tasks": [
    {
      "type": "multiple_choice",
      "question": "Frage hier",
      "options": ["A", "B", "C", "D"],
      "correct": 0,
      "explanation": "Erklärung"
    },
    {
      "type": "lueckentext",
      "text_with_gaps": "Satz mit ___ Lücken ___",
      "answers": ["Wort1", "Wort2"]
    },
    {
      "type": "open_question",
      "question": "Offene Frage",
      "sample_answer": "Beispielantwort"
    }
  ]
}"""

        task_type_str = ', '.join(task_types)
        user_message = f"""Text: {text}

Erstelle 5 Aufgaben. Schwierigkeit: {difficulty}. Aufgabentypen: {task_type_str}"""
        
        if provider == 'openai':
            result = call_openai_text(api_key, system_prompt, user_message)
        elif provider == 'anthropic':
            result = call_anthropic_text(api_key, system_prompt, user_message)
        elif provider == 'google':
            result = call_google_text(api_key, system_prompt, user_message)
        else:
            return jsonify({'error': 'Unbekannter Provider'}), 400
        
        # JSON parsen
        try:
            result = result.strip()
            if result.startswith('```'):
                result = re.sub(r'^```json?\n?', '', result)
                result = re.sub(r'\n?```$', '', result)
            tasks_data = json.loads(result)
            return jsonify(tasks_data)
        except json.JSONDecodeError:
            return jsonify({'error': 'KI-Antwort konnte nicht verarbeitet werden', 'raw': result}), 500
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# =============================================================================
# SPEECH-TO-TEXT (Session-basiert)
# =============================================================================

@app.route('/api/speech-to-text', methods=['POST'])
def proxy_speech_to_text():
    """Spracherkennung - Session-basiert oder mit direktem Key."""
    try:
        if 'audio' not in request.files:
            return jsonify({'error': 'Keine Audio-Datei'}), 400
        
        session_code = request.form.get('session_code', '').upper().strip()
        
        if session_code:
            keys = get_session_keys(session_code)
            if not keys:
                return jsonify({'error': 'Session nicht gefunden'}), 404
            api_key = keys['ai']
            provider = keys['ai_provider']
        else:
            api_key = request.form.get('api_key')
            provider = request.form.get('provider', 'openai')
        
        if not api_key:
            return jsonify({'error': 'API Key erforderlich'}), 400
        
        audio_file = request.files['audio']
        language = request.form.get('language', 'de')
        audio_data = audio_file.read()
        
        if provider == 'google':
            return transcribe_with_gemini(api_key, audio_data, language)
        else:
            return transcribe_with_whisper(api_key, audio_data, language)
            
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# =============================================================================
# AI API HELPER FUNCTIONS
# =============================================================================

def call_openai_text(api_key, system_prompt, user_message):
    headers = {
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/json'
    }
    payload = {
        'model': 'gpt-4o',
        'messages': [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_message}
        ],
        'max_tokens': 4000
    }
    response = requests.post('https://api.openai.com/v1/chat/completions', 
                           headers=headers, json=payload, timeout=60)
    if response.status_code != 200:
        raise Exception(f"OpenAI Error: {response.text}")
    return response.json()['choices'][0]['message']['content']

def call_anthropic_text(api_key, system_prompt, user_message):
    headers = {
        'x-api-key': api_key,
        'Content-Type': 'application/json',
        'anthropic-version': '2023-06-01'
    }
    payload = {
        'model': 'claude-sonnet-4-20250514',
        'max_tokens': 4000,
        'system': system_prompt,
        'messages': [{'role': 'user', 'content': user_message}]
    }
    response = requests.post('https://api.anthropic.com/v1/messages',
                           headers=headers, json=payload, timeout=60)
    if response.status_code != 200:
        raise Exception(f"Anthropic Error: {response.text}")
    return response.json()['content'][0]['text']

def call_google_text(api_key, system_prompt, user_message):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={api_key}"
    payload = {
        'contents': [{'parts': [{'text': f"{system_prompt}\n\n{user_message}"}]}]
    }
    response = requests.post(url, json=payload, timeout=60)
    if response.status_code != 200:
        raise Exception(f"Google Error: {response.text}")
    return response.json()['candidates'][0]['content']['parts'][0]['text']

def call_openai_vision(api_key, prompt, image_base64, mime_type):
    headers = {
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/json'
    }
    payload = {
        'model': 'gpt-4o',
        'messages': [{
            'role': 'user',
            'content': [
                {'type': 'text', 'text': prompt},
                {'type': 'image_url', 'image_url': {'url': f'data:{mime_type};base64,{image_base64}'}}
            ]
        }],
        'max_tokens': 4000
    }
    response = requests.post('https://api.openai.com/v1/chat/completions',
                           headers=headers, json=payload, timeout=60)
    if response.status_code != 200:
        raise Exception(f"OpenAI Vision Error: {response.text}")
    return response.json()['choices'][0]['message']['content']

def call_anthropic_vision(api_key, prompt, image_base64, mime_type):
    headers = {
        'x-api-key': api_key,
        'Content-Type': 'application/json',
        'anthropic-version': '2023-06-01'
    }
    payload = {
        'model': 'claude-sonnet-4-20250514',
        'max_tokens': 4000,
        'messages': [{
            'role': 'user',
            'content': [
                {'type': 'image', 'source': {'type': 'base64', 'media_type': mime_type, 'data': image_base64}},
                {'type': 'text', 'text': prompt}
            ]
        }]
    }
    response = requests.post('https://api.anthropic.com/v1/messages',
                           headers=headers, json=payload, timeout=60)
    if response.status_code != 200:
        raise Exception(f"Anthropic Vision Error: {response.text}")
    return response.json()['content'][0]['text']

def call_google_vision(api_key, prompt, image_base64, mime_type):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={api_key}"
    payload = {
        'contents': [{
            'parts': [
                {'text': prompt},
                {'inline_data': {'mime_type': mime_type, 'data': image_base64}}
            ]
        }]
    }
    response = requests.post(url, json=payload, timeout=60)
    if response.status_code != 200:
        raise Exception(f"Google Vision Error: {response.text}")
    return response.json()['candidates'][0]['content']['parts'][0]['text']

def transcribe_with_whisper(api_key, audio_data, language):
    headers = {'Authorization': f'Bearer {api_key}'}
    files = {'file': ('audio.webm', audio_data, 'audio/webm')}
    data = {'model': 'whisper-1', 'language': language}
    response = requests.post('https://api.openai.com/v1/audio/transcriptions',
                           headers=headers, files=files, data=data, timeout=60)
    if response.status_code != 200:
        return jsonify({'error': f'Whisper Error: {response.text}'}), response.status_code
    return jsonify({'text': response.json()['text']})

def transcribe_with_gemini(api_key, audio_data, language):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={api_key}"
    audio_base64 = base64.b64encode(audio_data).decode('utf-8')
    payload = {
        'contents': [{
            'parts': [
                {'text': f'Transkribiere diese Audio-Aufnahme auf {language}. Gib NUR den transkribierten Text zurück.'},
                {'inline_data': {'mime_type': 'audio/webm', 'data': audio_base64}}
            ]
        }]
    }
    response = requests.post(url, json=payload, timeout=60)
    if response.status_code != 200:
        return jsonify({'error': f'Gemini Error: {response.text}'}), response.status_code
    text = response.json()['candidates'][0]['content']['parts'][0]['text']
    return jsonify({'text': text})

# =============================================================================
# CACHE STATS
# =============================================================================

@app.route('/api/cache-stats')
def cache_stats():
    """Cache-Statistiken (für Monitoring)."""
    with cache_lock:
        tts_size = len(tts_cache)
    with translation_cache_lock:
        translation_size = len(translation_cache)
    with sessions_lock:
        session_count = len(sessions)
    
    return jsonify({
        'tts_cache': {'size': tts_size, 'max': MAX_CACHE_SIZE},
        'translation_cache': {'size': translation_size, 'max': MAX_TRANSLATION_CACHE_SIZE},
        'active_sessions': session_count
    })

# =============================================================================
# RUN
# =============================================================================

# Start cleanup thread only once
_cleanup_started = False

def start_cleanup_if_needed():
    global _cleanup_started
    if not _cleanup_started:
        _cleanup_started = True
        cleanup_thread.start()

# For gunicorn with eventlet
start_cleanup_if_needed()

if __name__ == '__main__':
    socketio.run(app, debug=True, host='0.0.0.0', port=5000)
