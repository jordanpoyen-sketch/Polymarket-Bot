from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import requests
import threading
import time
import os
import sqlite3
from datetime import datetime


app = FastAPI()

WALLET = "0x6e1d5040d0ac73709b0621f620d2a60b80d2d0fa"

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

MIN_USDC_SIZE = 100
MIN_PRICE = 0.85
EDGE_THRESHOLD = 1
PAPER_TRADE_SIZE = 1

DB_PATH = "/data/paper_trades.db"

sent_signals = set()
latest_edge_signals = []
last_scan_time = "Aucun scan"


# --------------------------
# DATABASE
# --------------------------

def init_db():
    os.makedirs("/data", exist_ok=True)

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS paper_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date_opened TEXT,
            title TEXT,
            slug TEXT,
            outcome TEXT,
            entry_price REAL,
            trade_size REAL,
            shares REAL,
            edge_score INTEGER,
            btc_live_open REAL,
            status TEXT,
            result TEXT,
            pnl REAL
        )
    """)

    conn.commit()
    conn.close()


def paper_trade_already_exists(title, outcome):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT COUNT(*) FROM paper_trades
        WHERE title = ? AND outcome = ? AND status = 'OPEN'
    """, (title, outcome))

    exists = cursor.fetchone()[0] > 0
    conn.close()

    return exists


def save_paper_trade(signal, btc_price):
    entry_price = signal["avg_price"]
    shares = PAPER_TRADE_SIZE / entry_price

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO paper_trades (
            date_opened, title, slug, outcome, entry_price,
            trade_size, shares, edge_score, btc_live_open,
            status, result, pnl
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        signal["title"],
        signal.get("slug") or "",
        signal["outcome"],
        round(entry_price, 4),
        PAPER_TRADE_SIZE,
        round(shares, 4),
        signal["edge_score"],
        btc_price,
        "OPEN",
        "",
        None
    ))

    conn.commit()
    conn.close()

    print("✅ Paper trade sauvegardé en DB")


# --------------------------
# TELEGRAM
# --------------------------

def send_telegram_message(message):
    try:
        if not BOT_TOKEN or not CHAT_ID:
            return

        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

        requests.get(url, params={
            "chat_id": CHAT_ID,
            "text": message
        }, timeout=10)

    except Exception as e:
        print("Erreur Telegram :", e)


# --------------------------
# BTC PRICE
# --------------------------

def get_btc_price():
    try:
        url = "https://api.coinbase.com/v2/prices/BTC-USD/spot"
        response = requests.get(url, timeout=10)
        data = response.json()

        if "data" in data and "amount" in data["data"]:
            return float(data["data"]["amount"])

        print("Erreur Coinbase :", data)
        return 0

    except Exception as e:
        print("Erreur BTC :", e)
        return 0


# --------------------------
# POLYMARKET
# --------------------------

def get_wallet_activity(limit=50):
    try:
        url = "https://data-api.polymarket.com/activity"

        response = requests.get(url, params={
            "user": WALLET,
            "limit": limit,
            "offset": 0
        }, timeout=20)

        if response.status_code != 200:
            print("Erreur activité :", response.text)
            return []

        return response.json()

    except Exception as e:
        print("Erreur activity :", e)
        return []


def get_market_data(slug):
    try:
        if not slug:
            return None

        url = "https://gamma-api.polymarket.com/markets"

        response = requests.get(url, params={
            "slug": slug
        }, timeout=20)

        if response.status_code != 200:
            return None

        data = response.json()

        if not data:
            return None

        return data[0]

    except Exception as e:
        print("Erreur market :", e)
        return None


# --------------------------
# MODEL
# --------------------------

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

    if btc_signal == "bearish" and outcome == "No":
        score += 2

    return min(score, 10)


# --------------------------
# RESOLVER
# --------------------------

