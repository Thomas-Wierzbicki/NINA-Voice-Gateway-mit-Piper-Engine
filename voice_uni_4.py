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

# --- AUTOMATISCHE AUDIO-SUCHE ---
def find_audio_device(search_name="USB Audio"):
    print(f"🔍 Suche nach Soundkarte mit dem Namen '{search_name}'...")
    try:
        result = subprocess.run(['aplay', '-l'], capture_output=True, text=True, check=True)
        for line in result.stdout.split('\n'):
            if line.startswith('card') and search_name in line:
                card_num = line.split('card ')[1].split(':')[0]
                device = f"plughw:{card_num},0"
                print(f"✅ Soundkarte gefunden! Nutze dynamisch: {device}")
                return device
        print(f"⚠️ '{search_name}' nicht gefunden. Fällt auf Standard (plughw:1,0) zurück.")
        return "plughw:1,0"
    except Exception as e:
        print(f"❌ Fehler bei der automatischen Audio-Suche: {e}")
        return "plughw:1,0"

# --- HARDCODED KONFIGURATION ---
MQTT_TOPIC   = "#"
PTT_PORT     = "/dev/ttyUSB0"
AUDIO_DEVICE = find_audio_device("USB Audio")

BASE_DIR     = "/home/pi/piper"
PIPER_BIN    = f"{BASE_DIR}/piper"
MODEL_PATH   = f"{BASE_DIR}/de_DE-thorsten-high.onnx"
OUTPUT_WAV   = f"{BASE_DIR}/alert.wav"

# --- NEU: ANTI-SPAM & WARTESCHLANGE ---
TEMP_COOLDOWN_MINUTES = 60 # Temperatur-Sensoren nur alle 60 Min vorlesen
last_announced = {}        # Gedächtnis für Sensoren: {"SensorName": Zeitstempel}
message_queue = queue.Queue() # Das "Wartezimmer" für Durchsagen

# --- PTT HARDWARE SETUP ---
try:
    ser_ptt = serial.Serial(PTT_PORT, 9600, timeout=1)
    ser_ptt.setRTS(False)
    ser_ptt.setDTR(False)
except Exception as e:
    print(f"❌ KRITISCH: PTT-Adapter nicht erreichbar: {e}")
    sys.exit(1)

def ptt_control(on):
    try:
        ser_ptt.setRTS(on)
        ser_ptt.setDTR(on)
    except Exception as e:
        print(f"⚠️ PTT-Fehler: {e}")

