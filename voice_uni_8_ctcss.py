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

# --- AUTOMATISCHE SUCHE ---
def find_audio_device(search_name="USB Audio"):
    try:
        result = subprocess.run(['aplay', '-l'], capture_output=True, text=True, check=True)
        for line in result.stdout.split('\n'):
            if line.startswith('card') and search_name in line:
                card_num = line.split('card ')[1].split(':')[0]
                return f"plughw:{card_num},0"
        return "plughw:1,0"
    except Exception: return "plughw:1,0"

def find_ptt_port():
    for i in range(5):
        port = f"/dev/ttyUSB{i}"
        if os.path.exists(port): return port
    return "/dev/ttyUSB0"

# --- KONFIGURATION ---
MQTT_TOPIC   = "#"
PTT_PORT     = find_ptt_port()
AUDIO_DEVICE = find_audio_device("USB Audio")
CLIENT_ID    = "voice_gateway_pi"

BASE_DIR     = "/home/pi/piper"
PIPER_BIN    = f"{BASE_DIR}/piper"
MODEL_PATH   = f"{BASE_DIR}/de_DE-thorsten-high.onnx"
OUTPUT_WAV   = f"{BASE_DIR}/alert.wav"

CTCSS_FREQ   = "88.5" # Auf "0" setzen zum Deaktivieren
TEMP_COOLDOWN_MINUTES = 60 
last_announced = {}        
message_queue = queue.Queue() 

# --- HEX-ZÄHLER ---
RESET_INTERVAL = 3600
hex_counter = 0
last_reset_time = time.time()

def get_next_hex_id():
    global hex_counter, last_reset_time
    if time.time() - last_reset_time >= RESET_INTERVAL:
        hex_counter = 0
        last_reset_time = time.time()
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
        # 1. Piper generiert die WAV-Datei[cite: 2]
        subprocess.run([PIPER_BIN, '--model', MODEL_PATH, '--output_file', OUTPUT_WAV], 
                       input=text.encode('utf-8'), env=env, check=True, capture_output=True)
        
        # 2. Senden einleiten[cite: 2]
        ptt_control(True)
        time.sleep(1.2) 

        # 3. Abspielen mit Sox (play) inklusive CTCSS[cite: 2]
        if CTCSS_FREQ != "0":
            # --buffer 32768 erhöht den Puffer gegen under-runs
            # -b 16 erzwingt 16-Bit gegen 0-bit Warnungen
            # -r 22050 passt die Rate an Piper an
            subprocess.run([
                'play', '-q', 
                '--buffer', '32768',          # Größerer Puffer gegen Aussetzer
                '-b', '16',                   # 16-Bit Tiefe
                '-r', '22050',                # Samplerate festlegen
                '-c', '1',                    # Mono erzwingen
                '-t', 'alsa', AUDIO_DEVICE, 
                OUTPUT_WAV, 
                'synth', 'sine', CTCSS_FREQ, 'vol', '0.15',
                'remix', '-', 
                'dither'
            ], env=env)
        else:
            # Fallback auf aplay ohne CTCSS[cite: 2]
            subprocess.run(["aplay", "-D", AUDIO_DEVICE, OUTPUT_WAV], capture_output=True)

        time.sleep(0.5) 
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
    if rc == 0:
        # Paho MQTT v2.x kompatibler Zugriff auf Flags[cite: 2]
        if flags.session_present:
            print(f"✅ Session gefunden! '{CLIENT_ID}' wird fortgesetzt.")
        else:
            print(f"🆕 Neue Session gestartet.")
        client.subscribe(MQTT_TOPIC, qos=1)
    else: 
        print(f"❌ Verbindung fehlgeschlagen (Code {rc})")

def on_message(client, userdata, msg):
    if "bridge/log" in msg.topic: return
    try:
        data = json.loads(msg.payload.decode('utf-8').strip())
        sensor_name = msg.topic.split("/")[-1].replace("_", " ")
        text = ""
        now = time.time()

        if any(k in data for k in ["msg", "text", "message"]):
            text = data.get("msg", data.get("text", data.get("message")))
            if "z2m:mqtt" in text: return
        elif "temperature" in data:
            if now - last_announced.get(f"t_{sensor_name}", 0) > (TEMP_COOLDOWN_MINUTES * 60):
                text = f"Temperatur Information: {sensor_name} meldet {data['temperature']} Grad."
                last_announced[f"t_{sensor_name}"] = now
        elif "contact" in data:
            if now - last_announced.get(f"c_{sensor_name}", 0) > 5:
                status = "geschlossen" if data["contact"] else "geöffnet"
                text = f"Sicherheitsinformation: {sensor_name} wurde {status}."
                last_announced[f"c_{sensor_name}"] = now

        if text:
            hid = get_next_hex_id()
            print(f"📥 [ID: 0x{hid}] [Queue] {text}")
            message_queue.put((hid, text))
    except Exception: pass

# --- MAIN ---
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("-i", "--ip", help="MQTT Broker IP")
    group.add_argument("-t", "--text", help="Direkt-Text zum Testen")
    parser.add_argument("-p", "--port", type=int, default=1883)
    args = parser.parse_args()

    # PTT initialisieren[cite: 2]
    try:
        ser_ptt = serial.Serial(PTT_PORT, 9600, timeout=1)
    except Exception: 
        print(f"❌ PTT Fehler auf {PTT_PORT}"); sys.exit(1)

    if args.text:
        play_text_to_radio(args.text, get_next_hex_id())
        ser_ptt.close()
        sys.exit(0)

    if args.ip:
        threading.Thread(target=audio_worker, daemon=True).start()
        # Initialisierung für Paho MQTT v2[cite: 2]
        client = mqtt_client.Client(mqtt_client.CallbackAPIVersion.VERSION2, client_id=CLIENT_ID, clean_session=False)
        client.on_connect = on_connect
        client.on_message = on_message
        
        try:
            client.connect(args.ip, args.port, 60)
            print(f"🚀 Gateway aktiv (MQTT: {args.ip})")
            client.loop_forever()
        except KeyboardInterrupt:
            print("\nBeendet durch Benutzer.")
        finally:
            ptt_control(False)
            ser_ptt.close()
