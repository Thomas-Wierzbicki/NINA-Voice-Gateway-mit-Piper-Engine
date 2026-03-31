import json
import socket
import binascii

MESHCOM_IP = "192.168.1.77"   # anpassen
MESHCOM_PORT = 1799

payload = {
    "type": "msg",
    "dst": "192.168.188.190",
    "msg": "TEST123"
}

# Wichtig: kompakt ohne überflüssige Leerzeichen
data_str = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
data_bytes = data_str.encode("utf-8")

print("JSON-String:")
print(data_str)
print()

print("UTF-8 Bytes:")
print(data_bytes)
print()

print("Hexdump:")
print(binascii.hexlify(data_bytes).decode())
print()

print(f"Ziel: {MESHCOM_IP}:{MESHCOM_PORT}")
print(f"Länge: {len(data_bytes)} Byte")

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sent = sock.sendto(data_bytes, (MESHCOM_IP, MESHCOM_PORT))
sock.close()

print(f"Gesendet: {sent} Byte")
