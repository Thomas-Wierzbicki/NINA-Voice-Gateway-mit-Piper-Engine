#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from paho.mqtt import client as mqtt_client
import subprocess
import threading
import argparse
import serial
import queue
import time
import json
import os
import csv
import math
import wave
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime


# --- SCHALTER ---
# Setze auf True für die neue OHF-Version (unterstützt Chinesisch)
# Setze auf False für die alte C++ Version

USE_NEW_PIPER = True




# --- KONFIGURATION ---
MQTT_TOPIC   = "flugfunk/#"
CLIENT_ID    = "voice_gateway_pi"

PTT_PORT     = "/dev/ttyUSB0"

# Bei Problemen erst "default" verwenden.

# AUDIO_DEVICE = "default"
AUDIO_DEVICE = "plughw:1,0"

BASE_DIR     = "/home/pi/piper"
PIPER_BIN    = f"{BASE_DIR}/piper"

# Für Raspberry Pi besser low oder medium.
# MODEL_PATH = f"{BASE_DIR}/de_DE-thorsten-high.onnx"

# MODEL_PATH   = f"{BASE_DIR}/de_DE-thorsten-low.onnx"
# MODEL_PATH = f"{BASE_DIR}/de_DE-thorsten-high.onnx"

#MODEL_PATH   = f"{BASE_DIR}/de_DE-eva_k-x_low.onnx"

MODEL_PATH   = f"{BASE_DIR}/zh_CN-xiao_ya-medium.onnx"


OUTPUT_WAV   = f"{BASE_DIR}/alert.wav"
BEACON_WAV   = f"{BASE_DIR}/beacon.wav"

AIRCRAFT_COOLDOWN_SEC = 300
MAX_DISTANCE_KM       = 15.0

# Zum Testen 15, später 300 oder 600.
BAKEN_INTERVAL_SEC    = 150

MY_CALLSIGN           = "DA1TWD"
MORSE_SPEED_WPM       = 15
MORSE_FREQ_HZ         = 800
BEACON_VOICE_TEXT     = "Automatisches Gateway meldet Betriebsbereitschaft. Aktuelle Uhrzeit:"

SCRIPT_DIR            = os.path.dirname(os.path.abspath(__file__))
CSV_DB_PATH           = os.path.join(SCRIPT_DIR, "aircraft.csv")
LOG_FILE              = os.path.join(SCRIPT_DIR, "adsb_fm_gateway.log")


# --- LOGGING ---
logger = logging.getLogger("ADSBtoFM")
logger.setLevel(logging.INFO)

formatter = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(threadName)s: %(message)s"
)

file_handler = RotatingFileHandler(
    LOG_FILE,
    maxBytes=1_000_000,
    backupCount=5,
    encoding="utf-8"
)
file_handler.setFormatter(formatter)

console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)

if not logger.handlers:
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)


# --- GLOBALER STATUS ---
last_radio_activity = time.time()
last_announced = {}
aircraft_cache = {}

hex_counter = 0
last_reset_time = time.time()

state_lock = threading.Lock()

# PriorityQueue: 1 wichtig, 2 Flugwarnung, 10 Bake
message_queue = queue.PriorityQueue(maxsize=50)


MORSE_DICT = {
    "A": ".-", "B": "-...", "C": "-.-.", "D": "-..", "E": ".",
    "F": "..-.", "G": "--.", "H": "....", "I": "..", "J": ".---",
    "K": "-.-", "L": ".-..", "M": "--", "N": "-.", "O": "---",
    "P": ".--.", "Q": "--.-", "R": ".-.", "S": "...", "T": "-",
    "U": "..-", "V": "...-", "W": ".--", "X": "-..-", "Y": "-.--",
    "Z": "--..",
    "1": ".----", "2": "..---", "3": "...--", "4": "....-",
    "5": ".....", "6": "-....", "7": "--...", "8": "---..",
    "9": "----.", "0": "-----",
    " ": " "
}


