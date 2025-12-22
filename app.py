"""
LeseAssistent - Flask Backend (BYOK Proxy)
==========================================
Ein "dummer" Proxy, der API-Keys vom Frontend entgegennimmt und
an die jeweiligen APIs weiterleitet. Speichert NICHTS!

Die Keys bleiben im localStorage des Lehrers, werden aber nie
im Browser-Network-Tab sichtbar, da alle Calls über dieses Backend laufen.
"""

from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
import requests
import os
import json
import base64
import hashlib
import re
from io import BytesIO
from collections import OrderedDict
import threading

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
CORS(app)  # Erlaubt Cross-Origin Requests

# =============================================================================
# TTS AUDIO CACHE (In-Memory)
# =============================================================================
# Speichert generierte Audio-Daten um ElevenLabs API-Calls zu sparen
# Key: hash(text + voice_id), Value: API response JSON
# Begrenzt auf MAX_CACHE_SIZE Einträge (FIFO)

MAX_CACHE_SIZE = 500  # Max 500 verschiedene Audios im Cache
tts_cache = OrderedDict()
cache_lock = threading.Lock()

# =============================================================================
# TRANSLATION CACHE (In-Memory)
# =============================================================================
# Speichert Übersetzungen um KI-API-Calls zu sparen
# Key: hash(text + target_language), Value: translated text

MAX_TRANSLATION_CACHE_SIZE = 1000  # Mehr Platz da Text kleiner als Audio
translation_cache = OrderedDict()
translation_cache_lock = threading.Lock()

def get_cache_key(text, voice_id):
    """Erzeugt einen eindeutigen Cache-Key aus Text und Voice ID."""
    content = f"{text}|{voice_id}"
    return hashlib.md5(content.encode('utf-8')).hexdigest()

def get_translation_cache_key(text, target_language):
    """Erzeugt einen eindeutigen Cache-Key aus Text und Zielsprache."""
    content = f"{text}|{target_language}"
    return hashlib.md5(content.encode('utf-8')).hexdigest()

def get_from_cache(cache_key):
    """Holt Audio aus dem Cache (thread-safe)."""
    with cache_lock:
        if cache_key in tts_cache:
            # Move to end (LRU)
            tts_cache.move_to_end(cache_key)
            return tts_cache[cache_key]
    return None

def add_to_cache(cache_key, data):
    """Fügt Audio zum Cache hinzu (thread-safe)."""
    with cache_lock:
        if cache_key in tts_cache:
            tts_cache.move_to_end(cache_key)
        else:
            if len(tts_cache) >= MAX_CACHE_SIZE:
                # Remove oldest entry
                tts_cache.popitem(last=False)
            tts_cache[cache_key] = data

def get_from_translation_cache(cache_key):
    """Holt Übersetzung aus dem Cache (thread-safe)."""
    with translation_cache_lock:
        if cache_key in translation_cache:
            translation_cache.move_to_end(cache_key)
            return translation_cache[cache_key]
    return None

def add_to_translation_cache(cache_key, translated_text):
    """Fügt Übersetzung zum Cache hinzu (thread-safe)."""
    with translation_cache_lock:
        if cache_key in translation_cache:
            translation_cache.move_to_end(cache_key)
        else:
            if len(translation_cache) >= MAX_TRANSLATION_CACHE_SIZE:
                translation_cache.popitem(last=False)
            translation_cache[cache_key] = translated_text

# =============================================================================
# TEXT CLEANUP UTILITIES
# =============================================================================

def cleanup_extracted_text(text):
    """
    Bereinigt aus PDFs/DOCX extrahierten Text.
    - Fügt Silbentrennungen wieder zusammen (Haus-\\ngaben -> Hausgaben)
    - Normalisiert Whitespace
    - Entfernt übermäßige Leerzeilen
    """
    if not text:
        return text
    
    # Silbentrennung am Zeilenende zusammenfügen
    # Muster: Wort- \n nächstes -> Wortnächstes
    text = re.sub(r'(\w)-\s*\n\s*(\w)', r'\1\2', text)
    
    # Auch mit optionalem Leerzeichen: "Haus- gaben" -> "Hausgaben"
    text = re.sub(r'(\w)-\s+(\w)', r'\1\2', text)
    
    # Einfache Zeilenumbrüche (ohne Absatz) in Leerzeichen umwandeln
    # Aber doppelte Zeilenumbrüche (Absätze) beibehalten
    text = re.sub(r'(?<!\n)\n(?!\n)', ' ', text)
    
    # Mehrfache Leerzeichen normalisieren
    text = re.sub(r' +', ' ', text)
    
    # Mehr als 2 Zeilenumbrüche auf 2 reduzieren
    text = re.sub(r'\n{3,}', '\n\n', text)
    
    return text.strip()

