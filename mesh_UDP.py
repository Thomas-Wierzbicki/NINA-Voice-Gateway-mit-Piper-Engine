import socket
import json

MESHCOM_IP = "192.168.188.190"   # anpassen!
MESHCOM_PORT = 1799

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

payload = {
    "type": "msg",
    "dst": "DA1TWD-55",
    "msg": "Direkter UDP Test"
}

data = json.dumps(payload).encode("utf-8")
sock.sendto(data, (MESHCOM_IP, MESHCOM_PORT))

print("UDP-Test gesendet")
