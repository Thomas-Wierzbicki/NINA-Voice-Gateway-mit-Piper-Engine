import json
import socket
import time
import threading
import queue
from datetime import datetime
from paho.mqtt import client as mqtt_client

MQTT_BROKER = "192.168.188.44"
MQTT_PORT = 1883
MQTT_TOPIC = "meshcom/tx"

NODE_A_IP = "192.168.188.190"
NODE_A_PORT = 1799
DST_CALLSIGN = "DA1TWD-55"
PREFIX = "[NINA]"
DUTY_CYCLE_SECONDS = 10

udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
send_queue = queue.Queue()


def log(text: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {text}", flush=True)


def send_line_to_meshcom(line: str) -> None:
    line = line.strip()
    if not line:
        return

    payload = {
        "type": "msg",
        "dst": DST_CALLSIGN,
        "msg": line[:150]
    }

    wire = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    sent = udp_sock.sendto(wire, (NODE_A_IP, NODE_A_PORT))

    log(f"SEND -> {NODE_A_IP}:{NODE_A_PORT} ({sent} Bytes)")
    log(f"UDP: {wire.decode('utf-8', errors='replace')}")


def queue_worker() -> None:
    log("Queue-Worker gestartet")
    while True:
        line = send_queue.get()
        try:
            log(f"QUEUE SEND: {line}")
            send_line_to_meshcom(line)
            log(f"Duty Cycle: warte {DUTY_CYCLE_SECONDS}s")
            time.sleep(DUTY_CYCLE_SECONDS)
        except Exception as e:
            log(f"Fehler im Worker: {e}")
        finally:
            send_queue.task_done()


def on_connect(client, userdata, flags, reason_code, properties=None):
    log(f"MQTT verbunden: rc={reason_code}")
    client.subscribe(MQTT_TOPIC)
    log(f"MQTT subscribed: {MQTT_TOPIC}")


def on_message(client, userdata, msg):
    raw = msg.payload.decode("utf-8", errors="replace").strip()

    log(f"MQTT Topic: {msg.topic}")
    log(f"MQTT RAW: {raw}")

    if not raw:
        log("MQTT Payload leer")
        return

    try:
        data = json.loads(raw)
        if isinstance(data, dict) and "msg" in data:
            text = str(data["msg"]).strip()
            log("JSON erkannt -> msg extrahiert")
        else:
            text = raw
            log("JSON ohne msg -> Rohtext verwendet")
    except json.JSONDecodeError:
        text = raw
        log("Kein JSON -> Rohtext verwendet")

    if not text:
        log("Kein nutzbarer Text")
        return

    text = f"{PREFIX} {text}"

    lines = text.splitlines()
    if not lines:
        log("Keine Zeilen gefunden")
        return

    for idx, line in enumerate(lines, start=1):
        line = line.strip()
        if not line:
            continue

        log(f"QUEUE ZEILE {idx}: {line}")
        send_queue.put(line)
        log(f"Queue-Länge: {send_queue.qsize()}")


def main():
    worker = threading.Thread(target=queue_worker, daemon=True)
    worker.start()

    client = mqtt_client.Client(mqtt_client.CallbackAPIVersion.VERSION2)
    client.on_connect = on_connect
    client.on_message = on_message

    log(f"Verbinde zu MQTT {MQTT_BROKER}:{MQTT_PORT}")
    client.connect(MQTT_BROKER, MQTT_PORT, 60)
    client.loop_forever()


if __name__ == "__main__":
    main()