# =============================================================================
# FRONTEND ROUTE
# =============================================================================

@app.route('/')
def index():
    """Serve the main application."""
    return render_template('index.html')


@app.route('/aufgaben')
def aufgaben():
    """Serve the tasks/exercises page."""
    return render_template('aufgaben.html')


@app.route('/nachsprechen')
def nachsprechen():
    """Serve the repeat-after-me speaking practice page."""
    return render_template('nachsprechen.html')


# =============================================================================
# FILE UPLOAD & TEXT EXTRACTION
# =============================================================================

@app.route('/api/extract-text', methods=['POST'])
def extract_text_from_file():
    """
    Extrahiert Text aus hochgeladenen DOCX oder PDF Dateien.
    
    Erwartet: multipart/form-data mit 'file' Feld
    """
    try:
        if 'file' not in request.files:
            return jsonify({'error': 'Keine Datei hochgeladen'}), 400
        
        file = request.files['file']
        filename = file.filename.lower()
        
        if filename.endswith('.docx'):
            if not DOCX_AVAILABLE:
                return jsonify({'error': 'DOCX-Verarbeitung nicht verfügbar'}), 500
            
            # DOCX verarbeiten
            doc = Document(BytesIO(file.read()))
            paragraphs = [para.text for para in doc.paragraphs if para.text.strip()]
            text = '\n\n'.join(paragraphs)
            
        elif filename.endswith('.pdf'):
            if not PDF_AVAILABLE:
                return jsonify({'error': 'PDF-Verarbeitung nicht verfügbar'}), 500
            
            # PDF verarbeiten
            text_parts = []
            with pdfplumber.open(BytesIO(file.read())) as pdf:
                for page in pdf.pages:
                    page_text = page.extract_text()
                    if page_text:
                        text_parts.append(page_text)
            text = '\n\n'.join(text_parts)
            
        elif filename.endswith('.txt'):
            # Einfache Textdatei
            text = file.read().decode('utf-8')
            
        else:
            return jsonify({'error': 'Nicht unterstütztes Dateiformat. Erlaubt: .docx, .pdf, .txt'}), 400
        
        # Text bereinigen (Silbentrennung etc.)
        text = cleanup_extracted_text(text)
        
        if not text.strip():
            return jsonify({'error': 'Kein Text in der Datei gefunden'}), 400
        
        return jsonify({'text': text.strip()})
        
    except Exception as e:
        return jsonify({'error': f'Fehler beim Verarbeiten: {str(e)}'}), 500


@app.route('/api/ocr', methods=['POST'])
def ocr_image():
    """
    OCR via KI-API - extrahiert Text aus Bildern.
    
    Erwartet JSON:
    {
        "api_key": "...",
        "provider": "openai" | "anthropic" | "google",
        "image": "base64-encoded image data",
        "mime_type": "image/jpeg" | "image/png" | etc.
    }
    """
    try:
        data = request.json
        
        api_key = data.get('api_key')
        provider = data.get('provider', 'openai')
        image_base64 = data.get('image')
        mime_type = data.get('mime_type', 'image/jpeg')
        
        if not api_key:
            return jsonify({'error': 'API Key fehlt'}), 400
        if not image_base64:
            return jsonify({'error': 'Kein Bild übermittelt'}), 400
        
        ocr_prompt = """Extrahiere den gesamten Text aus diesem Bild. 
Gib NUR den erkannten Text zurück, ohne Erklärungen oder Formatierungshinweise.
Behalte Absätze und Zeilenumbrüche bei, wo sie im Original erkennbar sind.
Wenn kein Text erkennbar ist, antworte mit: [KEIN TEXT ERKANNT]"""
        
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


