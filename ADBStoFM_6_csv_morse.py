from paho.mqtt import client as mqtt_client
import subprocess
import threading
import argparse
import serial
import queue
import time
import json
import sys
import os
import csv
import math
import wave
from datetime import datetime

# --- KONFIGURATION ---
MQTT_TOPIC   = "#"
CLIENT_ID    = "voice_gateway_pi"

PTT_PORT     = "/dev/ttyUSB0" 
AUDIO_DEVICE = "plughw:1,0" 

BASE_DIR     = "/home/pi/piper"
PIPER_BIN    = f"{BASE_DIR}/piper"
MODEL_PATH   = f"{BASE_DIR}/de_DE-thorsten-high.onnx"
OUTPUT_WAV   = f"{BASE_DIR}/alert.wav"
BEACON_WAV   = f"{BASE_DIR}/beacon.wav"

TEMP_COOLDOWN_MINUTES = 1 
AIRCRAFT_COOLDOWN_SEC = 300  

# Baken-Konfiguration
BAKEN_INTERVAL_SEC    = 180 
last_radio_activity   = time.time()

# MORSE & VOICE BEACON KONFIGURATION
MY_CALLSIGN           = "DA1TWD"  # <-- HIER DEIN CALLSIGN EINTRAGEN
MORSE_SPEED_WPM       = 15        
MORSE_FREQ_HZ         = 800       
BEACON_VOICE_TEXT     = "Automatisches Gateway meldet Betriebsbereitschaft. Aktuelle Uhrzeit:"

# Pfade
SCRIPT_DIR            = os.path.dirname(os.path.abspath(__file__))
CSV_DB_PATH           = os.path.join(SCRIPT_DIR, "aircraft.csv")

# Globaler Speicher
last_announced = {}        
message_queue = queue.Queue() 
aircraft_cache = {}  

# Hex-Zähler & Zeit Initialisierung
hex_counter = 0
last_reset_time = time.time()

MORSE_DICT = {
    'A': '.-', 'B': '-...', 'C': '-.-.', 'D': '-..', 'E': '.', 'F': '..-.', 'G': '--.', 'H': '....',
    'I': '..', 'J': '.---', 'K': '-.-', 'L': '.-..', 'M': '--', 'N': '-.', 'O': '---', 'P': '.--.',
    'Q': '--.-', 'R': '.-.', 'S': '...', 'T': '-', 'U': '..-', 'V': '...-', 'W': '.--', 'X': '-..-',
    'Y': '-.--', 'Z': '--..', '1': '.----', '2': '..---', '3': '...--', '4': '....-', '5': '.....',
    '6': '-....', '7': '--...', '8': '---..', '9': '----.', '0': '-----', ' ': ' ', ':': '---...'
}

def generate_morse_wav(text, filename):
    sample_rate = 44100
    dot_duration = 1.2 / MORSE_SPEED_WPM
    dash_duration = dot_duration * 3
    audio_data = bytearray()
    
    def add_tone(duration):
        num_samples = int(sample_rate * duration)
        for i in range(num_samples):
            value = int(32767 * math.sin(2 * math.pi * MORSE_FREQ_HZ * i / sample_rate))
            audio_data.extend(value.to_bytes(2, byteorder='little', signed=True))
            
    def add_silence(duration):
        num_samples = int(sample_rate * duration)
        for _ in range(num_samples):
            audio_data.extend((0).to_bytes(2, byteorder='little', signed=True))

    for char in text.upper():
        if char in MORSE_DICT:
            code = MORSE_DICT[char]
            if code == ' ':
                add_silence(dot_duration * 7)
            else:
                for symbol in code:
                    if symbol == '.': add_tone(dot_duration)
                    elif symbol == '-': add_tone(dash_duration)
                    add_silence(dot_duration)
                add_silence(dot_duration * 3)

    with wave.open(filename, 'wb') as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(audio_data)

def load_aircraft_database():
    global aircraft_cache
    if not os.path.exists(CSV_DB_PATH): 
        print(f"⚠️ CSV nicht gefunden: {CSV_DB_PATH}")
        return
    try:
        with open(CSV_DB_PATH, mode='r', encoding='utf-8', errors='ignore') as f:
            sample = f.read(2048); f.seek(0)
            delimiter = ';' if ';' in sample else ','
            reader = csv.reader(f, delimiter=delimiter)
            for row in reader:
                if len(row) >= 3:
                    icao = row[0].strip().upper()
                    ac_type = row[2].strip()
                    if len(ac_type) > 10 and len(row) >= 5: 
                        ac_type = row[4].strip()
                    if icao and ac_type and ac_type.lower() not in ["none", "n/a", ""]:
                        aircraft_cache[icao] = ac_type
        print(f"✅ Datenbank geladen: {len(aircraft_cache)} Einträge.")
    except Exception as e: print(f"❌ DB Fehler: {e}")

def get_next_hex_id():
    global hex_counter, last_reset_time
    if time.time() - last_reset_time >= 3600:
        hex_counter = 0; last_reset_time = time.time()
    hex_id = f"{hex_counter:04X}"
    hex_counter += 1
    return hex_id

