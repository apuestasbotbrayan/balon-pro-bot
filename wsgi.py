import os
from bot import web_app, run_web
import threading

if __name__ == "__main__":
    # Arrancamos el hilo web para satisfacer a Render al instante
    web_thread = threading.Thread(target=run_web, daemon=True)
    web_thread.start()
