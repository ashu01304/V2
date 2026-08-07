import webbrowser
from threading import Timer
from dashboard import app

if __name__ == "__main__":
    Timer(1, lambda: webbrowser.open("http://127.0.0.1:8050")).start()
    app.run(host="127.0.0.1", port=8050, debug=False)
