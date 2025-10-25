from flask import Flask
from threading import Thread
import bot  # импортируем твой bot.py как модуль

app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is running!"

# Запускаем бота в отдельном потоке
def run_bot():
    bot.main()  # функция main() в bot.py, которая запускает бота

if __name__ == "__main__":
    Thread(target=run_bot).start()
    app.run(host="0.0.0.0", port=10000)