def call_openai_vision(api_key, prompt, image_base64, mime_type):
    """OpenAI GPT-4o Vision API Call"""
    headers = {
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/json'
    }
    
    payload = {
        'model': 'gpt-4o',
        'messages': [
            {
                'role': 'user',
                'content': [
                    {'type': 'text', 'text': prompt},
                    {
                        'type': 'image_url',
                        'image_url': {
                            'url': f'data:{mime_type};base64,{image_base64}'
                        }
                    }
                ]
            }
        ],
        'max_tokens': 4096
    }
    
    response = requests.post(
        'https://api.openai.com/v1/chat/completions',
        headers=headers,
        json=payload,
        timeout=60
    )
    
    if response.status_code != 200:
        raise Exception(f"OpenAI Error: {response.text}")
    
    return response.json()['choices'][0]['message']['content']


def call_anthropic_vision(api_key, prompt, image_base64, mime_type):
    """Anthropic Claude Vision API Call"""
    headers = {
        'x-api-key': api_key,
        'Content-Type': 'application/json',
        'anthropic-version': '2023-06-01'
    }
    
    payload = {
        'model': 'claude-sonnet-4-20250514',
        'max_tokens': 4096,
        'messages': [
            {
                'role': 'user',
                'content': [
                    {
                        'type': 'image',
                        'source': {
                            'type': 'base64',
                            'media_type': mime_type,
                            'data': image_base64
                        }
                    },
                    {
                        'type': 'text',
                        'text': prompt
                    }
                ]
            }
        ]
    }
    
    response = requests.post(
        'https://api.anthropic.com/v1/messages',
        headers=headers,
        json=payload,
        timeout=60
    )
    
    if response.status_code != 200:
        raise Exception(f"Anthropic Error: {response.text}")
    
    return response.json()['content'][0]['text']


def call_google_vision(api_key, prompt, image_base64, mime_type):
    """Google Gemini Vision API Call"""
    url = f'https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={api_key}'
    
    payload = {
        'contents': [
            {
                'parts': [
                    {'text': prompt},
                    {
                        'inline_data': {
                            'mime_type': mime_type,
                            'data': image_base64
                        }
                    }
                ]
            }
        ]
    }
    
    response = requests.post(url, json=payload, timeout=60)
    
    if response.status_code != 200:
        raise Exception(f"Google Error: {response.text}")
    
    return response.json()['candidates'][0]['content']['parts'][0]['text']


# =============================================================================
# ELEVENLABS TTS PROXY
# =============================================================================

@app.route('/api/tts', methods=['POST'])
def proxy_tts():
    """
    Proxy für ElevenLabs Text-to-Speech mit Timestamps.
    Mit IN-MEMORY CACHING um API-Kosten zu sparen!
    
    Erwartet JSON:
    {
        "api_key": "sk_...",
        "voice_id": "21m00Tcm4TlvDq8ikWAM",
        "text": "Der zu sprechende Text...",
        "model_id": "eleven_multilingual_v2"  (optional)
    }
    """
    try:
        data = request.json
        
        # Validierung
        api_key = data.get('api_key')
        voice_id = data.get('voice_id')
        text = data.get('text')
        
        if not api_key:
            return jsonify({'error': 'API Key fehlt'}), 400
        if not voice_id:
            return jsonify({'error': 'Voice ID fehlt'}), 400
        if not text:
            return jsonify({'error': 'Text fehlt'}), 400
        
        # =========================================
        # CACHING: Check if we already have this audio
        # =========================================
        cache_key = get_cache_key(text, voice_id)
        cached_response = get_from_cache(cache_key)
        
        if cached_response:
            # Cache Hit! Return cached audio without API call
            app.logger.info(f"TTS Cache HIT for key {cache_key[:8]}...")
            return jsonify(cached_response)
        
        # Cache Miss - need to call ElevenLabs
        app.logger.info(f"TTS Cache MISS for key {cache_key[:8]}... calling API")
        
        # ElevenLabs API Call
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/with-timestamps"
        
        headers = {
            'xi-api-key': api_key,
            'Content-Type': 'application/json'
        }
        
        payload = {
            'text': text,
            'model_id': data.get('model_id', 'eleven_multilingual_v2'),
            'voice_settings': {
                'stability': data.get('stability', 0.5),
                'similarity_boost': data.get('similarity_boost', 0.75)
            }
        }
        
        response = requests.post(url, headers=headers, json=payload, timeout=60)
        
        if response.status_code != 200:
            error_detail = response.json().get('detail', {})
            error_msg = error_detail.get('message', 'ElevenLabs API Fehler')
            return jsonify({'error': error_msg}), response.status_code
        
        # Erfolgreiche Antwort - im Cache speichern
        response_data = response.json()
        add_to_cache(cache_key, response_data)
        
        return jsonify(response_data)
        
    except requests.exceptions.Timeout:
        return jsonify({'error': 'Timeout - Die Anfrage hat zu lange gedauert'}), 504
    except requests.exceptions.RequestException as e:
        return jsonify({'error': f'Verbindungsfehler: {str(e)}'}), 502
    except Exception as e:
        return jsonify({'error': f'Serverfehler: {str(e)}'}), 500