def resolve_paper_trades():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT id, slug, outcome, shares, trade_size
        FROM paper_trades
        WHERE status = 'OPEN'
    """)

    open_trades = cursor.fetchall()

    for trade in open_trades:
        trade_id, slug, outcome, shares, trade_size = trade

        market = get_market_data(slug)

        if not market:
            continue

        closed = market.get("closed")

        if not closed:
            continue

        winning_outcome = (
            market.get("winner")
            or market.get("winningOutcome")
            or market.get("outcome")
        )

        if winning_outcome == outcome:
            result = "WIN"
            pnl = round(float(shares) - float(trade_size), 2)
        else:
            result = "LOSS"
            pnl = -float(trade_size)

        cursor.execute("""
            UPDATE paper_trades
            SET status = 'CLOSED', result = ?, pnl = ?
            WHERE id = ?
        """, (result, pnl, trade_id))

        print(f"✅ Trade résolu : {result} | PnL {pnl}")

    conn.commit()
    conn.close()


# --------------------------
# STATS
# --------------------------

def get_backtest_stats():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) FROM paper_trades")
    total = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM paper_trades WHERE status = 'OPEN'")
    open_count = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM paper_trades WHERE status = 'CLOSED'")
    closed_count = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM paper_trades WHERE result = 'WIN'")
    wins = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM paper_trades WHERE result = 'LOSS'")
    losses = cursor.fetchone()[0]

    cursor.execute("SELECT COALESCE(SUM(pnl), 0) FROM paper_trades WHERE status = 'CLOSED'")
    total_pnl = cursor.fetchone()[0]

    winrate = (wins / closed_count * 100) if closed_count > 0 else 0

    cursor.execute("""
        SELECT date_opened, title, outcome, entry_price, edge_score, status, result, pnl
        FROM paper_trades
        ORDER BY id DESC
        LIMIT 20
    """)

    recent = cursor.fetchall()

    conn.close()

    return {
        "total": total,
        "open": open_count,
        "closed": closed_count,
        "wins": wins,
        "losses": losses,
        "pnl": total_pnl,
        "winrate": winrate,
        "recent": recent
    }


# --------------------------
# MAIN LOOP
# --------------------------

def whale_tracker_loop():
    global latest_edge_signals
    global last_scan_time

    init_db()

    while True:
        try:
            last_scan_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            print("\n" + "=" * 60)
            print("SCAN :", last_scan_time)
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

                    key = f"{title}|{outcome}"

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

                lecture = (
                    "Whale évite fortement ce scénario"
                    if signal["outcome"] == "No"
                    else "Whale privilégie ce scénario"
                )

                edge_detected = edge_score >= EDGE_THRESHOLD

                signal_data = {
                    "title": signal["title"],
                    "slug": signal["slug"],
                    "outcome": signal["outcome"],
                    "total_usdc": signal["total_usdc"],
                    "avg_price": avg_price,
                    "count": signal["count"],
                    "edge_score": edge_score,
                    "lecture": lecture,
                    "paper_trade": edge_detected
                }

                latest_edge_signals.append(signal_data)

                paper_saved = False

                if edge_detected:
                    if not paper_trade_already_exists(signal_data["title"], signal_data["outcome"]):
                        save_paper_trade(signal_data, btc_price)
                        paper_saved = True

                message = f"""
🧠 EDGE ANALYSIS

BTC LIVE : {btc_price}

Marché :
{signal['title']}

Outcome :
{signal['outcome']}

Montant :
{signal['total_usdc']:.2f} USDC

Prix moyen :
{avg_price:.3f}

Trades :
{signal['count']}

🔥 EDGE SCORE :
{edge_score}/10

Lecture :
{lecture}
"""

                if paper_saved:
                    message += "\n📄 Nouveau paper trade sauvegardé en DB"

                print(message)
                send_telegram_message(message)

        except Exception as e:
            print("Erreur loop :", e)

        print("Prochain scan dans 60 secondes...")
        time.sleep(60)


# --------------------------
# DASHBOARD
# --------------------------

@app.get("/", response_class=HTMLResponse)
def dashboard():
    init_db()

    btc_price = get_btc_price()
    btc_signal = get_model_signal(btc_price)
    stats = get_backtest_stats()

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

            .win {{
                color: #00ff99;
            }}

            .loss {{
                color: #ff6666;
            }}
        </style>
    </head>

    <body>
        <h1>🐋 Whale Dashboard</h1>

        <div class="card">
            <h2>BTC LIVE</h2>
            <h1>{btc_price}</h1>
        </div>

        <div class="card">
            <h2>Dernier scan</h2>
            <h2>{last_scan_time}</h2>
        </div>

        <div class="card">
            <h2>MODEL SIGNAL</h2>
            <h2>{btc_signal}</h2>
        </div>

        <div class="card">
            <h2>📊 Backtest Stats</h2>
            <p>Total trades : {stats["total"]}</p>
            <p>Open : {stats["open"]}</p>
            <p>Closed : {stats["closed"]}</p>
            <p>Wins : {stats["wins"]}</p>
            <p>Losses : {stats["losses"]}</p>
            <p>Winrate : {stats["winrate"]:.2f}%</p>
            <p>Total PnL : {stats["pnl"]:.2f} USDC</p>
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
            <p><b>EDGE SCORE :</b> {signal['edge_score']}/10</p>
            <p><b>Lecture :</b> {signal['lecture']}</p>
            <p><b>Paper Trade :</b> {signal['paper_trade']}</p>
        </div>
        """

    html += """
        <h2>📄 Derniers Paper Trades</h2>
    """

    for trade in stats["recent"]:
        date_opened, title, outcome, entry_price, edge_score, status, result, pnl = trade

        result_class = "win" if result == "WIN" else "loss"

        html += f"""
        <div class="card">
            <h3>{title}</h3>
            <p>Date : {date_opened}</p>
            <p>Outcome : {outcome}</p>
            <p>Entry : {entry_price}</p>
            <p>Edge score : {edge_score}/10</p>
            <p>Status : {status}</p>
            <p>Result : <span class="{result_class}">{result}</span></p>
            <p>PnL : {pnl}</p>
        </div>
        """

    html += """
    </body>
    </html>
    """

    return html


# --------------------------
# START
# --------------------------

init_db()

tracker_thread = threading.Thread(
    target=whale_tracker_loop,
    daemon=True
)

tracker_thread.start()