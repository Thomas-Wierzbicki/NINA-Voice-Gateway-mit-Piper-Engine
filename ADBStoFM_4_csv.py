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

# --- KONFIGURATION ---
MQTT_TOPIC   = "#"
CLIENT_ID    = "voice_gateway_pi"

PTT_PORT     = "/dev/ttyUSB0" 
AUDIO_DEVICE = "plughw:1,0" 

BASE_DIR     = "/home/pi/piper"
PIPER_BIN    = f"{BASE_DIR}/piper"
MODEL_PATH   = f"{BASE_DIR}/de_DE-thorsten-high.onnx"
OUTPUT_WAV   = f"{BASE_DIR}/alert.wav"

TEMP_COOLDOWN_MINUTES = 1 
AIRCRAFT_COOLDOWN_SEC = 300  

# Baken-Konfiguration (Hier auf 1800 Sek / 30 Min stellen nach erfolgreichem Test)
BAKEN_INTERVAL_SEC    = 30  
last_radio_activity   = time.time()

# DYNAMISCHER PFAD: Sucht die aircraft.csv im selben Ordner wie diese Python-Datei
SCRIPT_DIR            = os.path.dirname(os.path.abspath(__file__))
CSV_DB_PATH           = os.path.join(SCRIPT_DIR, "aircraft.csv")

last_announced = {}        
message_queue = queue.Queue() 
aircraft_cache = {}  

# --- LOKALE CSV-DATENBANK DYNAMISCH IN DEN RAM LADEN ---
def load_aircraft_database():
    global aircraft_cache
    if not os.path.exists(CSV_DB_PATH):
        print(f"⚠️ HINWEIS: aircraft.csv im Ordner {SCRIPT_DIR} nicht gefunden. Fahre ohne DB fort.")
        return
    
    print("⏳ Lade Flugzeug-Datenbank aus lokalem Verzeichnis in den RAM...")
    try:
        with open(CSV_DB_PATH, mode='r', encoding='utf-8', errors='ignore') as f:
            # Erkennt Trennzeichen (Komma oder Semikolon) automatisch
            sample = f.read(2048)
            f.seek(0)
            delimiter = ';' if ';' in sample else ','
            
            reader = csv.reader(f, delimiter=delimiter)
            for row in reader:
                if len(row) >= 3:
                    icao = row[0].strip().upper()
                    
                    # Versucht den Typ zu ermitteln (Standard: Spalte 2 oder 4)
                    ac_type = row[2].strip()
                    if len(ac_type) > 10 and len(row) >= 5: 
                        ac_type = row[4].strip()
                        
                    if icao and ac_type and ac_type.lower() not in ["none", "n/a", ""]:
                        aircraft_cache[icao] = ac_type
                        
        print(f"✅ Datenbank geladen! {len(aircraft_cache)} Flugzeuge im Cache.")
    except Exception as e:
        print(f"❌ Fehler beim Laden der CSV-Datei: {e}")

# --- HEX-ZÄHLER ---
RESET_INTERVAL = 3600
hex_counter = 0
last_reset_time = time.time()

def get_next_hex_id():
    global hex_counter, last_reset_time
    current_time = time.time()
    if current_time - last_reset_time >= RESET_INTERVAL:
        hex_counter = 0
        last_reset_time = current_time
    hex_id = f"{hex_counter:04X}"
    hex_counter += 1
    return hex_id

# --- AUDIO & PTT LOGIK ---
def play_text_to_radio(text, msg_id):
    global last_radio_activity
    print(f"\n📢 VERARBEITE [ID: 0x{msg_id}]: {text}")
    
    # Sende-Uhr zurücksetzen
    last_radio_activity = time.time()
    
    env = os.environ.copy()
    env["LD_LIBRARY_PATH"] = BASE_DIR
    
    try:
        print("⏳ Berechne Sprachausgabe...")
        subprocess.run([PIPER_BIN, '--model', MODEL_PATH, '--output_file', OUTPUT_WAV], 
                       input=text.encode('utf-8'), env=env, check=True, capture_output=True)
        
        print("📡 Schalte PTT ein...")
        try:
            ser_ptt = serial.Serial(PTT_PORT, 9600, timeout=1)
            ser_ptt.setRTS(True)
            ser_ptt.setDTR(True)
        except Exception as e:
            print(f"❌ PTT Fehler: {e}")
            return

        time.sleep(0.8) 
        
        print(f"🔊 Spiele Audio ab...")
        subprocess.run(f"aplay -D {AUDIO_DEVICE} {OUTPUT_WAV}", shell=True, capture_output=True, timeout=15)
        time.sleep(0.4) 
        
    except Exception as e: 
        print(f"❌ Audio Fehler: {e}")
    finally: 
        try:
            ser_ptt.setRTS(False)
            ser_ptt.setDTR(False)
            ser_ptt.close()
            print("🔇 Senden beendet. PTT gelöst.")
        except Exception:
            pass

def audio_worker():
    while True:
        item = message_queue.get()
        if item is None: 
            break
        msg_id, text = item
        play_text_to_radio(text, msg_id)
        message_queue.task_done()