@app.route('/api/cache-stats', methods=['GET'])
def cache_stats():
    """Gibt Cache-Statistiken zurück (für Debugging/Monitoring)."""
    with cache_lock:
        tts_size = len(tts_cache)
    with translation_cache_lock:
        translation_size = len(translation_cache)
    
    return jsonify({
        'tts_cache': {
            'size': tts_size,
            'max_size': MAX_CACHE_SIZE
        },
        'translation_cache': {
            'size': translation_size,
            'max_size': MAX_TRANSLATION_CACHE_SIZE
        }
    })


@app.route('/api/voices', methods=['POST'])
def proxy_voices():
    """
    Proxy zum Abrufen der verfügbaren ElevenLabs Stimmen.
    
    Erwartet JSON:
    {
        "api_key": "sk_..."
    }
    """


@app.route('/api/speech-to-text', methods=['POST'])
def proxy_speech_to_text():
    """
    Proxy für Speech-to-Text (unterstützt OpenAI Whisper und Google Gemini).
    
    Erwartet: multipart/form-data mit:
    - audio: Audio-Datei (webm, mp3, wav, etc.)
    - api_key: API Key
    - provider: 'openai' oder 'google'
    - language: Sprache (optional, default: 'de')
    """
    try:
        if 'audio' not in request.files:
            return jsonify({'error': 'Keine Audio-Datei'}), 400
        
        api_key = request.form.get('api_key')
        provider = request.form.get('provider', 'openai')
        
        if not api_key:
            return jsonify({'error': 'API Key fehlt'}), 400
        
        audio_file = request.files['audio']
        language = request.form.get('language', 'de')
        audio_data = audio_file.read()
        
        if provider == 'google':
            # Google Gemini Audio Transcription
            return transcribe_with_gemini(api_key, audio_data, language)
        else:
            # OpenAI Whisper (default)
            return transcribe_with_whisper(api_key, audio_data, audio_file.filename, audio_file.content_type, language)
        
    except requests.exceptions.Timeout:
        return jsonify({'error': 'Timeout bei Spracherkennung'}), 504
    except Exception as e:
        return jsonify({'error': f'Fehler: {str(e)}'}), 500


def transcribe_with_whisper(api_key, audio_data, filename, content_type, language):
    """OpenAI Whisper Transcription"""
    headers = {
        'Authorization': f'Bearer {api_key}'
    }
    
    files = {
        'file': (filename or 'audio.webm', audio_data, content_type or 'audio/webm'),
        'model': (None, 'whisper-1'),
        'language': (None, language),
        'response_format': (None, 'json')
    }
    
    response = requests.post(
        'https://api.openai.com/v1/audio/transcriptions',
        headers=headers,
        files=files,
        timeout=30
    )
    
    if response.status_code != 200:
        error_msg = response.json().get('error', {}).get('message', 'Whisper API Fehler')
        return jsonify({'error': error_msg}), response.status_code
    
    result = response.json()
    return jsonify({
        'text': result.get('text', '').strip(),
        'success': True
    })


