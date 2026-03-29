import paho.mqtt.client as mqtt
from paho.mqtt import client as mqtt_client
import subprocess
import serial
import time
import json
import sys
import os

# --- KONFIGURATION ---
MQTT_BROKER = "192.168.188.44"
MQTT_TOPIC = "meshcom/tx"
PTT_PORT = "/dev/ttyUSB0"
AUDIO_DEVICE = "alsa:device=hw:1,0"

# --- GLOBALE SERIAL-VERBINDUNG ---
# Wir öffnen den Port einmal global, damit er stabil bleibt
try:
    ser_ptt = serial.Serial(PTT_PORT, 9600, timeout=1)
    print(f"✅ Hardware-Check: PTT-Adapter auf {PTT_PORT} bereit.")
except Exception as e:
    print(f"❌ Kritisch: Kann PTT-Adapter auf {PTT_PORT} nicht öffnen: {e}")
    sys.exit(1)

def ptt_control(on):
    """Steuert RTS und DTR über die bestehende Verbindung."""
    try:
        ser_ptt.setRTS(on)
        ser_ptt.setDTR(on)
    except Exception as e:
        print(f"⚠️ PTT-Steuerungsfehler: {e}")

def on_message(client, userdata, msg):
    try:
        payload = msg.payload.decode()
        data = json.loads(payload)
        text = data.get("msg", "")
        if not text: return

        print(f"\n🎙️ EMPFANGEN: {text}")

        # 1. Sprache generieren
        subprocess.run(["espeak", "-v", "de", "-s", "145", "-p", "40", "-w", "alert.wav", text])

        # 2. SENDEN
        print("📡 PTT AN (Senden...)")
        ptt_control(True)
        time.sleep(1.5) # Vorlauf (Etwas länger für Stabilität)

        print("🔉 Audio-Ausgabe...")
        # Wir nutzen 'aplay' statt 'mplayer' für einen Test, falls mplayer zickt
        # aplay ist schlanker und oft stabiler auf dem Pi
        subprocess.run(["aplay", "-D", "plughw:1,0", "alert.wav"], 
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        time.sleep(1.0) # Nachlauf
        ptt_control(False)
        print("✅ PTT AUS. Ende der Durchsage.")

    except Exception as e:
        print(f"❌ Fehler: {e}")
        ptt_control(False)

# --- MQTT SETUP ---
client = mqtt_client.Client(mqtt_client.CallbackAPIVersion.VERSION2)
client.on_message = on_message

try:
    print(f"🔗 Verbinde zu MQTT: {MQTT_BROKER}...")
    client.connect(MQTT_BROKER, 1883, 60)
    client.subscribe(MQTT_TOPIC)
    print(f"🚀 VOICE-GATEWAY LÄUFT!")
    client.loop_forever()
except KeyboardInterrupt:
    ptt_control(False)
    ser_ptt.close()
    sys.exit(0)
