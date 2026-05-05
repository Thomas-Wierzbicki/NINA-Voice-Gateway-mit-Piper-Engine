from paho.mqtt import client as mqtt_client
import time

# --- KONFIGURATION ---
CLIENT_ID = "voice_gateway_pi" # Muss exakt die ID des Gateways sein!
MQTT_BROKER = "192.168.188.123"
PORT = 1883
MQTT_TOPIC = "#"

messages_received = 0

def on_message(client, userdata, msg):
    global messages_received
    messages_received += 1
    # Hier geben wir die Nachrichten nur auf der Konsole aus
    try:
        payload = msg.payload.decode('utf-8').strip()
        print(f"🗑️ [Gelesen & Gelöscht] Topic: {msg.topic} | Inhalt: {payload[:50]}...")
    except:
        print(f"🗑️ [Gelesen & Gelöscht] Topic: {msg.topic} | (Binärdaten)")

def on_connect(client, userdata, flags, rc, properties=None):
    if rc == 0:
        print(f"✅ Verbunden. Suche nach verpassten Nachrichten...")
        # WICHTIG: Mit QoS 1 abonnieren, damit der Broker die alten Nachrichten schickt
        client.subscribe(MQTT_TOPIC, qos=1)
    else:
        print(f"❌ Verbindung fehlgeschlagen (Code {rc})")

# Wir bleiben strikt bei clean_session=False
client = mqtt_client.Client(
    mqtt_client.CallbackAPIVersion.VERSION2,
    client_id=CLIENT_ID,
    clean_session=False 
)

client.on_connect = on_connect
client.on_message = on_message

try:
    print(f"📥 Öffne Postfach für '{CLIENT_ID}' (Sitzung bleibt erhalten)...")
    client.connect(MQTT_BROKER, PORT, 60)
    
    # Wir starten den Loop für 5 Sekunden. 
    # In dieser Zeit werden alle alten Nachrichten vom Broker an das Skript geschickt.
    # Da das Skript sie empfängt, gelten sie für den Broker als "zugestellt".
    client.loop_start()
    
    timeout = 5  # Sekunden warten, um sicherzugehen, dass alles abgeholt wurde
    time.sleep(timeout)
    
    client.loop_stop()
    client.disconnect()
    
    print("-" * 60)
    print(f"✨ Fertig! {messages_received} Nachrichten wurden aus dem Postfach entfernt.")
    print("Die Persistent Session blieb dabei aktiv.")

except Exception as e:
    print(f"❌ Fehler: {e}")