def transcribe_with_gemini(api_key, audio_data, language):
    """Google Gemini Audio Transcription"""
    import base64
    
    audio_base64 = base64.b64encode(audio_data).decode('utf-8')
    
    url = f'https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={api_key}'
    
    payload = {
        'contents': [
            {
                'parts': [
                    {
                        'text': f'Transkribiere das folgende Audio auf Deutsch. Gib NUR den transkribierten Text zurück, ohne Erklärungen oder Formatierung. Wenn kein verständlicher Text zu hören ist, antworte mit: [KEINE SPRACHE ERKANNT]'
                    },
                    {
                        'inline_data': {
                            'mime_type': 'audio/webm',
                            'data': audio_base64
                        }
                    }
                ]
            }
        ]
    }
    
    response = requests.post(url, json=payload, timeout=30)
    
    if response.status_code != 200:
        error_detail = response.json().get('error', {}).get('message', 'Gemini API Fehler')
        return jsonify({'error': error_detail}), response.status_code
    
    result = response.json()
    try:
        text = result['candidates'][0]['content']['parts'][0]['text'].strip()
        
        if '[KEINE SPRACHE ERKANNT]' in text:
            return jsonify({'text': '', 'success': True})
        
        return jsonify({
            'text': text,
            'success': True
        })
    except (KeyError, IndexError) as e:
        return jsonify({'error': 'Unerwartetes Antwortformat von Gemini'}), 500
        
        if not api_key:
            return jsonify({'error': 'API Key fehlt'}), 400
        
        response = requests.get(
            'https://api.elevenlabs.io/v1/voices',
            headers={'xi-api-key': api_key},
            timeout=10
        )
        
        if response.status_code != 200:
            return jsonify({'error': 'Ungültiger API Key oder Verbindungsfehler'}), response.status_code
        
        return jsonify(response.json())
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# =============================================================================
# ÜBERSETZUNG PROXY
# =============================================================================

@app.route('/api/translate', methods=['POST'])
def proxy_translate():
    """
    Proxy für Textübersetzung via KI.
    Mit IN-MEMORY CACHING um API-Kosten zu sparen!
    
    Erwartet JSON:
    {
        "api_key": "sk_...",
        "provider": "openai" | "anthropic" | "google",
        "text": "Der zu übersetzende Text...",
        "target_language": "tr" | "bg" | "de"
    }
    """
    try:
        data = request.json
        
        api_key = data.get('api_key')
        provider = data.get('provider', 'openai')
        text = data.get('text')
        target_language = data.get('target_language', 'de')
        
        if not api_key:
            return jsonify({'error': 'API Key fehlt'}), 400
        if not text:
            return jsonify({'error': 'Text fehlt'}), 400
        
        # Sprachnamen für den Prompt
        language_names = {
            'de': 'Deutsch',
            'tr': 'Türkisch',
            'bg': 'Bulgarisch',
            'ar': 'Arabisch',
            'uk': 'Ukrainisch',
            'en': 'Englisch'
        }
        
        target_name = language_names.get(target_language, 'Deutsch')
        
        # Wenn Zielsprache Deutsch ist, keine Übersetzung nötig
        if target_language == 'de':
            return jsonify({'translated_text': text})
        
        # =========================================
        # CACHING: Check if we already have this translation
        # =========================================
        cache_key = get_translation_cache_key(text, target_language)
        cached_translation = get_from_translation_cache(cache_key)
        
        if cached_translation:
            # Cache Hit! Return cached translation
            app.logger.info(f"Translation Cache HIT for {target_language}, key {cache_key[:8]}...")
            return jsonify({'translated_text': cached_translation, 'cached': True})
        
        # Cache Miss - need to call AI API
        app.logger.info(f"Translation Cache MISS for {target_language}, key {cache_key[:8]}... calling API")
        
        system_prompt = f"""Du bist ein professioneller Übersetzer. Übersetze den folgenden Text ins {target_name}.

Wichtige Regeln:
- Übersetze NUR den Text, füge keine Erklärungen hinzu
- Behalte die Formatierung und Absätze bei
- Übersetze natürlich und flüssig, nicht wörtlich
- Antworte NUR mit der Übersetzung, nichts anderes"""

        user_message = text
        
        # Je nach Provider unterschiedliche API aufrufen
        if provider == 'openai':
            result = call_openai_text(api_key, system_prompt, user_message)
        elif provider == 'anthropic':
            result = call_anthropic_text(api_key, system_prompt, user_message)
        elif provider == 'google':
            result = call_google_text(api_key, system_prompt, user_message)
        else:
            return jsonify({'error': f'Unbekannter Provider: {provider}'}), 400
        
        # Cache the translation
        add_to_translation_cache(cache_key, result)
        
        return jsonify({'translated_text': result})
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def call_openai_text(api_key, system_prompt, user_message):
    """OpenAI API für Textantwort aufrufen."""
    response = requests.post(
        'https://api.openai.com/v1/chat/completions',
        headers={
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json'
        },
        json={
            'model': 'gpt-4o-mini',
            'messages': [
                {'role': 'system', 'content': system_prompt},
                {'role': 'user', 'content': user_message}
            ],
            'temperature': 0.3
        },
        timeout=30
    )
    
    if response.status_code != 200:
        error = response.json().get('error', {})
        raise Exception(error.get('message', 'OpenAI API Fehler'))
    
    return response.json()['choices'][0]['message']['content'].strip()