def beacon_worker():
    global last_radio_activity
    print("🛸 Baken-Überwachung gestartet.")
    while True:
        time.sleep(1)  
        now = time.time()
        if now - last_radio_activity >= BAKEN_INTERVAL_SEC:
            last_radio_activity = now  
            
            beacon_text = "Automatische Stationsmeldung. Gateway Bereitschaft."
            hid = get_next_hex_id()
            print(f"📡 [ID: 0x{hid}] [Bake ausgelöst] {beacon_text}")
            message_queue.put((hid, beacon_text))

# --- MQTT CALLBACKS ---
def on_connect(client, userdata, flags, rc, properties=None):
    if rc == 0:
        print(f"✅ Erfolgreich mit Broker verbunden.")
        client.subscribe(MQTT_TOPIC, qos=1)
    else:
        print(f"❌ Verbindung fehlgeschlagen (Code {rc})")

def on_message(client, userdata, msg):
    if "bridge/log" in msg.topic: return
    try:
        raw_payload = msg.payload.decode('utf-8').strip()
        if not raw_payload: return
        
        data = json.loads(raw_payload)
        if not isinstance(data, dict): return
        
        sensor_name = msg.topic.split("/")[-1].replace("_", " ")
        text_to_speak = ""
        now = time.time()

        if "event" in data and data.get("event") == "AIRCRAFT_CLOSE":
            icao = data.get("hex_id", "").upper().strip()
            callsign = data.get("callsign", "").strip()
            callsign_speak = "unbekannt" if not callsign or callsign == "N/A" else callsign
            dist = data.get("distance_km", 0)
            alt = data.get("altitude_ft", 0)
            
            ac_type = data.get("type", "").strip()
            
            # Ausfall-Spur: Falls Typ im MQTT leer oder N/A ist, in lokaler CSV suchen
            if not ac_type or ac_type == "N/A" or ac_type == "None":
                ac_type = aircraft_cache.get(icao, "")
            
            if not ac_type or ac_type == "N/A" or ac_type == "None":
                type_phrase = "ein Luftfahrzeug"
            else:
                type_phrase = f"ein Flugzeug vom Typ {ac_type}"
            
            if now - last_announced.get(f"air_{icao}", 0) > AIRCRAFT_COOLDOWN_SEC:
                text_to_speak = f"Luftraum Warnung. Es nähert sich {type_phrase}. Rufzeichen {callsign_speak}. Entfernung {dist} Kilometer. Höhe {alt} Fuß."
                last_announced[f"air_{icao}"] = now

        elif any(k in data for k in ["msg", "text", "message"]):
            text_to_speak = data.get("msg", data.get("text", data.get("message")))
            if "z2m:mqtt" in text_to_speak: return
            
        elif "temperature" in data or "device_temperature" in data:
            temp = data.get("temperature", data.get("device_temperature"))
            if now - last_announced.get(f"t_{sensor_name}", 0) > (TEMP_COOLDOWN_MINUTES * 60):
                text_to_speak = f"Temperatur Information: {sensor_name} meldet {temp} Grad."
                last_announced[f"t_{sensor_name}"] = now
                
        elif "contact" in data:
            status = "geschlossen" if data["contact"] else "geöffnet"
            if now - last_announced.get(f"c_{sensor_name}", 0) > 5:
                text_to_speak = f"Sicherheitsinformation: {sensor_name} wurde {status}."
                last_announced[f"c_{sensor_name}"] = now

        if text_to_speak:
            hid = get_next_hex_id()
            print(f"📥 [ID: 0x{hid}] [Warteschlange] {text_to_speak}")
            message_queue.put((hid, text_to_speak))
            
    except Exception: 
        pass

# --- MAIN ---
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("-i", "--ip", help="MQTT Broker IP")
    group.add_argument("-t", "--text", help="Direkt-Text")
    parser.add_argument("-p", "--port", type=int, default=1883)
    args = parser.parse_args()

    try:
        test_ser = serial.Serial(PTT_PORT, 9600, timeout=1)
        test_ser.close()
    except Exception:
        print(f"❌ KRITISCH: PTT-Adapter auf {PTT_PORT} reagiert nicht."); sys.exit(1)

    if args.text:
        play_text_to_radio(args.text, get_next_hex_id())
        sys.exit(0)

    if args.ip:
        # Lokale CSV-Datenbank beim Start initialisieren
        load_aircraft_database()
        
        threading.Thread(target=audio_worker, daemon=True).start()
        threading.Thread(target=beacon_worker, daemon=True).start()

        client = mqtt_client.Client(
            mqtt_client.CallbackAPIVersion.VERSION2, 
            client_id=CLIENT_ID, 
            clean_session=False
        )
        client.on_connect = on_connect
        client.on_message = on_message

        try:
            client.connect(args.ip, args.port, 60)
            print("-" * 55)
            print(f"📡 PTT: {PTT_PORT} | Audio: {AUDIO_DEVICE}")
            print(f"🚀 Gateway läuft im manuellen Modus auf {args.ip}...")
            print("-" * 55)
            client.loop_forever()
        except KeyboardInterrupt:
            print("\n👋 Beendet.")

