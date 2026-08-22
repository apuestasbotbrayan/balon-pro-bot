import os
from flask import Flask

app = Flask(__name__)

@app.route("/")
def home():
    return "¡El Bot de Apuestas está activo 24/7, mi hermano! 🚀⚽"

if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