def call_anthropic_text(api_key, system_prompt, user_message):
    """Anthropic Claude API für Textantwort aufrufen."""
    response = requests.post(
        'https://api.anthropic.com/v1/messages',
        headers={
            'x-api-key': api_key,
            'Content-Type': 'application/json',
            'anthropic-version': '2023-06-01'
        },
        json={
            'model': 'claude-3-haiku-20240307',
            'max_tokens': 4096,
            'system': system_prompt,
            'messages': [
                {'role': 'user', 'content': user_message}
            ]
        },
        timeout=30
    )
    
    if response.status_code != 200:
        error = response.json().get('error', {})
        raise Exception(error.get('message', 'Anthropic API Fehler'))
    
    return response.json()['content'][0]['text'].strip()


def call_google_text(api_key, system_prompt, user_message):
    """Google Gemini API für Textantwort aufrufen."""
    response = requests.post(
        f'https://generativelanguage.googleapis.com/v1beta/models/gemini-3-pro-preview:generateContent?key={api_key}',
        headers={'Content-Type': 'application/json'},
        json={
            'contents': [{
                'parts': [{
                    'text': f'{system_prompt}\n\n{user_message}'
                }]
            }],
            'generationConfig': {
                'temperature': 0.3
            }
        },
        timeout=30
    )
    
    if response.status_code != 200:
        error = response.json().get('error', {})
        raise Exception(error.get('message', 'Google AI API Fehler'))
    
    return response.json()['candidates'][0]['content']['parts'][0]['text'].strip()


# =============================================================================
# KI PROXY (OpenAI, Anthropic, Google)
# =============================================================================

