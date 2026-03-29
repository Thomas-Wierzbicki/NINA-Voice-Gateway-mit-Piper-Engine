import paho.mqtt.client as mqtt
from paho.mqtt import client as mqtt_client
import subprocess
import serial
import time
import json
import sys
import os

# --- KONFIGURATION (Pfade basierend auf deinen Tests) ---
MQTT_BROKER  = "192.168.188.44"
MQTT_TOPIC   = "meshcom/tx"
PTT_PORT     = "/dev/ttyUSB0"
AUDIO_DEVICE = "plughw:1,0"

BASE_DIR     = "/home/pi/piper"
PIPER_BIN    = f"{BASE_DIR}/piper"
MODEL_PATH   = f"{BASE_DIR}/de_DE-thorsten-high.onnx"
OUTPUT_WAV   = f"{BASE_DIR}/alert.wav"

# --- PTT HARDWARE SETUP ---
try:
    # Wir öffnen den Port einmalig und halten ihn offen
    ser_ptt = serial.Serial(PTT_PORT, 9600, timeout=1)
    ser_ptt.setRTS(False)
    ser_ptt.setDTR(False)
    print(f"✅ PTT-Schnittstelle {PTT_PORT} initialisiert.")
except Exception as e:
    print(f"❌ KRITISCH: PTT-Adapter nicht erreichbar: {e}")
    sys.exit(1)

def ptt_control(on):
    """Schaltet den Sender (True) oder Empfänger (False)."""
    try:
        ser_ptt.setRTS(on)
        ser_ptt.setDTR(on)
    except Exception as e:
        print(f"⚠️ PTT-Fehler während Sendung: {e}")

# --- DURCHSAGEN-LOGIK ---
def on_message(client, userdata, msg):
    try:
        # 1. Daten empfangen
        payload = msg.payload.decode()
        data = json.loads(payload)
        text = data.get("msg", "")
        if not text:
            return

        print(f"\n📢 NEUER ALARM: {text}")

        # 2. SPRACH-SYNTHESE (Im Hintergrund berechnen)
        print("🤖 Piper berechnet Audio...")
        start_time = time.time()
        
        env = os.environ.copy()
        env["LD_LIBRARY_PATH"] = BASE_DIR
        
        # Piper-Prozess starten und auf Ende warten
        gen_process = subprocess.Popen(['echo', text], stdout=subprocess.PIPE)
        subprocess.run([
            PIPER_BIN, 
            '--model', MODEL_PATH, 
            '--output_file', OUTPUT_WAV
        ], stdin=gen_process.stdout, env=env, check=True)
        gen_process.stdout.close()
        
        calc_duration = time.time() - start_time
        print(f"✅ Audio bereit (Berechnungszeit: {calc_duration:.1f}s)")

        # 3. FUNK-SEQUENCE STARTEN
        print("📡 PTT AN (Senden...)")
        ptt_control(True)
        
        # Vorlaufzeit (Squelch-Öffnung am Empfänger)
        time.sleep(1.5) 

        print("🔉 Modulation...")
        # 'aplay' spielt die Datei ab. Timeout-Schutz falls ALSA hängt.
        try:
            subprocess.run(
                ["aplay", "-D", AUDIO_DEVICE, OUTPUT_WAV], 
                stdout=subprocess.DEVNULL, 
                stderr=subprocess.DEVNULL,
                timeout=60 # PTT-Watchdog
            )
        except subprocess.TimeoutExpired:
            print("⚠️ Timeout bei Audio-Wiedergabe!")

        # Nachlaufzeit (Tail)
        time.sleep(0.8)
        
        ptt_control(False)
        print("✅ TX beendet. PTT AUS.")

    except Exception as e:
        print(f"❌ Fehler im Voice-Prozess: {e}")
        ptt_control(False) # Sicher ist sicher

# --- MQTT VERBINDUNG ---
client = mqtt_client.Client(mqtt_client.CallbackAPIVersion.VERSION2)
client.on_message = on_message

try:
    print(f"🔗 Verbinde zu MQTT Broker: {MQTT_BROKER}...")
    client.connect(MQTT_BROKER, 1883, 60)
    client.subscribe(MQTT_TOPIC)
    
    print("-" * 40)
    print(f"🚀 NINA-VOICE GATEWAY BEREIT")
    print(f"   Modell: {os.path.basename(MODEL_PATH)}")
    print(f"   Audio:  {AUDIO_DEVICE}")
    print("-" * 40)
    
    client.loop_forever()

except KeyboardInterrupt:
    print("\n👋 Gateway wird manuell beendet.")
    ptt_control(False)
    ser_ptt.close()
    sys.exit(0)
except Exception as e:
    print(f"❌ MQTT Verbindungsfehler: {e}")
    ptt_control(False)
    sys.exit(1)