def play_audio_to_radio(is_beacon, content, msg_id):
    global last_radio_activity
    last_radio_activity = time.time()
    try:
        # Vorbereitung: Audio berechnen BEVOR PTT aktiviert wird
        if is_beacon:
            print(f"⏳ Berechne Voice-Bake (ID {msg_id}): {content}")
            env = os.environ.copy(); env["LD_LIBRARY_PATH"] = BASE_DIR
            subprocess.run([PIPER_BIN, '--model', MODEL_PATH, '--output_file', OUTPUT_WAV], 
                           input=content.encode('utf-8'), env=env, check=True, capture_output=True)
            files_to_play = [BEACON_WAV, OUTPUT_WAV]
        else:
            print(f"⏳ Berechne Sprachalarm (ID {msg_id}): {content}")
            env = os.environ.copy(); env["LD_LIBRARY_PATH"] = BASE_DIR
            subprocess.run([PIPER_BIN, '--model', MODEL_PATH, '--output_file', OUTPUT_WAV], 
                           input=content.encode('utf-8'), env=env, check=True, capture_output=True)
            files_to_play = [OUTPUT_WAV]

        # Senden: Jetzt PTT einschalten
        print(f"📡 Schalte PTT ein für ID {msg_id}")
        ser_ptt = serial.Serial(PTT_PORT, 9600, timeout=1)
        ser_ptt.setRTS(True); ser_ptt.setDTR(True)
        time.sleep(0.6)

        for f in files_to_play:
            subprocess.run(f"aplay -D {AUDIO_DEVICE} {f}", shell=True, capture_output=True)
            if len(files_to_play) > 1 and f == BEACON_WAV:
                time.sleep(0.3)

        time.sleep(0.3)
        ser_ptt.setRTS(False); ser_ptt.setDTR(False); ser_ptt.close()
        print(f"🔇 Senden beendet.")
    except Exception as e: 
        print(f"❌ Fehler: {e}")

def audio_worker():
    while True:
        item = message_queue.get()
        if item is None: break
        is_beacon, content, msg_id = item
        play_audio_to_radio(is_beacon, content, msg_id)
        message_queue.task_done()

def beacon_worker():
    global last_radio_activity
    print("🛸 Baken-Überwachung gestartet.")
    while True:
        time.sleep(1)
        if time.time() - last_radio_activity >= BAKEN_INTERVAL_SEC:
            last_radio_activity = time.time()
            uhrzeit = datetime.now().strftime("%H:%M")
            generate_morse_wav(MY_CALLSIGN, BEACON_WAV)
            speech_content = f"{BEACON_VOICE_TEXT} {uhrzeit}"
            message_queue.put((True, speech_content, get_next_hex_id()))

def on_connect(client, userdata, flags, rc, properties=None):
    if rc == 0: 
        print("✅ Broker verbunden.")
        client.subscribe(MQTT_TOPIC, qos=1)

def on_message(client, userdata, msg):
    try:
        raw = msg.payload.decode('utf-8').strip()
        if not raw: return
        data = json.loads(raw)
        sensor = msg.topic.split("/")[-1].replace("_", " ")
        text = ""
        now = time.time()
        if data.get("event") == "AIRCRAFT_CLOSE":
            icao = data.get("hex_id", "").upper().strip()
            call = data.get("callsign", "").strip()
            call_sp = "unbekannt" if not call or call == "N/A" else call
            ac_type = data.get("type", "").strip() or aircraft_cache.get(icao, "")
            type_ph = "ein Luftfahrzeug" if not ac_type or ac_type == "N/A" else f"ein Flugzeug vom Typ {ac_type}"
            if now - last_announced.get(f"air_{icao}", 0) > AIRCRAFT_COOLDOWN_SEC:
                dist = data.get('distance_km', 'unbekannt')
                alt = data.get('altitude_ft', 'unbekannt')
                text = f"Luftraum Warnung. Es nähert sich {type_ph}. Rufzeichen {call_sp}. Entfernung {dist} Kilometer. Höhe {alt} Fuß."
                last_announced[f"air_{icao}"] = now
        elif any(k in data for k in ["msg", "text", "message"]):
            text = data.get("msg", data.get("text", data.get("message")))
        elif "temperature" in data:
            if now - last_announced.get(f"t_{sensor}", 0) > (TEMP_COOLDOWN_MINUTES * 60):
                text = f"Temperatur Information: {sensor} meldet {data['temperature']} Grad."
                last_announced[f"t_{sensor}"] = now
        if text: message_queue.put((False, text, get_next_hex_id()))
    except: pass

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("-i", "--ip", help="MQTT Broker IP")
    args = parser.parse_args()
    if args.ip:
        load_aircraft_database()
        threading.Thread(target=audio_worker, daemon=True).start()
        threading.Thread(target=beacon_worker, daemon=True).start()
        client = mqtt_client.Client(mqtt_client.CallbackAPIVersion.VERSION2, client_id=CLIENT_ID)
        client.on_connect = on_connect; client.on_message = on_message
        client.connect(args.ip, 1883, 60)
        client.loop_forever()

