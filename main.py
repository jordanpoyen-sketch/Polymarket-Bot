from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import requests
import time
import threading
import csv
import os
from datetime import datetime


app = FastAPI()

WALLET = "0x6e1d5040d0ac73709b0621f620d2a60b80d2d0fa"

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

MIN_USDC_SIZE = 100
MIN_PRICE = 0.85

PAPER_TRADE_SIZE = 10
PAPER_FILE = "paper_trades.csv"

sent_signals = set()
latest_edge_signals = []
last_scan_time = "Aucun scan encore"


def send_telegram_message(message):
    if not BOT_TOKEN or not CHAT_ID:
        return

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    params = {
        "chat_id": CHAT_ID,
        "text": message
    }

    requests.get(url, params=params)


def get_btc_price():
    url = "https://api.binance.com/api/v3/ticker/price"
    params = {"symbol": "BTCUSDT"}

    response = requests.get(url, params=params)
    data = response.json()

    return float(data["price"])


def get_wallet_activity(limit=50):
    url = "https://data-api.polymarket.com/activity"

    params = {
        "user": WALLET,
        "limit": limit,
        "offset": 0
    }

    response = requests.get(url, params=params)

    if response.status_code != 200:
        return []

    return response.json()


def get_market_data(slug):
    if not slug:
        return None

    url = "https://gamma-api.polymarket.com/markets"

    params = {
        "slug": slug
    }

    response = requests.get(url, params=params)

    if response.status_code != 200:
        return None

    data = response.json()

    if not data:
        return None

    return data[0]


def get_model_signal(btc_price):
    if btc_price > 78000:
        return "bullish"

    elif btc_price > 76000:
        return "range_bullish"

    elif btc_price > 74000:
        return "neutral"

    else:
        return "bearish"


def calculate_edge_score(outcome, avg_price, total_usdc, trade_count, btc_signal):
    score = 0

    if total_usdc > 1000:
        score += 3
    elif total_usdc > 500:
        score += 2
    elif total_usdc > 100:
        score += 1

    if avg_price > 0.97:
        score += 3
    elif avg_price > 0.93:
        score += 2
    elif avg_price > 0.88:
        score += 1

    if trade_count >= 5:
        score += 2
    elif trade_count >= 3:
        score += 1

    if btc_signal in ["bullish", "range_bullish"] and outcome == "Yes":
        score += 2

    elif btc_signal == "bearish" and outcome == "No":
        score += 2

    return min(score, 10)


def paper_trade_already_exists(title, outcome):
    if not os.path.isfile(PAPER_FILE):
        return False

    with open(PAPER_FILE, mode="r", encoding="utf-8") as file:
        reader = csv.DictReader(file)

        for row in reader:
            if (
                row.get("title") == title
                and row.get("outcome") == outcome
                and row.get("status") == "OPEN"
            ):
                return True

    return False


def save_paper_trade(signal, btc_price):
    file_exists = os.path.isfile(PAPER_FILE)

    entry_price = signal["avg_price"]
    shares = PAPER_TRADE_SIZE / entry_price

    row = {
        "date_opened": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "title": signal["title"],
        "slug": signal.get("slug") or "",
        "outcome": signal["outcome"],
        "entry_price": round(entry_price, 4),
        "trade_size": PAPER_TRADE_SIZE,
        "shares": round(shares, 4),
        "edge_score": signal["edge_score"],
        "btc_live_open": btc_price,
        "status": "OPEN",
        "result": "",
        "pnl": ""
    }

    with open(PAPER_FILE, mode="a", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=row.keys())

        if not file_exists:
            writer.writeheader()

        writer.writerow(row)


