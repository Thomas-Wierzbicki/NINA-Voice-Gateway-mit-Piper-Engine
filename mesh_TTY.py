#!/usr/bin/env python3
import argparse
import time
import serial
import json
import random
from datetime import datetime


DEFAULT_PORT = "/dev/ttyUSB0"
DEFAULT_BAUD = 115200


def make_msg(src: str, dst: str, text: str) -> dict:
    """Erzeugt ein MeshCom-JSON im beobachteten Nachrichtenformat."""
    return {
        "src_type": "udp",
        "type": "msg",
        "src": src.upper(),
        "dst": dst.upper(),
        "msg": text,
        "msg_id": f"{random.getrandbits(32):08X}"
    }


def send_meshcom(ser: serial.Serial, src: str, dst: str, text: str) -> str:
    """Sendet genau eine JSON-Zeile mit Newline."""
    packet = make_msg(src, dst, text)
    line = json.dumps(packet, ensure_ascii=False) + "\n"
    ser.write(line.encode("utf-8"))
    ser.flush()
    return line


def read_for_duration(ser: serial.Serial, duration: float = 2.0, label: str = "") -> str:
    """Liest für eine feste Zeit alle seriellen Daten ein."""
    end_time = time.time() + duration
    chunks = []

    while time.time() < end_time:
        waiting = ser.in_waiting
        if waiting:
            data = ser.read(waiting).decode("utf-8", errors="ignore")
            chunks.append(data)
        time.sleep(0.05)

    text = "".join(chunks)
    if label:
        print(f"[*] {label}:")
        if text.strip():
            print(text.rstrip())
        else:
            print("(keine Daten)")
    return text


def wait_after_open(ser: serial.Serial, boot_wait: float = 3.0) -> str:
    """
    Wartet nach dem Öffnen des Ports, damit ein möglicher ESP32-Reset/Boot
    vollständig durchlaufen kann.
    """
    print(f"[*] Warte {boot_wait:.1f}s auf möglichen ESP32-Boot ...")
    time.sleep(boot_wait)
    bootlog = read_for_duration(ser, duration=1.0, label="Boot-/Startausgabe")
    return bootlog


def clear_input(ser: serial.Serial) -> None:
    """Leert den RX-Puffer."""
    try:
        ser.reset_input_buffer()
    except Exception:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(
        description="MeshCom Serial Sender mit Boot-Wartephase"
    )
    parser.add_argument(
        "-p", "--port",
        type=str,
        default=DEFAULT_PORT,
        help=f"Serieller Port (Default: {DEFAULT_PORT})"
    )
    parser.add_argument(
        "-b", "--baud",
        type=int,
        default=DEFAULT_BAUD,
        help=f"Baudrate (Default: {DEFAULT_BAUD})"
    )
    parser.add_argument(
        "-t", "--time",
        type=int,
        default=0,
        help="Wartezeit vor dem Öffnen des Ports"
    )
    parser.add_argument(
        "-w", "--boot-wait",
        type=float,
        default=3.0,
        help="Wartezeit nach dem Öffnen des Ports, um Reset/Boot abzuwarten"
    )
    parser.add_argument(
        "-s", "--src",
        type=str,
        required=True,
        help="Quell-Callsign, z. B. DA1TWD-56"
    )
    parser.add_argument(
        "-g", "--callsign",
        type=str,
        required=True,
        help="Ziel-Callsign, z. B. DA1TWD-55 oder *"
    )
    parser.add_argument(
        "-m", "--message",
        type=str,
        default="",
        help="Nachrichtentext"
    )
    parser.add_argument(
        "--hold",
        type=float,
        default=2.0,
        help="Wie lange nach dem Senden noch Antworten gelesen werden"
    )

    args = parser.parse_args()

    if args.time > 0:
        print(f"[*] Warte {args.time}s vor Start ...")
        time.sleep(args.time)

    if args.message.strip():
        message_text = args.message
    else:
        timestamp = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
        message_text = f"Test via TTY: {timestamp}"

    ser = None

    try:
        ser = serial.Serial()
        ser.port = args.port
        ser.baudrate = args.baud
        ser.timeout = 0.2

        # Handshake-Leitungen so weit wie möglich deaktivieren
        ser.dsrdtr = False
        ser.rtscts = False
        ser.xonxoff = False
        ser.dtr = False
        ser.rts = False

        print(f"[*] Öffne {args.port} mit {args.baud} Baud ...")
        ser.open()

        # Direkt nach open() erneut hart setzen
        try:
            ser.setDTR(False)
            ser.setRTS(False)
        except Exception:
            pass

        # Möglichen Reset/Boot abwarten
        bootlog = wait_after_open(ser, boot_wait=args.boot_wait)

        # Eingabepuffer leeren, damit danach nur neue Antworten sichtbar sind
        clear_input(ser)
        print("[*] RX-Puffer geleert.")

        # Nachricht senden
        print(f"[*] Sende JSON von {args.src.upper()} an {args.callsign.upper()} ...")
        sent_json = send_meshcom(ser, args.src, args.callsign, message_text)
        print(f"[*] Gesendet: {sent_json.strip()}")

        # Antworten nach dem Senden mitlesen
        read_for_duration(ser, duration=args.hold, label="Antwort nach Senden")

        print("[*] Fertig.")

    except KeyboardInterrupt:
        print("\n[!] Abbruch durch Benutzer.")
    except Exception as e:
        print(f"[!] Skriptfehler: {e}")
    finally:
        if ser and ser.is_open:
            ser.close()
            print("[*] Port geschlossen.")


if __name__ == "__main__":
    main()
