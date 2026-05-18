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
BAKEN_INTERVAL_SEC    = 60  
last_radio_activity   = time.time()

# MORSE & VOICE BEACON KONFIGURATION
MY_CALLSIGN           = "DA1TWD"
MORSE_SPEED_WPM       = 15        
MORSE_FREQ_HZ         = 800       
BEACON_VOICE_TEXT     = "Automatisches Gateway meldet Betriebsbereitschaft. Aktuelle Uhrzeit:"

# Pfade
SCRIPT_DIR            = os.path.dirname(os.path.abspath(__file__))
CSV_DB_PATH           = os.path.join(SCRIPT_DIR, "aircraft.csv")

# Speicher
last_announced = {}        
message_queue = queue.Queue() 
aircraft_cache = {}  
hex_counter = 0
last_reset_time = time.time()

MORSE_DICT = {
    'A': '.-', 'B': '-...', 'C': '-.-.', 'D': '-..', 'E': '.', 'F': '..-.', 'G': '--.', 'H': '....',
    'I': '..', 'J': '.---', 'K': '-.-', 'L': '.-..', 'M': '--', 'N': '-.', 'O': '---', 'P': '.--.',
    'Q': '--.-', 'R': '.-.', 'S': '...', 'T': '-', 'U': '..-', 'V': '...-', 'W': '.--', 'X': '-..-',
    'Y': '-.--', 'Z': '--..', '1': '.----', '2': '..---', '3': '...--', '4': '....-', '5': '.....',
    '6': '-....', '7': '--...', '8': '---..', '9': '----.', '0': '-----', ' ': ' '
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
            if code == ' ': add_silence(dot_duration * 7)
            else:
                for symbol in code:
                    if symbol == '.': add_tone(dot_duration)
                    elif symbol == '-': add_tone(dash_duration)
                    add_silence(dot_duration)
                add_silence(dot_duration * 3)
    with wave.open(filename, 'wb') as wav_file:
        wav_file.setnchannels(1); wav_file.setsampwidth(2); wav_file.setframerate(sample_rate); wav_file.writeframes(audio_data)

def load_aircraft_database():
    global aircraft_cache
    if not os.path.exists(CSV_DB_PATH): return
    try:
        with open(CSV_DB_PATH, mode='r', encoding='utf-8', errors='ignore') as f:
            reader = csv.reader(f, delimiter=';')
            for row in reader:
                if len(row) >= 5:
                    icao = row[0].strip().upper()
                    ac_type = row[4].strip() if row[4].strip() else row[2].strip()
                    if icao and ac_type: aircraft_cache[icao] = ac_type
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
    try:
        # 1. Vorbereiten
        if is_beacon:
            print(f"⏳ Vorbereitung Bake ID {msg_id}: {content}")
            env = os.environ.copy(); env["LD_LIBRARY_PATH"] = BASE_DIR
            subprocess.run([PIPER_BIN, '--model', MODEL_PATH, '--output_file', OUTPUT_WAV], input=content.encode('utf-8'), env=env, check=True, capture_output=True)
            files = [BEACON_WAV, OUTPUT_WAV]
        else:
            print(f"📢 ALARM ID {msg_id}: {content}")
            env = os.environ.copy(); env["LD_LIBRARY_PATH"] = BASE_DIR
            subprocess.run([PIPER_BIN, '--model', MODEL_PATH, '--output_file', OUTPUT_WAV], input=content.encode('utf-8'), env=env, check=True, capture_output=True)
            files = [OUTPUT_WAV]

        # 2. Senden
        ser_ptt = serial.Serial(PTT_PORT, 9600, timeout=1)
        ser_ptt.setRTS(True); ser_ptt.setDTR(True); time.sleep(0.8)
        for f in files:
            subprocess.run(f"aplay -D {AUDIO_DEVICE} {f}", shell=True, capture_output=True)
            if len(files) > 1 and f == BEACON_WAV: time.sleep(0.3)
        time.sleep(0.4); ser_ptt.setRTS(False); ser_ptt.setDTR(False); ser_ptt.close()
        last_radio_activity = time.time()
        print(f"🔇 Senden beendet.")
    except Exception as e: print(f"❌ Fehler: {e}")

def audio_worker():
    while True:
        item = message_queue.get()
        if item is None: break
        is_beacon, content, msg_id = item
        play_audio_to_radio(is_beacon, content, msg_id)
        message_queue.task_done()

def beacon_worker():
    global last_radio_activity
    print("🛸 Baken-Überwachung aktiv.")
    while True:
        time.sleep(5)
        if time.time() - last_radio_activity >= BAKEN_INTERVAL_SEC:
            uhrzeit = datetime.now().strftime("%H:%M")
            generate_morse_wav(MY_CALLSIGN, BEACON_WAV)
            message_queue.put((True, f"{BEACON_VOICE_TEXT} {uhrzeit}", get_next_hex_id()))
            last_radio_activity = time.time()

def on_connect(client, userdata, flags, rc, properties=None):
    if rc == 0: client.subscribe(MQTT_TOPIC)

def on_message(client, userdata, msg):
    try:
        data = json.loads(msg.payload.decode('utf-8'))
        now = time.time()
        if "flugfunk/alarm" in msg.topic or data.get("event") == "AIRCRAFT_CLOSE":
            icao = data.get("hex_id", msg.topic.split("/")[-1]).upper()
            if now - last_announced.get(f"air_{icao}", 0) > AIRCRAFT_COOLDOWN_SEC:
                ac_type = aircraft_cache.get(icao, data.get("type", ""))
                if ac_type and ac_type.upper() not in ["NONE", "N/A", ""]:
                    type_ph = f"Flugzeug Typ {' '.join(list(ac_type))}"
                else: type_ph = "ein Luftfahrzeug"
                text = f"Luftraum Warnung. Es nähert sich {type_ph}. Rufzeichen {data.get('callsign','unbekannt')}. Entfernung {data.get('distance_km')} Kilometer. Höhe {data.get('altitude_ft')} Fuß."
                message_queue.put((False, text, get_next_hex_id()))
                last_announced[f"air_{icao}"] = now
    except: pass

if __name__ == "__main__":
    parser = argparse.ArgumentParser(); parser.add_argument("-i", "--ip"); args = parser.parse_args()
    if args.ip:
        load_aircraft_database()
        threading.Thread(target=audio_worker, daemon=True).start()
        threading.Thread(target=beacon_worker, daemon=True).start()
        client = mqtt_client.Client(mqtt_client.CallbackAPIVersion.VERSION2, client_id=CLIENT_ID)
        client.on_connect = on_connect; client.on_message = on_message
        client.connect(args.ip, 1883, 60)
        print(f"📡 Lausche auf {args.ip}...")
        client.loop_forever()

