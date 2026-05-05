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

# --- AUTOMATISCHE SUCHE: SOUNDKARTE ---
def find_audio_device(search_name="USB Audio"):
    try:
        result = subprocess.run(['aplay', '-l'], capture_output=True, text=True, check=True)
        for line in result.stdout.split('\n'):
            if line.startswith('card') and search_name in line:
                card_num = line.split('card ')[1].split(':')[0]
                return f"plughw:{card_num},0"
        return "plughw:1,0"
    except Exception:
        return "plughw:1,0"

# --- AUTOMATISCHE SUCHE: PTT-ADAPTER ---
def find_ptt_port():
    # Prüft /dev/ttyUSB0 bis USB4
    for i in range(5):
        port = f"/dev/ttyUSB{i}"
        if os.path.exists(port):
            return port
    return "/dev/ttyUSB1" 

# --- KONFIGURATION ---
MQTT_TOPIC   = "#"
PTT_PORT     = find_ptt_port()
AUDIO_DEVICE = find_audio_device("USB Audio")
CLIENT_ID    = "voice_gateway_pi"

BASE_DIR     = "/home/pi/piper"
PIPER_BIN    = f"{BASE_DIR}/piper"
MODEL_PATH   = f"{BASE_DIR}/de_DE-thorsten-high.onnx"
OUTPUT_WAV   = f"{BASE_DIR}/alert.wav"

TEMP_COOLDOWN_MINUTES = 1 
last_announced = {}        
message_queue = queue.Queue() 

# --- HEX-ZÄHLER (Reset jede Stunde) ---
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

# --- PTT & AUDIO LOGIK ---
def ptt_control(on):
    try:
        ser_ptt.setRTS(on)
        ser_ptt.setDTR(on)
    except Exception: pass

def play_text_to_radio(text, msg_id):
    print(f"\n📢 VERARBEITE [ID: 0x{msg_id}]: {text}")
    env = os.environ.copy()
    env["LD_LIBRARY_PATH"] = BASE_DIR
    try:
        # 1. Audio berechnen
        subprocess.run([PIPER_BIN, '--model', MODEL_PATH, '--output_file', OUTPUT_WAV], 
                       input=text.encode('utf-8'), env=env, check=True, capture_output=True)
        # 2. Senden
        ptt_control(True)
        time.sleep(1.5)
        subprocess.run(["aplay", "-D", AUDIO_DEVICE, OUTPUT_WAV], capture_output=True, timeout=60)
        time.sleep(0.8)
    except Exception as e: 
        print(f"❌ Audio Fehler: {e}")
    finally: 
        ptt_control(False)

def audio_worker():
    while True:
        item = message_queue.get()
        if item is None: break
        play_text_to_radio(item[1], item[0])
        message_queue.task_done()

# --- MQTT CALLBACKS ---
def on_connect(client, userdata, flags, rc, properties=None):
    """
    Prüft beim Verbinden, ob bereits eine Session auf dem Broker existiert.
    Angepasst für Paho MQTT Client v2.x
    """
    if rc == 0:
        # KORREKTUR: Zugriff auf session_present als Attribut des flags-Objekts
        session_present = flags.session_present
        
        if session_present:
            print(f"✅ Session gefunden! '{CLIENT_ID}' wird fortgesetzt.")
        else:
            print(f"🆕 Neue Session gestartet. (Postfach war leer oder wurde gelöscht)")
        
        # Immer abonnieren, um sicherzugehen
        client.subscribe(MQTT_TOPIC, qos=1)
    else:
        print(f"❌ Verbindung fehlgeschlagen (Code {rc})")

def on_message(client, userdata, msg):
    if "bridge/log" in msg.topic: return
    try:
        raw_payload = msg.payload.decode('utf-8').strip()
        data = json.loads(raw_payload)
        if not isinstance(data, dict): return
        
        sensor_name = msg.topic.split("/")[-1].replace("_", " ")
        text_to_speak = ""
        now = time.time()

        if any(k in data for k in ["msg", "text", "message"]):
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
            print(f"📥 [ID: 0x{hid}] [Queue] {text_to_speak}")
            message_queue.put((hid, text_to_speak))
    except Exception: pass

# --- MAIN ---
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("-i", "--ip", help="MQTT Broker IP")
    group.add_argument("-t", "--text", help="Direkt-Text")
    parser.add_argument("-p", "--port", type=int, default=1883)
    args = parser.parse_args()

    # PTT initialisieren
    try:
        ser_ptt = serial.Serial(PTT_PORT, 9600, timeout=1)
    except Exception as e: 
        print(f"❌ KRITISCH: PTT-Adapter auf {PTT_PORT} nicht erreichbar."); sys.exit(1)

    if args.text:
        play_text_to_radio(args.text, get_next_hex_id())
        ser_ptt.close()
        sys.exit(0)

    if args.ip:
        threading.Thread(target=audio_worker, daemon=True).start()

        # MQTT Client Setup (v2.x)
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
            print(f"🚀 Gateway läuft auf {args.ip}...")
            print("-" * 55)
            client.loop_forever()
        except KeyboardInterrupt:
            print("\n👋 Beendet.")
        finally:
            ptt_control(False)
            ser_ptt.close()