@app.route('/api/generate-questions', methods=['POST'])
def proxy_generate_questions():
    """
    Proxy für KI-basierte Fragengenerierung.
    
    Erwartet JSON:
    {
        "api_key": "sk_...",
        "provider": "openai" | "anthropic" | "google",
        "text": "Der zu analysierende Text...",
        "difficulty": "einfach" | "mittel" | "schwer" | "oberstufe"
    }
    """
    try:
        data = request.json
        
        api_key = data.get('api_key')
        provider = data.get('provider', 'openai')
        text = data.get('text')
        difficulty = data.get('difficulty', 'mittel')
        
        if not api_key:
            return jsonify({'error': 'API Key fehlt'}), 400
        if not text:
            return jsonify({'error': 'Text fehlt'}), 400
        
        # System Prompt erstellen
        difficulty_prompts = {
            'einfach': 'einfache Fragen für Schüler der Klassen 5-6. Verwende einfache Sprache und teste grundlegendes Textverständnis.',
            'mittel': 'Fragen mittlerer Schwierigkeit für Klassen 7-8. Teste sowohl Textverständnis als auch einfache Schlussfolgerungen.',
            'schwer': 'anspruchsvolle Fragen für Klassen 9-10. Fordere kritisches Denken und tieferes Verständnis.',
            'oberstufe': 'komplexe Fragen für die Oberstufe (Klassen 11-12). Erfordere Analyse, Interpretation und Transfer.'
        }
        
        system_prompt = f"""Du bist ein erfahrener Pädagoge, der Leseverständnis-Aufgaben erstellt. 
Erstelle basierend auf dem gegebenen Text genau 3 Verständnisfragen und 2 weiterführende Aufgaben.

Schwierigkeitsstufe: {difficulty_prompts.get(difficulty, difficulty_prompts['mittel'])}

Formatiere deine Antwort als JSON-Array mit folgendem Schema:
[
  {{"type": "frage", "text": "Die Frage..."}},
  {{"type": "frage", "text": "Weitere Frage..."}},
  {{"type": "frage", "text": "Dritte Frage..."}},
  {{"type": "aufgabe", "text": "Eine Aufgabe..."}},
  {{"type": "aufgabe", "text": "Weitere Aufgabe..."}}
]

Antworte NUR mit dem JSON-Array, keine weiteren Erklärungen."""

        user_message = f"Text zum Analysieren:\n\n{text}"
        
        # Je nach Provider unterschiedliche API aufrufen
        if provider == 'openai':
            result = call_openai(api_key, system_prompt, user_message)
        elif provider == 'anthropic':
            result = call_anthropic(api_key, system_prompt, user_message)
        elif provider == 'google':
            result = call_google(api_key, system_prompt, user_message)
        else:
            return jsonify({'error': f'Unbekannter Provider: {provider}'}), 400
        
        return jsonify({'questions': result})
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/generate-tasks', methods=['POST'])
def proxy_generate_tasks():
    """
    Proxy für KI-basierte interaktive Aufgabengenerierung (für iPad-optimierte Seite).
    
    Generiert verschiedene Aufgabentypen:
    - Multiple Choice
    - Richtig/Falsch
    - Offene Fragen
    """
    try:
        data = request.json
        
        api_key = data.get('api_key')
        provider = data.get('provider', 'openai')
        text = data.get('text')
        difficulty = data.get('difficulty', 'mittel')
        
        if not api_key:
            return jsonify({'error': 'API Key fehlt'}), 400
        if not text:
            return jsonify({'error': 'Text fehlt'}), 400
        
        difficulty_descriptions = {
            'einfach': 'Klasse 5-6, einfache Sprache, grundlegendes Textverständnis',
            'mittel': 'Klasse 7-8, Textverständnis und einfache Schlussfolgerungen',
            'schwer': 'Klasse 9-10, kritisches Denken und tieferes Verständnis',
            'oberstufe': 'Klasse 11-12, Analyse, Interpretation und Transfer'
        }
        
        system_prompt = f"""Du bist ein erfahrener Pädagoge. Erstelle interaktive Aufgaben zum Text.

Schwierigkeitsstufe: {difficulty_descriptions.get(difficulty, difficulty_descriptions['mittel'])}

Erstelle GENAU 6 Aufgaben in diesem Format als JSON-Array:
- 3 Multiple-Choice-Fragen (4 Antwortmöglichkeiten, eine richtig)
- 2 Richtig/Falsch-Aussagen
- 1 offene Frage

JSON-Schema:
[
  {{
    "type": "frage",
    "taskType": "multiple_choice",
    "question": "Die Frage...",
    "options": ["Option A", "Option B", "Option C", "Option D"],
    "correctAnswer": "Die richtige Option (exakter Text)"
  }},
  {{
    "type": "frage",
    "taskType": "true_false",
    "question": "Eine Aussage zum Text...",
    "correctAnswer": true
  }},
  {{
    "type": "aufgabe",
    "taskType": "open",
    "question": "Eine offene Frage, die zum Nachdenken anregt..."
  }}
]

WICHTIG:
- Bei Multiple Choice muss "correctAnswer" EXAKT einer der Texte aus "options" sein
- Bei Richtig/Falsch muss "correctAnswer" true oder false sein (boolean, nicht String)
- Mische die Reihenfolge der Aufgabentypen
- Antworte NUR mit dem JSON-Array"""

        user_message = f"Text:\n\n{text}"
        
        if provider == 'openai':
            result = call_openai(api_key, system_prompt, user_message)
        elif provider == 'anthropic':
            result = call_anthropic(api_key, system_prompt, user_message)
        elif provider == 'google':
            result = call_google(api_key, system_prompt, user_message)
        else:
            return jsonify({'error': f'Unbekannter Provider: {provider}'}), 400
        
        return jsonify({'tasks': result})
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def call_openai(api_key, system_prompt, user_message):
    """OpenAI API aufrufen."""
    response = requests.post(
        'https://api.openai.com/v1/chat/completions',
        headers={
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json'
        },
        json={
            'model': 'gpt-4o-mini',
            'messages': [
                {'role': 'system', 'content': system_prompt},
                {'role': 'user', 'content': user_message}
            ],
            'temperature': 0.7
        },
        timeout=30
    )
    
    if response.status_code != 200:
        error = response.json().get('error', {})
        raise Exception(error.get('message', 'OpenAI API Fehler'))
    
    content = response.json()['choices'][0]['message']['content']
    
    # JSON extrahieren (falls in Markdown-Blöcken)
    import json
    import re
    json_match = re.search(r'\[[\s\S]*\]', content)
    if json_match:
        return json.loads(json_match.group())
    return json.loads(content)