def safe_float(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def put_message(priority, is_beacon, content, msg_id):
    try:
        message_queue.put_nowait((priority, time.time(), is_beacon, content, msg_id))
        logger.info(f"In Queue gelegt: Prio={priority}, ID={msg_id}")
    except queue.Full:
        logger.warning(f"Queue voll. Nachricht verworfen: ID={msg_id}")


def generate_morse_wav(text, filename):
    sample_rate = 44100
    dot_duration = 1.2 / MORSE_SPEED_WPM
    dash_duration = dot_duration * 3
    audio_data = bytearray()

    def add_tone(duration):
        num_samples = int(sample_rate * duration)
        for i in range(num_samples):
            value = int(
                32767 * math.sin(2 * math.pi * MORSE_FREQ_HZ * i / sample_rate)
            )
            audio_data.extend(value.to_bytes(2, byteorder="little", signed=True))

    def add_silence(duration):
        num_samples = int(sample_rate * duration)
        for _ in range(num_samples):
            audio_data.extend((0).to_bytes(2, byteorder="little", signed=True))

    for char in text.upper():
        if char not in MORSE_DICT:
            continue

        code = MORSE_DICT[char]

        if code == " ":
            add_silence(dot_duration * 7)
        else:
            for symbol in code:
                if symbol == ".":
                    add_tone(dot_duration)
                elif symbol == "-":
                    add_tone(dash_duration)
                add_silence(dot_duration)
            add_silence(dot_duration * 3)

    with wave.open(filename, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(audio_data)

    logger.info(f"Morse-WAV erzeugt: {filename}")


def load_aircraft_database():
    global aircraft_cache

    if not os.path.exists(CSV_DB_PATH):
        logger.warning(f"Aircraft CSV nicht gefunden: {CSV_DB_PATH}")
        return

    try:
        count = 0

        with open(CSV_DB_PATH, mode="r", encoding="utf-8", errors="ignore") as f:
            reader = csv.reader(f, delimiter=";")

            for row in reader:
                if len(row) >= 5:
                    icao = row[0].strip().upper()
                    ac_type = row[4].strip() if row[4].strip() else row[2].strip()

                    if icao and ac_type:
                        aircraft_cache[icao] = ac_type
                        count += 1

        logger.info(f"Datenbank geladen: {count} Einträge.")

    except Exception as e:
        logger.exception(f"DB Fehler: {e}")


def get_next_hex_id():
    global hex_counter, last_reset_time

    with state_lock:
        if time.time() - last_reset_time >= 3600:
            hex_counter = 0
            last_reset_time = time.time()

        hex_id = f"{hex_counter:04X}"
        hex_counter += 1

    return hex_id

"""

def run_piper(content):
    env = os.environ.copy()
    env["LD_LIBRARY_PATH"] = BASE_DIR

    start = time.time()
    logger.info(f"Piper startet. Textlänge={len(content)} Zeichen")

    result = subprocess.run(
        [PIPER_BIN, "--model", MODEL_PATH, "--output_file", OUTPUT_WAV],
        input=(content + "\n").encode("utf-8"),
        env=env,
        check=True,
        capture_output=True,
        timeout=120
    )

    duration = time.time() - start
    logger.info(f"Piper fertig nach {duration:.1f}s")

    if result.stderr:
        logger.info(f"Piper STDERR: {result.stderr.decode(errors='ignore')}")


"""



def run_piper(content):
    # Die Argumente für Modell und Ausgabe sind bei beiden Versionen gleich
    piper_args = ["--model", MODEL_PATH, "--output_file", OUTPUT_WAV]
    
    if USE_NEW_PIPER:
        # Aufruf der neuen OHF Python-Bibliothek
        command = ["python3", "-m", "piper"] + piper_args
    else:
        # Aufruf der klassischen C++ Datei
        command = ["/home/pi/piper/piper"] + piper_args

    try:
        result = subprocess.run(
            command,
            input=content.encode('utf-8'),
            capture_output=True, # Fängt Fehler ab, damit sie nicht ins Leere laufen
            check=True,          # Bricht ab, wenn Piper crasht
            timeout=120
        )
    except subprocess.CalledProcessError as e:
        print("--- PIPER ABSTURZ ---")
        print(f"Befehl: {' '.join(command)}")
        print("Fehlermeldung:")
        print(e.stderr.decode('utf-8', errors='replace'))
        raise



def get_wav_duration(filename):
    try:
        with wave.open(filename, "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate()
            return frames / float(rate)
    except Exception as e:
        logger.warning(f"WAV-Länge konnte nicht gelesen werden: {filename}: {e}")
        return 30.0


def play_wav(filename):
    duration = get_wav_duration(filename)
    timeout = int(duration + 15)

    logger.info(
        f"Spiele WAV: {filename}, Länge={duration:.1f}s, Timeout={timeout}s"
    )

    result = subprocess.run(
        ["aplay", "-D", AUDIO_DEVICE, filename],
        check=True,
        capture_output=True,
        timeout=timeout
    )

    if result.stderr:
        logger.info(f"aplay STDERR: {result.stderr.decode(errors='ignore')}")


def play_audio_to_radio(is_beacon, content, msg_id):
    global last_radio_activity

    ser_ptt = None

    try:
        if is_beacon:
            logger.info(f"Vorbereitung Bake ID {msg_id}: {content}")
            run_piper(content)
            files = [BEACON_WAV, OUTPUT_WAV]
        else:
            logger.info(f"ALARM ID {msg_id}: {content}")
            run_piper(content)
            files = [OUTPUT_WAV]

        ser_ptt = serial.Serial(PTT_PORT, 9600, timeout=1)

        logger.info("PTT EIN")
        ser_ptt.setRTS(True)
        ser_ptt.setDTR(True)

        time.sleep(0.8)

        for wav_file in files:
            play_wav(wav_file)

            if is_beacon and wav_file == BEACON_WAV:
                time.sleep(0.3)

        time.sleep(0.4)

        logger.info("PTT AUS")
        ser_ptt.setRTS(False)
        ser_ptt.setDTR(False)

        with state_lock:
            last_radio_activity = time.time()

        logger.info(f"Senden beendet. ID={msg_id}")

    except subprocess.TimeoutExpired as e:
        logger.exception(f"Timeout bei Audio/TTS. ID={msg_id}: {e}")

    except subprocess.CalledProcessError as e:
        stderr = e.stderr.decode(errors="ignore") if e.stderr else ""
        logger.exception(f"Subprocess-Fehler. ID={msg_id}: {stderr}")

    except serial.SerialException as e:
        logger.exception(f"PTT/Serial-Fehler. ID={msg_id}: {e}")

    except Exception as e:
        logger.exception(f"Allgemeiner Fehler beim Senden. ID={msg_id}: {e}")

    finally:
        if ser_ptt:
            try:
                ser_ptt.setRTS(False)
                ser_ptt.setDTR(False)
                ser_ptt.close()
                logger.info("PTT sicher ausgeschaltet und Port geschlossen.")
            except Exception:
                pass


def audio_worker():
    logger.info("Audio Worker gestartet.")

    while True:
        item = message_queue.get()

        try:
            priority, ts, is_beacon, content, msg_id = item
            logger.info(f"Queue verarbeitet: Prio={priority}, ID={msg_id}")
            play_audio_to_radio(is_beacon, content, msg_id)

        except Exception as e:
            logger.exception(f"Fehler im Audio Worker: {e}")

        finally:
            message_queue.task_done()


def beacon_worker():
    logger.info("Baken-Überwachung aktiv.")

    while True:
        time.sleep(5)

        with state_lock:
            inactive_time = time.time() - last_radio_activity

        logger.info(f"Bake-Check: letzte Funkaktivität vor {inactive_time:.1f}s")

        if inactive_time >= BAKEN_INTERVAL_SEC:
            try:
                uhrzeit = datetime.now().strftime("%H:%M")

                generate_morse_wav(MY_CALLSIGN, BEACON_WAV)

                logger.info("Bake wird in Queue gelegt.")

                put_message(
                    priority=10,
                    is_beacon=True,
                    content=f"{BEACON_VOICE_TEXT} {uhrzeit}",
                    msg_id=get_next_hex_id()
                )

                # WICHTIG:
                # last_radio_activity wird NICHT hier gesetzt.
                # Es wird erst nach erfolgreichem Senden in play_audio_to_radio gesetzt.

            except Exception as e:
                logger.exception(f"Fehler im Beacon Worker: {e}")


def on_connect(client, userdata, flags, rc, properties=None):
    if rc == 0:
        client.subscribe(MQTT_TOPIC)
        logger.info(f"MQTT verbunden. Abonniert: {MQTT_TOPIC}")
    else:
        logger.error(f"MQTT Verbindung fehlgeschlagen. RC={rc}")


def on_message(client, userdata, msg):
    try:
        payload = msg.payload.decode("utf-8", errors="replace").strip()

        if not payload:
            logger.warning(f"Leere MQTT-Nachricht auf Topic {msg.topic}")
            return

        logger.info(f"MQTT RAW Topic={msg.topic}, Payload={payload[:300]}")

        try:
            data = json.loads(payload)
        except json.JSONDecodeError as e:
            logger.warning(
                f"Kein JSON auf Topic {msg.topic}. Payload={payload[:120]} Fehler={e}"
            )
            data = {}

        now = time.time()

        is_aircraft_alarm = (
            "flugfunk/alarm" in msg.topic
            or data.get("event") == "AIRCRAFT_CLOSE"
        )

        if not is_aircraft_alarm:
            return

        icao = data.get("hex_id", msg.topic.split("/")[-1]).upper()

        distance_km = safe_float(data.get("distance_km"))

        if distance_km is None:
            logger.warning(
                f"Keine gültige Entfernung für {icao}. Topic={msg.topic}, Payload={payload[:200]}"
            )
            return

        if distance_km > MAX_DISTANCE_KM:
            logger.info(
                f"Flugzeug ignoriert: {icao}, Entfernung {distance_km:.1f} km > {MAX_DISTANCE_KM:.1f} km"
            )
            return

        with state_lock:
            last_time = last_announced.get(f"air_{icao}", 0)

        if now - last_time <= AIRCRAFT_COOLDOWN_SEC:
            logger.info(f"Cooldown aktiv für {icao}. Meldung ignoriert.")
            return

        ac_type = aircraft_cache.get(icao, data.get("type", ""))

        if ac_type and ac_type.upper() not in ["NONE", "N/A", ""]:
            type_ph = f"Flugzeug Typ {' '.join(list(ac_type))}"
        else:
            type_ph = "ein Luftfahrzeug"

        callsign = data.get("callsign", "unbekannt")
        altitude_ft = data.get("altitude_ft", "unbekannt")

        text = (
            f"Luftraum Warnung. Es nähert sich {type_ph}. "
            f"Rufzeichen {callsign}. "
            f"Entfernung {distance_km:.1f} Kilometer. "
            f"Höhe {altitude_ft} Fuß."
        )

        put_message(
            priority=2,
            is_beacon=False,
            content=text,
            msg_id=get_next_hex_id()
        )

        with state_lock:
            last_announced[f"air_{icao}"] = now

    except Exception as e:
        logger.exception(f"Fehler bei MQTT-Nachricht auf Topic {msg.topic}: {e}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-i", "--ip", required=True, help="MQTT Broker IP-Adresse")
    args = parser.parse_args()

    logger.info("Starte ADSB-to-FM Gateway.")
    logger.info(f"Script-Verzeichnis: {SCRIPT_DIR}")
    logger.info(f"Logdatei: {LOG_FILE}")
    logger.info(f"MQTT Broker: {args.ip}")
    logger.info(f"MQTT Topic: {MQTT_TOPIC}")
    logger.info(f"Audio Device: {AUDIO_DEVICE}")
    logger.info(f"Piper Modell: {MODEL_PATH}")
    logger.info(f"Bakenintervall: {BAKEN_INTERVAL_SEC}s")
    logger.info(f"Maximale Warnentfernung: {MAX_DISTANCE_KM} km")

    load_aircraft_database()

    threading.Thread(
        target=audio_worker,
        daemon=True,
        name="AudioWorker"
    ).start()

    threading.Thread(
        target=beacon_worker,
        daemon=True,
        name="BeaconWorker"
    ).start()

    client = mqtt_client.Client(
        mqtt_client.CallbackAPIVersion.VERSION2,
        client_id=CLIENT_ID
    )

    client.on_connect = on_connect
    client.on_message = on_message

    try:
        client.connect(args.ip, 1883, 60)
        logger.info(f"Lausche auf MQTT Broker {args.ip}:1883")
        client.loop_forever()

    except KeyboardInterrupt:
        logger.info("Beendet durch Benutzer.")

    except Exception as e:
        logger.exception(f"MQTT Hauptfehler: {e}")


if __name__ == "__main__":
    main()
