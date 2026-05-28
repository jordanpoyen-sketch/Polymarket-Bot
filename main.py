from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import requests
import threading
import time
import os
import sqlite3
import json
from datetime import datetime


app = FastAPI()

WALLET = "0x6e1d5040d0ac73709b0621f620d2a60b80d2d0fa"

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

MIN_USDC_SIZE = 25
MIN_PRICE = 0.50
PAPER_TRADE_SIZE = 1

DB_PATH = "/data/paper_trades.db"

latest_edge_signals = []
last_scan_time = "Aucun scan"


def init_db():
    os.makedirs("/data", exist_ok=True)

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS raw_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date_detected TEXT,
            tx_hash TEXT UNIQUE,
            title TEXT,
            slug TEXT,
            outcome TEXT,
            price REAL,
            usdc_size REAL,
            side TEXT,
            btc_live REAL
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS paper_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date_opened TEXT,
            tx_hash TEXT UNIQUE,
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

    cursor.execute("PRAGMA table_info(raw_trades)")
    raw_columns = [col[1] for col in cursor.fetchall()]

    if "status" not in raw_columns:
        cursor.execute("ALTER TABLE raw_trades ADD COLUMN status TEXT DEFAULT 'OPEN'")
    if "result" not in raw_columns:
        cursor.execute("ALTER TABLE raw_trades ADD COLUMN result TEXT DEFAULT ''")
    if "pnl" not in raw_columns:
        cursor.execute("ALTER TABLE raw_trades ADD COLUMN pnl REAL")
    if "roi" not in raw_columns:
        cursor.execute("ALTER TABLE raw_trades ADD COLUMN roi REAL")
    if "resolved_at" not in raw_columns:
        cursor.execute("ALTER TABLE raw_trades ADD COLUMN resolved_at TEXT")

    cursor.execute("""
        UPDATE raw_trades
        SET status = 'OPEN'
        WHERE status IS NULL OR status = ''
    """)

    cursor.execute("PRAGMA table_info(paper_trades)")
    paper_columns = [col[1] for col in cursor.fetchall()]

    if "tx_hash" not in paper_columns:
        cursor.execute("ALTER TABLE paper_trades ADD COLUMN tx_hash TEXT")

    conn.commit()
    conn.close()


def send_telegram_message(message):
    if not BOT_TOKEN or not CHAT_ID:
        return

    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        requests.get(url, params={"chat_id": CHAT_ID, "text": message}, timeout=10)
    except Exception as e:
        print("Erreur Telegram :", e)


def get_btc_price():
    try:
        url = "https://api.coinbase.com/v2/prices/BTC-USD/spot"
        response = requests.get(url, timeout=10)
        data = response.json()

        if "data" in data and "amount" in data["data"]:
            return float(data["data"]["amount"])

        return 0

    except Exception as e:
        print("Erreur BTC :", e)
        return 0


def get_wallet_activity(limit=50):
    try:
        url = "https://data-api.polymarket.com/activity"
        params = {
            "user": WALLET,
            "limit": limit,
            "offset": 0
        }

        response = requests.get(url, params=params, timeout=20)

        if response.status_code != 200:
            print("Erreur activité :", response.text)
            return []

        return response.json()

    except Exception as e:
        print("Erreur activity :", e)
        return []


def get_market_data(slug):
    if not slug:
        return None

    try:
        url = f"https://gamma-api.polymarket.com/markets/slug/{slug}"
        response = requests.get(url, timeout=20)

        if response.status_code != 200:
            return None

        return response.json()

    except Exception as e:
        print("Erreur market :", e)
        return None


def extract_winning_outcome(market):
    for key in ["winner", "winningOutcome", "outcome", "resolvedOutcome"]:
        value = market.get(key)
        if value in ["Yes", "No"]:
            return value

    outcomes_raw = market.get("outcomes")
    prices_raw = market.get("outcomePrices")

    try:
        outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
        prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw

        if outcomes and prices:
            prices_float = [float(p) for p in prices]
            max_index = prices_float.index(max(prices_float))

            if max(prices_float) >= 0.99:
                return outcomes[max_index]

    except Exception as e:
        print("Erreur extraction winner :", e)

    return None


def get_model_signal(btc_price):
    if btc_price > 78000:
        return "bullish"
    elif btc_price > 76000:
        return "range_bullish"
    elif btc_price > 74000:
        return "neutral"
    else:
        return "bearish"