def resolve_paper_trades():
    if not os.path.isfile(PAPER_FILE):
        return

    rows = []
    updated = False

    with open(PAPER_FILE, mode="r", encoding="utf-8") as file:
        reader = csv.DictReader(file)

        for row in reader:
            if row.get("status") == "OPEN":
                slug = row.get("slug") or ""

                if not slug:
                    rows.append(row)
                    continue

                market = get_market_data(slug)

                if market:
                    closed = market.get("closed")

                    winning_outcome = (
                        market.get("outcome")
                        or market.get("winner")
                        or market.get("winningOutcome")
                    )

                    if closed:
                        updated = True

                        if winning_outcome == row.get("outcome"):
                            row["result"] = "WIN"

                            pnl = float(row["shares"]) - float(row["trade_size"])
                            row["pnl"] = round(pnl, 2)

                        else:
                            row["result"] = "LOSS"
                            row["pnl"] = -float(row["trade_size"])

                        row["status"] = "CLOSED"

            rows.append(row)

    if updated and rows:
        with open(PAPER_FILE, mode="w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)

        print("✅ Paper trades mis à jour")


def whale_tracker_loop():
    global latest_edge_signals
    global last_scan_time

    while True:
        try:
            last_scan_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            print("\n" + "=" * 60)
            print("SCAN BOT :", last_scan_time)
            print("=" * 60)

            resolve_paper_trades()

            latest_edge_signals = []

            btc_price = get_btc_price()
            btc_signal = get_model_signal(btc_price)

            print("BTC :", btc_price)
            print("Signal modèle :", btc_signal)

            activities = get_wallet_activity(50)
            print("Activités récupérées :", len(activities))

            grouped_signals = {}

            for activity in activities:
                title = str(activity.get("title"))
                slug = activity.get("slug") or ""
                outcome = activity.get("outcome")
                price = float(activity.get("price") or 0)
                usdc_size = float(activity.get("usdcSize") or 0)
                tx_hash = activity.get("transactionHash")

                text = title.lower()

                is_btc = "bitcoin" in text or "btc" in text

                if (
                    is_btc
                    and usdc_size >= MIN_USDC_SIZE
                    and price >= MIN_PRICE
                    and tx_hash
                ):
                    if tx_hash in sent_signals:
                        continue

                    sent_signals.add(tx_hash)

                    key = f"{title} | {outcome}"

                    if key not in grouped_signals:
                        grouped_signals[key] = {
                            "title": title,
                            "slug": slug,
                            "outcome": outcome,
                            "total_usdc": 0,
                            "prices": [],
                            "count": 0
                        }

                    grouped_signals[key]["total_usdc"] += usdc_size
                    grouped_signals[key]["prices"].append(price)
                    grouped_signals[key]["count"] += 1

            for signal in grouped_signals.values():
                avg_price = sum(signal["prices"]) / len(signal["prices"])

                edge_score = calculate_edge_score(
                    signal["outcome"],
                    avg_price,
                    signal["total_usdc"],
                    signal["count"],
                    btc_signal
                )

                if signal["outcome"] == "No":
                    lecture = "Whale évite fortement ce scénario"
                else:
                    lecture = "Whale privilégie ce scénario"

                edge_detected = edge_score >= 7

                signal_data = {
                    "title": signal["title"],
                    "slug": signal.get("slug") or "",
                    "outcome": signal["outcome"],
                    "total_usdc": signal["total_usdc"],
                    "avg_price": avg_price,
                    "count": signal["count"],
                    "edge_score": edge_score,
                    "lecture": lecture,
                    "paper_trade": edge_detected
                }

                latest_edge_signals.append(signal_data)

                if edge_detected:
                    if not paper_trade_already_exists(
                        signal_data["title"],
                        signal_data["outcome"]
                    ):
                        save_paper_trade(signal_data, btc_price)

                message = f"""
🧠 EDGE ANALYSIS

BTC LIVE : {btc_price}

Marché :
{signal['title']}

Outcome : {signal['outcome']}

Montant :
{signal['total_usdc']:.2f} USDC

Prix moyen :
{avg_price:.3f}

🔥 EDGE SCORE :
{edge_score}/10

Lecture :
{lecture}
"""

                print(message)
                send_telegram_message(message)

        except Exception as e:
            print("Erreur :", e)

        print("Prochain scan dans 60 secondes...")
        time.sleep(60)


@app.get("/", response_class=HTMLResponse)
def dashboard():
    btc_price = get_btc_price()
    btc_signal = get_model_signal(btc_price)

    html = f"""
    <html>
    <head>
        <title>Whale Dashboard</title>
        <meta http-equiv="refresh" content="60">

        <style>
            body {{
                background-color: #111;
                color: white;
                font-family: Arial;
                padding: 20px;
            }}

            .card {{
                background-color: #1c1c1c;
                padding: 15px;
                margin-bottom: 15px;
                border-radius: 10px;
            }}

            h1 {{
                color: orange;
            }}

            .small {{
                color: #aaa;
            }}
        </style>
    </head>

    <body>
        <h1>🐋 Whale Quant Dashboard</h1>

        <div class="card">
            <h2>BTC LIVE</h2>
            <h1>{btc_price}$</h1>
        </div>

        <div class="card">
            <h2>Dernier scan bot</h2>
            <h2>{last_scan_time}</h2>
            <p class="small">Cette heure doit changer environ toutes les 60 secondes.</p>
        </div>

        <div class="card">
            <h2>🧠 MODEL SIGNAL</h2>
            <h2>{btc_signal}</h2>
        </div>

        <h2>🧠 EDGE SIGNALS</h2>
    """

    if not latest_edge_signals:
        html += """
        <div class="card">
            <h3>Aucun signal récent</h3>
        </div>
        """

    for signal in latest_edge_signals:
        html += f"""
        <div class="card">
            <h3>{signal['title']}</h3>
            <p><b>Outcome :</b> {signal['outcome']}</p>
            <p><b>Montant :</b> {signal['total_usdc']:.2f} USDC</p>
            <p><b>Prix moyen :</b> {signal['avg_price']:.3f}</p>
            <p><b>🔥 EDGE SCORE :</b> {signal['edge_score']}/10</p>
            <p><b>Lecture :</b> {signal['lecture']}</p>
            <p><b>Paper Trade :</b> {signal['paper_trade']}</p>
        </div>
        """

    html += """
    </body>
    </html>
    """

    return html


tracker_thread = threading.Thread(
    target=whale_tracker_loop,
    daemon=True
)

tracker_thread.start()