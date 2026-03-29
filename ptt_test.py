import serial
import time
import sys

# Dein CH340 Adapter (bitte prüfen mit ls /dev/ttyUSB*)
PORT = "/dev/ttyUSB0" 

try:
    ser = serial.Serial(PORT)
    print(f"✅ Verbindung zu {PORT} hergestellt.")
    
    print("🚀 PTT AN (Senden...) - Die LED am Adapter sollte leuchten")
    ser.setRTS(True)
    ser.setDTR(True)
    
    time.sleep(3) # 3 Sekunden "Senden"
    
    print("🛑 PTT AUS (Empfang) - LED sollte erlöschen")
    ser.setRTS(False)
    ser.setDTR(False)
    
    ser.close()
    print("Done.")

except Exception as e:
    print(f"❌ Fehler: {e}")
    print("Tipp: Hast du die Rechte? Versuche: sudo python3 ptt_test.py")