def calculate_edge_score(outcome, price, usdc_size, btc_signal):
    score = 0

    if usdc_size > 1000:
        score += 3
    elif usdc_size > 500:
        score += 2
    elif usdc_size > 100:
        score += 1

    if price > 0.97:
        score += 3
    elif price > 0.93:
        score += 2
    elif price > 0.88:
        score += 1

    if btc_signal in ["bullish", "range_bullish"] and outcome == "Yes":
        score += 2

    if btc_signal == "bearish" and outcome == "No":
        score += 2

    return min(score, 10)


def raw_trade_exists(tx_hash):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) FROM raw_trades WHERE tx_hash = ?", (tx_hash,))
    exists = cursor.fetchone()[0] > 0

    conn.close()
    return exists


def save_raw_trade(activity, btc_price):
    tx_hash = activity.get("transactionHash")

    if not tx_hash or raw_trade_exists(tx_hash):
        return False

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO raw_trades (
            date_detected,
            tx_hash,
            title,
            slug,
            outcome,
            price,
            usdc_size,
            side,
            btc_live,
            status,
            result,
            pnl,
            roi,
            resolved_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        tx_hash,
        activity.get("title"),
        activity.get("slug") or "",
        activity.get("outcome"),
        float(activity.get("price") or 0),
        float(activity.get("usdcSize") or 0),
        activity.get("side"),
        btc_price,
        "OPEN",
        "",
        None,
        None,
        None
    ))

    conn.commit()
    conn.close()

    return True


def save_paper_trade(activity, btc_price, edge_score):
    tx_hash = activity.get("transactionHash")
    price = float(activity.get("price") or 0)

    if not tx_hash or price <= 0:
        return False

    shares = PAPER_TRADE_SIZE / price

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    try:
        cursor.execute("""
            INSERT INTO paper_trades (
                date_opened,
                tx_hash,
                title,
                slug,
                outcome,
                entry_price,
                trade_size,
                shares,
                edge_score,
                btc_live_open,
                status,
                result,
                pnl
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            tx_hash,
            activity.get("title"),
            activity.get("slug") or "",
            activity.get("outcome"),
            round(price, 4),
            PAPER_TRADE_SIZE,
            round(shares, 4),
            edge_score,
            btc_price,
            "OPEN",
            "",
            None
        ))

        conn.commit()
        conn.close()
        return True

    except sqlite3.IntegrityError:
        conn.close()
        return False


def resolve_raw_trades():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT id, slug, outcome, price, title
        FROM raw_trades
        WHERE status = 'OPEN'
    """)

    open_raw = cursor.fetchall()

    for raw_id, slug, outcome, price, title in open_raw:
        market = get_market_data(slug)

        if not market or not market.get("closed"):
            continue

        winning_outcome = extract_winning_outcome(market)

        if not winning_outcome:
            continue

        if winning_outcome == outcome:
            result = "WIN"
            pnl = round((1 / float(price)) - 1, 4)
            roi = round(((1 - float(price)) / float(price)) * 100, 2)
        else:
            result = "LOSS"
            pnl = -1
            roi = -100

        cursor.execute("""
            UPDATE raw_trades
            SET status = 'CLOSED',
                result = ?,
                pnl = ?,
                roi = ?,
                resolved_at = ?
            WHERE id = ?
        """, (
            result,
            pnl,
            roi,
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            raw_id
        ))

        print(f"✅ RAW résolu : {result} | ROI {roi}% | {title}")

    conn.commit()
    conn.close()


