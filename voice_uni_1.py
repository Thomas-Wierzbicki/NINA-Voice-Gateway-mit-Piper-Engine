from paho.mqtt import client as mqtt_client
import subprocess
import argparse
import serial
import time
import json
import sys
import os

# --- HARDCODED KONFIGURATION ---
MQTT_TOPIC   = "#"
PTT_PORT     = "/dev/ttyUSB0"
AUDIO_DEVICE = "plughw:3,0"

BASE_DIR     = "/home/pi/piper"
PIPER_BIN    = f"{BASE_DIR}/piper"
MODEL_PATH   = f"{BASE_DIR}/de_DE-thorsten-high.onnx"
OUTPUT_WAV   = f"{BASE_DIR}/alert.wav"

# --- PTT HARDWARE SETUP ---
try:
    ser_ptt = serial.Serial(PTT_PORT, 9600, timeout=1)
    ser_ptt.setRTS(False)
    ser_ptt.setDTR(False)
except Exception as e:
    print(f"❌ KRITISCH: PTT-Adapter nicht erreichbar: {e}")
    sys.exit(1)

def ptt_control(on):
    """Schaltet den Sender (True) oder Empfänger (False)."""
    try:
        ser_ptt.setRTS(on)
        ser_ptt.setDTR(on)
    except Exception as e:
        print(f"⚠️ PTT-Fehler: {e}")

# --- KERN-LOGIK: AUDIO & FUNK ---
def play_text_to_radio(text):
    """Generiert die Sprache und steuert den Sendevorgang."""
    print(f"\n📢 VERARBEITE: {text}")

    # 1. SPRACH-SYNTHESE
    print("🤖 Piper berechnet Audio...")
    start_time = time.time()
    
    env = os.environ.copy()
    env["LD_LIBRARY_PATH"] = BASE_DIR
    
    try:
        subprocess.run(
            [PIPER_BIN, '--model', MODEL_PATH, '--output_file', OUTPUT_WAV], 
            input=text.encode('utf-8'),
            env=env, 
            check=True,
            stdout=subprocess.DEVNULL, 
            stderr=subprocess.DEVNULL
        )
    except subprocess.CalledProcessError as e:
        print(f"❌ Fehler bei der Audio-Generierung: {e}")
        return

    calc_duration = time.time() - start_time
    print(f"✅ Audio bereit (Berechnungszeit: {calc_duration:.1f}s)")

    # 2. FUNK-SEQUENCE STARTEN
    print("📡 PTT AN (Senden...)")
    ptt_control(True)
    time.sleep(1.5) # Vorlaufzeit (Squelch öffnen)

    print("🔉 Modulation...")
    try:
        subprocess.run(
            ["aplay", "-D", AUDIO_DEVICE, OUTPUT_WAV], 
            stdout=subprocess.DEVNULL, 
            stderr=subprocess.DEVNULL,
            timeout=60 # PTT-Watchdog
        )
    except subprocess.TimeoutExpired:
        print("⚠️ Timeout bei Audio-Wiedergabe!")

    time.sleep(0.8) # Nachlaufzeit (Tail)
    ptt_control(False)
    print("✅ TX beendet. PTT AUS.\n")

# --- MQTT CALLBACKS ---
def on_message(client, userdata, msg):
    try:
        raw_payload = msg.payload.decode('utf-8').strip()
        if not raw_payload:
            return

        text_to_speak = ""

        try:
            # 1. Strikte Prüfung: Ist es ein gültiges JSON?
            data = json.loads(raw_payload)
            
            # 2. Ist das JSON ein Objekt (Dictionary)?
            if isinstance(data, dict):
                # Wir akzeptieren nur diese spezifischen Schlüssel für Durchsagen
                if "msg" in data:
                    text_to_speak = data["msg"]
                elif "text" in data:
                    text_to_speak = data["text"]
                elif "message" in data:
                    text_to_speak = data["message"]
                else:
                    # Es ist JSON, aber ohne Text für uns (z.B. Temperatur-Daten)
                    print(f"ℹ️ JSON ignoriert (Kein relevantes Text-Feld gefunden): {raw_payload}")
                    return
            else:
                return # JSON-Listen oder einzelne Zahlen ignorieren

        except json.JSONDecodeError:
            # 3. ES IST KEIN JSON -> Blockieren und ignorieren!
            print(f"⚠️ Blockiert: Empfangener Text ist kein gültiges JSON.")
            return

        # 4. Wenn wir einen sauberen Text aus dem JSON gefiltert haben -> Senden!
        if text_to_speak:
            play_text_to_radio(text_to_speak)

    except Exception as e:
        print(f"❌ Unerwarteter Fehler im MQTT-Prozess: {e}")
        ptt_control(False) # PTT zur Sicherheit lösen

# --- HAUPTPROGRAMM (CLI PARSING) ---
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Funk-Gateway für Text-to-Speech (Piper)")
    
    # Mutually Exclusive Group (entweder -i ODER -t, eins davon ist Pflicht)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("-i", "--ip", help="IP-Adresse des MQTT-Brokers (startet den Gateway-Modus)")
    group.add_argument("-t", "--text", help="Direkter Text für einmalige Sendung (Standalone-Modus)")
    
    # Optionaler Port (wird ignoriert, wenn -t genutzt wird)
    parser.add_argument("-p", "--port", type=int, default=1883, help="Port des MQTT-Brokers (Standard: 1883)")

    args = parser.parse_args()

    # MODUS 1: Direkter Text (-t)
    if args.text:
        print("🚀 Starte im Standalone-Modus (Direkteingabe)")
        play_text_to_radio(args.text)
        ser_ptt.close()
        sys.exit(0)

    # MODUS 2: MQTT Gateway (-i)
    if args.ip:
        print("🚀 Starte im MQTT-Gateway-Modus")
        client = mqtt_client.Client(mqtt_client.CallbackAPIVersion.VERSION2)
        client.on_message = on_message

        try:
            print(f"🔗 Verbinde zu MQTT Broker: {args.ip}:{args.port}...")
            client.connect(args.ip, args.port, 60)
            client.subscribe(MQTT_TOPIC)
            
            print("-" * 50)
            print(f"🚀 GATEWAY BEREIT")
            print(f"   Modus:   Striktes JSON")
            print(f"   Topic:   {MQTT_TOPIC}")
            print(f"   Audio:   {AUDIO_DEVICE}")
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