# --- KERN-LOGIK: AUDIO & FUNK ---
def play_text_to_radio(text):
    print(f"\n📢 VERARBEITE: {text}")
    print("🤖 Piper berechnet Audio...")
    start_time = time.time()
    
    env = os.environ.copy()
    env["LD_LIBRARY_PATH"] = BASE_DIR
    
    try:
        subprocess.run(
            [PIPER_BIN, '--model', MODEL_PATH, '--output_file', OUTPUT_WAV], 
            input=text.encode('utf-8'), env=env, check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    except subprocess.CalledProcessError as e:
        print(f"❌ Fehler bei der Audio-Generierung: {e}")
        return

    calc_duration = time.time() - start_time
    print(f"✅ Audio bereit ({calc_duration:.1f}s)")

    print("📡 PTT AN (Senden...)")
    ptt_control(True)
    time.sleep(1.5) 

    print("🔉 Modulation...")
    try:
        subprocess.run(
            ["aplay", "-D", AUDIO_DEVICE, OUTPUT_WAV], 
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60
        )
    except subprocess.TimeoutExpired:
        print("⚠️ Timeout bei Audio-Wiedergabe!")

    time.sleep(0.8)
    ptt_control(False)
    print("✅ TX beendet. PTT AUS.\n")

# --- NEU: DER HINTERGRUND-ARBEITER (THREAD) ---
def audio_worker():
    """Holt Nachrichten nacheinander aus der Warteschlange und funkt sie ab."""
    while True:
        text = message_queue.get() # Blockiert, bis eine neue Nachricht da ist
        if text is None:
            break # Beendet den Thread, falls None gesendet wird
        play_text_to_radio(text)
        message_queue.task_done()

# --- MQTT CALLBACKS ---
def on_message(client, userdata, msg):
    if "bridge/logging" in msg.topic or "bridge/log" in msg.topic:
        return

    try:
        raw_payload = msg.payload.decode('utf-8').strip()
        if not raw_payload: return

        text_to_speak = ""
        current_time = time.time()

        try:
            data = json.loads(raw_payload)
            if isinstance(data, dict):
                
                sensor_name = msg.topic.split("/")[-1].replace("_", " ")

                # A: Text-Nachrichten (Prio 1 - Kein Cooldown)
                if "msg" in data or "text" in data or "message" in data:
                    text_to_speak = data.get("msg", data.get("text", data.get("message")))
                    if "z2m:mqtt" in text_to_speak or "MQTT publish" in text_to_speak:
                        return
                
                # B: Tür- und Fensterkontakte (Nur kurzer 5-Sekunden-Schutz gegen Doppelauslösen)
                elif "contact" in data:
                    status = "geschlossen" if data["contact"] else "geöffnet"
                    mem_key = f"contact_{sensor_name}"
                    
                    if current_time - last_announced.get(mem_key, 0) > 5:
                        text_to_speak = f"Sicherheitsinformation: {sensor_name} wurde {status}."
                        last_announced[mem_key] = current_time

                # C: Temperatur-Sensoren (Strikter Cooldown von z.B. 60 Minuten)
                elif "temperature" in data or "device_temperature" in data:
                    temp = data.get("temperature", data.get("device_temperature"))
                    mem_key = f"temp_{sensor_name}"
                    
                    # Cooldown prüfen
                    if current_time - last_announced.get(mem_key, 0) > (TEMP_COOLDOWN_MINUTES * 60):
                        text_to_speak = f"Temperatur Information: {sensor_name} meldet {temp} Grad."
                        last_announced[mem_key] = current_time
                    else:
                        return # Cooldown aktiv -> Ignorieren

                else:
                    return # Unbekanntes JSON
            else:
                return 

        except json.JSONDecodeError:
            return 

        # Nachricht in die Warteschlange schieben (statt direkt zu senden)
        if text_to_speak:
            print(f"📥 [In Warteschlange] {text_to_speak}")
            message_queue.put(text_to_speak)

    except Exception as e:
        print(f"❌ Unerwarteter Fehler: {e}")

# --- HAUPTPROGRAMM (CLI PARSING) ---
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Funk-Gateway für Text-to-Speech (Piper)")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("-i", "--ip", help="IP-Adresse des MQTT-Brokers (Gateway-Modus)")
    group.add_argument("-t", "--text", help="Direkter Text für einmalige Sendung (Standalone-Modus)")
    parser.add_argument("-p", "--port", type=int, default=1883, help="Port des MQTT-Brokers")

    args = parser.parse_args()

    if args.text:
        print("🚀 Starte im Standalone-Modus")
        play_text_to_radio(args.text)
        ser_ptt.close()
        sys.exit(0)

    if args.ip:
        print("🚀 Starte im MQTT-Gateway-Modus")
        
        # Starte den Hintergrund-Thread für die Funk-Warteschlange
        worker = threading.Thread(target=audio_worker, daemon=True)
        worker.start()

        client = mqtt_client.Client(mqtt_client.CallbackAPIVersion.VERSION2)
        client.on_message = on_message

        try:
            client.connect(args.ip, args.port, 60)
            client.subscribe(MQTT_TOPIC)
            
            print("-" * 50)
            print(f"🚀 GATEWAY BEREIT (Warteschlange aktiv)")
            print(f"   Audio:   {AUDIO_DEVICE}")
            print(f"   Spam-Schutz: {TEMP_COOLDOWN_MINUTES} Min für Temperatur")
            print("-" * 50)
            
            client.loop_forever()

        except KeyboardInterrupt:
            print("\n👋 Gateway wird manuell beendet.")
        except Exception as e:
            print(f"❌ MQTT Fehler: {e}")
        finally:
            ptt_control(False)
            ser_ptt.close()
            sys.exit(0)
