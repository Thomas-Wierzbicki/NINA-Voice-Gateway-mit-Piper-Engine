import json
import socket
from datetime import datetime
from paho.mqtt import client as mqtt_client

MQTT_BROKER = "192.168.188.44"
MQTT_PORT = 1883
MQTT_TOPIC = "#"

NODE_A_IP = "192.168.188.190"
NODE_A_PORT = 1799
DST_CALLSIGN = "DA1TWD-55"
PREFIX = "[NINA]"

udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)


def log(text: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {text}")


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


def on_connect(client, userdata, flags, reason_code, properties=None):
    log(f"MQTT verbunden: rc={reason_code}")
    client.subscribe(MQTT_TOPIC)
    log(f"MQTT subscribed: {MQTT_TOPIC}")


def on_message(client, userdata, msg):
    raw = msg.payload.decode("utf-8", errors="replace").strip()

    log(f"MQTT Topic: {msg.topic}")

    if not raw:
        log("MQTT Payload leer")
        return

    # JSON -> msg extrahieren, sonst Rohtext verwenden
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

    # Prefix hinzufügen
    text = f"{PREFIX} {text}"

    lines = text.splitlines()

    if not lines:
        log("Keine Zeilen gefunden")
        return

    for idx, line in enumerate(lines, start=1):
        line = line.strip()
        if not line:
            continue

        log(f"ZEILE {idx}: {line}")
        send_line_to_meshcom(line)


def main():
    client = mqtt_client.Client(mqtt_client.CallbackAPIVersion.VERSION2)
    client.on_connect = on_connect
    client.on_message = on_message

    log(f"Verbinde zu MQTT {MQTT_BROKER}:{MQTT_PORT}")
    client.connect(MQTT_BROKER, MQTT_PORT, 60)
    client.loop_forever()


if __name__ == "__main__":
    main()