def resolve_paper_trades():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT id, slug, outcome, shares, trade_size, title
        FROM paper_trades
        WHERE status = 'OPEN'
    """)

    open_trades = cursor.fetchall()

    for trade_id, slug, outcome, shares, trade_size, title in open_trades:
        market = get_market_data(slug)

        if not market or not market.get("closed"):
            continue

        winning_outcome = extract_winning_outcome(market)

        if not winning_outcome:
            continue

        if winning_outcome == outcome:
            result = "WIN"
            pnl = round(float(shares) - float(trade_size), 2)
        else:
            result = "LOSS"
            pnl = -float(trade_size)

        cursor.execute("""
            UPDATE paper_trades
            SET status = 'CLOSED',
                result = ?,
                pnl = ?
            WHERE id = ?
        """, (
            result,
            pnl,
            trade_id
        ))

        print(f"✅ PAPER résolu : {result} | PnL {pnl} | {title}")

    conn.commit()
    conn.close()


def get_stats():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) FROM raw_trades")
    raw_total = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM raw_trades WHERE status = 'CLOSED'")
    raw_closed = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM raw_trades WHERE result = 'WIN'")
    raw_wins = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM raw_trades WHERE result = 'LOSS'")
    raw_losses = cursor.fetchone()[0]

    cursor.execute("SELECT COALESCE(AVG(roi), 0) FROM raw_trades WHERE status = 'CLOSED'")
    raw_avg_roi = cursor.fetchone()[0]

    raw_winrate = (raw_wins / raw_closed * 100) if raw_closed else 0

    cursor.execute("""
        SELECT COALESCE(SUM(
            CASE
                WHEN result = 'WIN'
                THEN usdc_size * roi / 100
                ELSE usdc_size * -1
            END
        ), 0)
        FROM raw_trades
        WHERE status = 'CLOSED'
    """)
    weighted_pnl = cursor.fetchone()[0]

    cursor.execute("""
        SELECT COALESCE(SUM(usdc_size), 0)
        FROM raw_trades
        WHERE status = 'CLOSED'
    """)
    total_weight = cursor.fetchone()[0]

    weighted_roi = (
        (weighted_pnl / total_weight) * 100
        if total_weight > 0
        else 0
    )

    cursor.execute("""
        SELECT COALESCE(AVG(usdc_size), 0)
        FROM raw_trades
        WHERE result = 'WIN'
    """)
    avg_win_size = cursor.fetchone()[0]

    cursor.execute("""
        SELECT COALESCE(AVG(usdc_size), 0)
        FROM raw_trades
        WHERE result = 'LOSS'
    """)
    avg_loss_size = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM paper_trades")
    paper_total = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM paper_trades WHERE status = 'OPEN'")
    paper_open = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM paper_trades WHERE status = 'CLOSED'")
    paper_closed = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM paper_trades WHERE result = 'WIN'")
    paper_wins = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM paper_trades WHERE result = 'LOSS'")
    paper_losses = cursor.fetchone()[0]

    cursor.execute("""
        SELECT COALESCE(SUM(pnl), 0)
        FROM paper_trades
        WHERE status = 'CLOSED'
    """)
    paper_pnl = cursor.fetchone()[0]

    paper_winrate = (paper_wins / paper_closed * 100) if paper_closed else 0

    cursor.execute("""
        SELECT date_detected, title, outcome, price, usdc_size, status, result, roi
        FROM raw_trades
        ORDER BY id DESC
        LIMIT 20
    """)
    recent_raw = cursor.fetchall()

    cursor.execute("""
        SELECT date_opened, title, outcome, entry_price, edge_score, status, result, pnl
        FROM paper_trades
        ORDER BY id DESC
        LIMIT 20
    """)
    recent_paper = cursor.fetchall()

    conn.close()

    return {
        "raw_total": raw_total,
        "raw_closed": raw_closed,
        "raw_wins": raw_wins,
        "raw_losses": raw_losses,
        "raw_winrate": raw_winrate,
        "raw_avg_roi": raw_avg_roi,
        "weighted_pnl": weighted_pnl,
        "weighted_roi": weighted_roi,
        "avg_win_size": avg_win_size,
        "avg_loss_size": avg_loss_size,
        "paper_total": paper_total,
        "paper_open": paper_open,
        "paper_closed": paper_closed,
        "paper_wins": paper_wins,
        "paper_losses": paper_losses,
        "paper_pnl": paper_pnl,
        "paper_winrate": paper_winrate,
        "recent_raw": recent_raw,
        "recent_paper": recent_paper
    }


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

            resolve_raw_trades()
            resolve_paper_trades()

            latest_edge_signals = []

            btc_price = get_btc_price()
            btc_signal = get_model_signal(btc_price)

            print("BTC :", btc_price)
            print("Signal modèle :", btc_signal)

            activities = get_wallet_activity(50)
            print("Activités récupérées :", len(activities))

            for activity in activities:
                title = str(activity.get("title"))
                outcome = activity.get("outcome")
                price = float(activity.get("price") or 0)
                usdc_size = float(activity.get("usdcSize") or 0)
                tx_hash = activity.get("transactionHash")
                text = title.lower()

                is_btc = "bitcoin" in text or "btc" in text

                if not (
                    is_btc
                    and usdc_size >= MIN_USDC_SIZE
                    and price >= MIN_PRICE
                    and tx_hash
                ):
                    continue

                is_new = save_raw_trade(activity, btc_price)

                if not is_new:
                    continue

                edge_score = calculate_edge_score(
                    outcome,
                    price,
                    usdc_size,
                    btc_signal
                )

                paper_saved = save_paper_trade(
                    activity,
                    btc_price,
                    edge_score
                )

                lecture = (
                    "Whale évite fortement ce scénario"
                    if outcome == "No"
                    else "Whale privilégie ce scénario"
                )

                signal_data = {
                    "title": title,
                    "outcome": outcome,
                    "total_usdc": usdc_size,
                    "avg_price": price,
                    "count": 1,
                    "edge_score": edge_score,
                    "lecture": lecture,
                    "paper_trade": paper_saved
                }

                latest_edge_signals.append(signal_data)

                message = f"""
