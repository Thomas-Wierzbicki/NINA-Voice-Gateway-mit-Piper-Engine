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

# --- KONFIGURATION ---
MQTT_TOPIC   = "#"
CLIENT_ID    = "voice_gateway_pi"

# Hardware-Definitionen aus deiner funktionierenden voice_uni_6.py
PTT_PORT     = "/dev/ttyUSB0" 
AUDIO_DEVICE = "plughw:1,0" 

BASE_DIR     = "/home/pi/piper"
PIPER_BIN    = f"{BASE_DIR}/piper"
MODEL_PATH   = f"{BASE_DIR}/de_DE-thorsten-high.onnx"
OUTPUT_WAV   = f"{BASE_DIR}/alert.wav"

TEMP_COOLDOWN_MINUTES = 1 
AIRCRAFT_COOLDOWN_SEC = 300  
last_announced = {}        
message_queue = queue.Queue() 

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
    print(f"\n📢 VERARBEITE [ID: 0x{msg_id}]: {text}")
    env = os.environ.copy()
    env["LD_LIBRARY_PATH"] = BASE_DIR
    
    try:
        # 1. Audio berechnen (Funkgerät bleibt stumm im Standby)
        print("⏳ Berechne Sprachausgabe...")
        subprocess.run([PIPER_BIN, '--model', MODEL_PATH, '--output_file', OUTPUT_WAV], 
                       input=text.encode('utf-8'), env=env, check=True, capture_output=True)
        
        # 2. PTT aktivieren (Port wird geöffnet und direkt geschaltet)
        print("📡 Schalte PTT ein...")
        try:
            ser_ptt = serial.Serial(PTT_PORT, 9600, timeout=1)
            ser_ptt.setRTS(True)
            ser_ptt.setDTR(True)
        except Exception as e:
            print(f"❌ PTT Fehler: {e}")
            return

        # Sendevorlauf (Sicherheitszeit für TX-Relais)
        time.sleep(0.8) 
        
        # 3. Audio abspielen mit exakten Shell-Parametern
        print(f"🔊 Spiele Audio ab...")
        # Nutzt 'shell=True' oder direkte Array-Übergabe analog zu funktionierenden ALSA-Skripten
        subprocess.run(f"aplay -D {AUDIO_DEVICE} {OUTPUT_WAV}", shell=True, capture_output=True, timeout=15)
        
        # Sendenachlauf
        time.sleep(0.4) 
        
    except Exception as e: 
        print(f"❌ Audio Fehler: {e}")
    finally: 
        # 4. PTT zwingend wieder abschalten und Schnittstelle schließen
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

        # Auswertung: Flugfunk Geofencing-Alarm
        if "event" in data and data.get("event") == "AIRCRAFT_CLOSE":
            icao = data.get("hex_id", sensor_name).upper()
            callsign = data.get("callsign", "").strip()
            callsign_speak = "unbekannt" if not callsign or callsign == "N/A" else callsign
            dist = data.get("distance_km", 0)
            alt = data.get("altitude_ft", 0)
            ac_type = data.get("type", "unbekannt")
            
            if now - last_announced.get(f"air_{icao}", 0) > AIRCRAFT_COOLDOWN_SEC:
                text_to_speak = f"Luftraum Warnung. Flugzeug {callsign_speak}, Typ {ac_type}, nähert sich auf {dist} Kilometer. Höhe {alt} Fuß."
                last_announced[f"air_{icao}"] = now

        # Auswertung: Standard Textnachrichten
        elif any(k in data for k in ["msg", "text", "message"]):
            text_to_speak = data.get("msg", data.get("text", data.get("message")))
            if "z2m:mqtt" in text_to_speak: return
            
        # Auswertung: Klimasensoren
        elif "temperature" in data or "device_temperature" in data:
            temp = data.get("temperature", data.get("device_temperature"))
            if now - last_announced.get(f"t_{sensor_name}", 0) > (TEMP_COOLDOWN_MINUTES * 60):
                text_to_speak = f"Temperatur Information: {sensor_name} meldet {temp} Grad."
                last_announced[f"t_{sensor_name}"] = now
                
        # Auswertung: Türkontakte
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

    # Vorab-Check der PTT-Hardware
    try:
        test_ser = serial.Serial(PTT_PORT, 9600, timeout=1)
        test_ser.close()
    except Exception:
        print(f"❌ KRITISCH: PTT-Adapter auf {PTT_PORT} reagiert nicht."); sys.exit(1)

    if args.text:
        play_text_to_radio(args.text, get_next_hex_id())
        sys.exit(0)

    if args.ip:
        threading.Thread(target=audio_worker, daemon=True).start()

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