def call_anthropic(api_key, system_prompt, user_message):
    """Anthropic Claude API aufrufen."""
    response = requests.post(
        'https://api.anthropic.com/v1/messages',
        headers={
            'x-api-key': api_key,
            'Content-Type': 'application/json',
            'anthropic-version': '2023-06-01'
        },
        json={
            'model': 'claude-3-haiku-20240307',
            'max_tokens': 1024,
            'system': system_prompt,
            'messages': [
                {'role': 'user', 'content': user_message}
            ]
        },
        timeout=30
    )
    
    if response.status_code != 200:
        error = response.json().get('error', {})
        raise Exception(error.get('message', 'Anthropic API Fehler'))
    
    content = response.json()['content'][0]['text']
    
    import json
    import re
    json_match = re.search(r'\[[\s\S]*\]', content)
    if json_match:
        return json.loads(json_match.group())
    return json.loads(content)


def call_google(api_key, system_prompt, user_message):
    """Google Gemini API aufrufen."""
    # Aktuelles Modell: gemini-3-pro-preview (Stand Dezember 2025)
    # Alternative: gemini-3-flash-preview (schneller, günstiger)
    response = requests.post(
        f'https://generativelanguage.googleapis.com/v1beta/models/gemini-3-pro-preview:generateContent?key={api_key}',
        headers={'Content-Type': 'application/json'},
        json={
            'contents': [{
                'parts': [{
                    'text': f'{system_prompt}\n\n{user_message}'
                }]
            }],
            'generationConfig': {
                'temperature': 0.7
            }
        },
        timeout=30
    )
    
    if response.status_code != 200:
        error = response.json().get('error', {})
        raise Exception(error.get('message', 'Google AI API Fehler'))
    
    content = response.json()['candidates'][0]['content']['parts'][0]['text']
    
    import json
    import re
    json_match = re.search(r'\[[\s\S]*\]', content)
    if json_match:
        return json.loads(json_match.group())
    return json.loads(content)


# =============================================================================
# HEALTH CHECK (für Render.com)
# =============================================================================

@app.route('/health')
def health():
    """Health check endpoint für Monitoring."""
    return jsonify({'status': 'healthy', 'service': 'LeseAssistent'})


# =============================================================================
# MAIN
# =============================================================================

def generate_self_signed_cert(cert_file='cert.pem', key_file='key.pem'):
    """Generate self-signed certificate using Python cryptography library."""
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization
    import datetime
    
    # Generate private key
    key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )
    
    # Generate certificate
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, u"LeseAssistent Local"),
    ])
    
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.utcnow())
        .not_valid_after(datetime.datetime.utcnow() + datetime.timedelta(days=365))
        .add_extension(
            x509.SubjectAlternativeName([
                x509.DNSName(u"localhost"),
                x509.IPAddress(ipaddress.IPv4Address(u"127.0.0.1")),
            ]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    
    # Write certificate
    with open(cert_file, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    
    # Write private key
    with open(key_file, "wb") as f:
        f.write(key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption()
        ))
    
    print("✅ Zertifikat erstellt!")


if __name__ == '__main__':
    import ipaddress
    
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'
    use_https = os.environ.get('USE_HTTPS', 'false').lower() == 'true'
    
    if use_https:
        cert_file = 'cert.pem'
        key_file = 'key.pem'
        
        if not os.path.exists(cert_file) or not os.path.exists(key_file):
            print("🔐 Generiere selbstsigniertes Zertifikat...")
            try:
                generate_self_signed_cert(cert_file, key_file)
            except ImportError:
                print("❌ Bitte installiere: pip install cryptography")
                print("   Dann nochmal starten.")
                exit(1)
        
        print(f"\n🔒 HTTPS Server startet auf Port {port}")
        print(f"   → Öffne: https://DEINE-IP:{port}")
        print(f"   ⚠️  Browser-Warnung mit 'Trotzdem fortfahren' bestätigen!\n")
        
        app.run(host='0.0.0.0', port=port, debug=debug, ssl_context=(cert_file, key_file))
    else:
        print(f"\n🌐 HTTP Server startet auf Port {port}")
        print(f"   → Für HTTPS: USE_HTTPS=true python app.py\n")
        app.run(host='0.0.0.0', port=port, debug=debug)