🧠 RAW WHALE TRADE

BTC LIVE : {btc_price}

Marché :
{title}

Outcome :
{outcome}

Montant :
{usdc_size:.2f} USDC

Prix :
{price:.3f}

🔥 EDGE SCORE :
{edge_score}/10

📄 Paper trade 1$ :
{paper_saved}

Lecture :
{lecture}
"""

                print(message)
                send_telegram_message(message)

        except Exception as e:
            print("Erreur loop :", e)

        print("Prochain scan dans 60 secondes...")
        time.sleep(60)


@app.get("/", response_class=HTMLResponse)
def dashboard():
    init_db()

    btc_price = get_btc_price()
    btc_signal = get_model_signal(btc_price)
    stats = get_stats()

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
            <h2>Model Signal</h2>
            <h2>{btc_signal}</h2>
        </div>

        <div class="card">
            <h2>📊 Raw Whale Trades</h2>
            <p>Total raw : {stats["raw_total"]}</p>
            <p>Raw closed : {stats["raw_closed"]}</p>
            <p>Raw wins : {stats["raw_wins"]}</p>
            <p>Raw losses : {stats["raw_losses"]}</p>
            <p>Raw winrate : {stats["raw_winrate"]:.2f}%</p>
            <p>Raw average ROI : {stats["raw_avg_roi"]:.2f}%</p>
            <hr>
            <p><b>Weighted whale PnL : {stats["weighted_pnl"]:.2f}</b></p>
            <p><b>Weighted whale ROI : {stats["weighted_roi"]:.2f}%</b></p>
            <p>Average WIN size : {stats["avg_win_size"]:.2f} USDC</p>
            <p>Average LOSS size : {stats["avg_loss_size"]:.2f} USDC</p>
        </div>

        <div class="card">
            <h2>📄 Paper Trades</h2>
            <p>Total paper : {stats["paper_total"]}</p>
            <p>Open : {stats["paper_open"]}</p>
            <p>Closed : {stats["paper_closed"]}</p>
            <p>Wins : {stats["paper_wins"]}</p>
            <p>Losses : {stats["paper_losses"]}</p>
            <p>Winrate : {stats["paper_winrate"]:.2f}%</p>
            <p>Total PnL : {stats["paper_pnl"]:.2f} USDC</p>
        </div>

        <h2>🧠 Derniers signaux</h2>
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
            <p>Outcome : {signal['outcome']}</p>
            <p>Montant : {signal['total_usdc']:.2f} USDC</p>
            <p>Prix : {signal['avg_price']:.3f}</p>
            <p>EDGE SCORE : {signal['edge_score']}/10</p>
            <p>Paper Trade : {signal['paper_trade']}</p>
            <p>{signal['lecture']}</p>
        </div>
        """

    html += "<h2>📊 Derniers raw trades</h2>"

    for trade in stats["recent_raw"]:
        date_detected, title, outcome, price, usdc_size, status, result, roi = trade
        result_class = "win" if result == "WIN" else "loss"

        html += f"""
        <div class="card">
            <h3>{title}</h3>
            <p>Date : {date_detected}</p>
            <p>Outcome : {outcome}</p>
            <p>Prix : {price}</p>
            <p>Montant whale : {usdc_size:.2f} USDC</p>
            <p>Status : {status}</p>
            <p>Result : <span class="{result_class}">{result}</span></p>
            <p>ROI : {roi}</p>
        </div>
        """

    html += "<h2>📄 Derniers paper trades</h2>"

    for trade in stats["recent_paper"]:
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


init_db()

tracker_thread = threading.Thread(
    target=whale_tracker_loop,
    daemon=True
)

tracker_thread.start()