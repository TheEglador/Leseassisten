# 📖 LeseAssistent

**Texte verstehen mit synchronisiertem Vorlesen**

Ein Tool für Lehrkräfte, das Texte mit KI-generierter Sprachausgabe vorliest und dabei Wort für Wort synchron markiert. Zusätzlich können KI-basierte Verständnisfragen zum Text generiert werden.

## 🔑 BYOK - Bring Your Own Key

Dieses Projekt folgt dem **Bring Your Own Key** Konzept:
- Jeder Nutzer verwendet seine eigenen API-Keys
- Keys werden nur lokal im Browser gespeichert
- Keine zentralen Kosten oder Datenschutz-Probleme
- **Schüler können die Keys nicht sehen** (alle API-Calls laufen über den Proxy-Server)

## ✨ Features

- **Synchronisiertes Vorlesen**: Text wird Wort für Wort markiert während er vorgelesen wird
- **Hochwertige Stimmen**: ElevenLabs Multilingual TTS mit verschiedenen Stimmen
- **KI-Fragengenerierung**: Automatische Erstellung von Verständnisfragen und Aufgaben
- **4 Schwierigkeitsstufen**: Von Klasse 5-6 bis Oberstufe
- **Flexible KI-Auswahl**: OpenAI, Anthropic (Claude) oder Google (Gemini)
- **Geschwindigkeitskontrolle**: Wiedergabegeschwindigkeit von 0.5x bis 1.5x

## 🚀 Deployment auf Render.com

### Schritt 1: Repository erstellen

```bash
# Neues Git-Repository initialisieren
git init
git add .
git commit -m "Initial commit"

# Auf GitHub pushen
git remote add origin https://github.com/DEIN-USERNAME/leseassistent.git
git push -u origin main
```

### Schritt 2: Auf Render.com deployen

1. Gehe zu [render.com](https://render.com) und logge dich ein
2. Klicke auf **"New +"** → **"Web Service"**
3. Verbinde dein GitHub Repository
4. Render erkennt automatisch die `render.yaml` Konfiguration
5. Klicke auf **"Create Web Service"**

Das war's! Nach 2-3 Minuten ist deine App live unter `https://leseassistent.onrender.com` (oder ähnlich).

## 🔧 Lokale Entwicklung

```bash
# Repository klonen
git clone https://github.com/DEIN-USERNAME/leseassistent.git
cd leseassistent

# Virtual Environment erstellen
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# Dependencies installieren
pip install -r requirements.txt

# Server starten
python app.py
```

Öffne dann http://localhost:5000 im Browser.

## ✨ Features

- **📄 Datei-Upload** – DOCX, PDF, TXT oder Fotos von Texten hochladen
- **📷 KI-basierte Texterkennung (OCR)** – Fotos von Texten werden automatisch erkannt
- **🔊 Synchronisiertes Vorlesen** – Wort-für-Wort Highlighting während des Vorlesens
- **🌍 Mehrsprachig** – Texte auf Deutsch, Türkisch oder Bulgarisch vorlesen
- **📝 Interaktive Aufgaben** – KI-generierte Verständnisfragen (Multiple Choice, Richtig/Falsch, Offene Fragen)
- **🎤 Nachsprech-Modus** – Satz-für-Satz Aussprache üben (ideal für IVK/DaZ)
- **♿ Barrierefreiheit** – OpenDyslexic Font, einstellbarer Zeilen-/Zeichenabstand, Fokus-Modus
- **🎓 Schwierigkeitsstufen** – Von Klasse 5 bis Oberstufe
- **🔐 BYOK-Sicherheit** – API-Keys bleiben beim Lehrer, nie im Backend gespeichert

## 📁 Projektstruktur

```
leseassistent/
├── app.py              # Flask Backend (Proxy-Server)
├── requirements.txt    # Python Dependencies
├── render.yaml         # Render.com Deployment Config
├── .gitignore
├── README.md
├── static/             # Statische Dateien
│   ├── d.png           # Deutsche Flagge
│   ├── t.png           # Türkische Flagge
│   └── b.png           # Bulgarische Flagge
└── templates/
    ├── index.html      # Hauptseite (Text + Audio + Lesehilfen)
    ├── aufgaben.html   # Interaktive Aufgaben-Seite
    └── nachsprechen.html # Nachsprech-Übung (Satz für Satz)
```

**Hinweis:** Die Flaggen-Bilder (d.png, t.png, b.png) müssen im `static/` Ordner liegen!

## 🔐 Benötigte API-Keys

### ElevenLabs (für Text-to-Speech)
1. Registriere dich auf [elevenlabs.io](https://elevenlabs.io)
2. Gehe zu Profile Settings → API Key
3. Kopiere deinen API Key

**Kosten**: Kostenloser Tier mit 10.000 Zeichen/Monat

### KI für Fragengenerierung (eine der folgenden)

**OpenAI (GPT-4o-mini)**
- Registriere dich auf [platform.openai.com](https://platform.openai.com)
- API Key unter API Keys erstellen

**Anthropic (Claude)**
- Registriere dich auf [console.anthropic.com](https://console.anthropic.com)
- API Key erstellen

**Google (Gemini)**
- Gehe zu [aistudio.google.com](https://aistudio.google.com)
- API Key unter "Get API Key" erstellen

## 🛡️ Sicherheit

- API-Keys werden **nur im localStorage des Browsers** gespeichert
- Keys werden **pro Request** an das Backend gesendet
- Das Backend **speichert keine Keys** - es leitet sie nur weiter
- Im Browser Network-Tab sehen Schüler nur Calls zu deinem Server
- Die eigentlichen API-Keys sind **nie sichtbar**

## 📝 Lizenz

MIT License - Frei verwendbar für Bildungszwecke

---

Entwickelt für den Einsatz im Schulunterricht 🎓
