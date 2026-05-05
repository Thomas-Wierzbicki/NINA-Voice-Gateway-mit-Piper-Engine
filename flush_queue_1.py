from paho.mqtt import client as mqtt_client
import argparse
import time
import sys

# --- KONFIGURATION ---
CLIENT_ID = "voice_gateway_pi" 
MQTT_TOPIC = "#"

def clear_and_kill_session(ip, port):
    print(f"🧹 Verbinde zu {ip}:{port}...")
    print(f"📥 Schritt 1: Verpasste Nachrichten auslesen...")
    print("-" * 60)
    
    messages_deleted = 0

    def on_message(client, userdata, msg):
        nonlocal messages_deleted
        messages_deleted += 1
        try:
            payload = msg.payload.decode('utf-8').strip()
            print(f"🗑️ [Gelesen] {msg.topic: <30} | {payload[:40]}...")
        except:
            print(f"🗑️ [Gelesen] {msg.topic: <30} | (Binärdaten)")

    # 1. Mit False anmelden, um die alten Nachrichten zu erhalten
    client = mqtt_client.Client(
        mqtt_client.CallbackAPIVersion.VERSION2, 
        client_id=CLIENT_ID, 
        clean_session=False
    )
    client.on_message = on_message

    try:
        client.connect(ip, port, 60)
        # Abonnieren mit QoS 1, um gespeicherte Nachrichten anzufordern
        client.subscribe(MQTT_TOPIC, qos=1)
        
        client.loop_start()
        time.sleep(5) # 5 Sekunden Zeit zum Einsammeln geben
        client.loop_stop()
        client.disconnect()
        
        print("-" * 60)
        print(f"✅ {messages_deleted} Nachrichten verarbeitet.")

        # 2. DER KILL-SCHRITT: Mit True anmelden, um die Session zu löschen
        print(f"💀 Schritt 2: Session für '{CLIENT_ID}' auf dem Broker zerstören...")
        
        killer = mqtt_client.Client(
            mqtt_client.CallbackAPIVersion.VERSION2, 
            client_id=CLIENT_ID, 
            clean_session=True 
        )
        killer.connect(ip, port, 60)
        time.sleep(1) # Kurzer Moment für den Broker zum Aufräumen
        killer.disconnect()
        
        print("✨ Session beendet. Der Broker hat alles vergessen.")

    except Exception as e:
        print(f"❌ Fehler: {e}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MQTT Session Killer")
    parser.add_argument("-i", "--ip", required=True, help="Broker IP")
    parser.add_argument("-p", "--port", type=int, default=1883, help="Port")
    parser.add_argument("-d", "--delete", action="store_true", help="Bestätigung")

    args = parser.parse_args()
    if not args.delete:
        print("⚠️ Parameter '-d' fehlt.")
        sys.exit(1)

    clear_and_kill_session(args.ip, args.port)
