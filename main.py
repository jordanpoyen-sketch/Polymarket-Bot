from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import requests
import threading
import time
import os
import sqlite3
import json
from datetime import datetime, timezone


app = FastAPI()

WALLET = "0x6e1d5040d0ac73709b0621f620d2a60b80d2d0fa"

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

MIN_USDC_SIZE = 25
MIN_PRICE = 0.50
PAPER_TRADE_SIZE = 1
MIN_STRATEGY_TRADES = 20
MIN_COMBO_TRADES = 10

DB_PATH = "/data/paper_trades.db"

latest_edge_signals = []
last_scan_time = "Aucun scan"


def init_db():
    os.makedirs("/data", exist_ok=True)
    conn = sqlite3.connect(
	DB_PATH,
	timeout=30,
	check_same_thread=False
)

    cursor = conn.cursor()

    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")


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

    migrations = {
        "status": "ALTER TABLE raw_trades ADD COLUMN status TEXT DEFAULT 'OPEN'",
        "result": "ALTER TABLE raw_trades ADD COLUMN result TEXT DEFAULT ''",
        "pnl": "ALTER TABLE raw_trades ADD COLUMN pnl REAL",
        "roi": "ALTER TABLE raw_trades ADD COLUMN roi REAL",
        "resolved_at": "ALTER TABLE raw_trades ADD COLUMN resolved_at TEXT",
        "market_type": "ALTER TABLE raw_trades ADD COLUMN market_type TEXT",
        "quality_signal": "ALTER TABLE raw_trades ADD COLUMN quality_signal INTEGER DEFAULT 0",
        "reinforcement_count": "ALTER TABLE raw_trades ADD COLUMN reinforcement_count INTEGER DEFAULT 1",
        "cumulative_size": "ALTER TABLE raw_trades ADD COLUMN cumulative_size REAL DEFAULT 0",
        "time_before_expiry_minutes": "ALTER TABLE raw_trades ADD COLUMN time_before_expiry_minutes REAL",
        "aggressiveness_score": "ALTER TABLE raw_trades ADD COLUMN aggressiveness_score INTEGER DEFAULT 1",
        "entry_timing": "ALTER TABLE raw_trades ADD COLUMN entry_timing TEXT",
        "probability_score": "ALTER TABLE raw_trades ADD COLUMN probability_score REAL",
        "trade_grade": "ALTER TABLE raw_trades ADD COLUMN trade_grade TEXT",
        "expected_edge": "ALTER TABLE raw_trades ADD COLUMN expected_edge TEXT"
    }

    for column, sql in migrations.items():
        if column not in raw_columns:
            cursor.execute(sql)

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
        params = {"user": WALLET, "limit": limit, "offset": 0}
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
    if btc_price > 76000:
        return "range_bullish"
    if btc_price > 74000:
        return "neutral"
    return "bearish"


def classify_market(title):
    text = str(title).lower()

    if "reach" in text:
        return "Reach"
    if "dip" in text:
        return "Dip"
    if "above" in text:
        return "Above"
    if "below" in text:
        return "Below"
    if "between" in text:
        return "Range"

    return "Other"


def is_quality_signal(title, outcome):
    market_type = classify_market(title)

    if market_type == "Dip" and outcome == "No":
        return False
    if outcome == "Yes":
        return True
    if market_type in ["Range", "Reach", "Above"]:
        return True

    return False


def price_bucket(price):
    price = float(price)

    if price < 0.70:
        return "0.50-0.70"
    if price < 0.90:
        return "0.70-0.90"

    return "0.90+"


def reinforcement_bucket(count):
    count = int(count or 1)

    if count <= 1:
        return "1"
    if count <= 3:
        return "2-3"
    if count <= 10:
        return "4-10"

    return "10+"


def cumulative_size_bucket(size):
    size = float(size or 0)

    if size < 500:
        return "<500"
    if size < 2000:
        return "500-2000"
    if size < 5000:
        return "2000-5000"

    return "5000+"


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



def calculate_probability_score(title, outcome, price, reinforcement_count, cumulative_size, quality_signal):
    market_type = classify_market(title)
    price = float(price or 0)
    reinforcement_count = int(reinforcement_count or 1)
    cumulative_size = float(cumulative_size or 0)

    score = 50
    reasons = []

    if quality_signal == 1:
        score += 25
        reasons.append("Quality signal validé")
    else:
        score -= 25
        reasons.append("Signal exclu par le filtre qualité")

    if market_type == "Dip" and outcome == "Yes" and 0.50 <= price < 0.70:
        score += 25
        reasons.append("Setup premium : Dip + Yes + prix 0.50-0.70")

    elif market_type == "Range" and outcome == "Yes" and 0.50 <= price < 0.70:
        score += 20
        reasons.append("Setup fort : Range + Yes + prix 0.50-0.70")

    elif market_type in ["Reach", "Above"] and outcome in ["Yes", "No"] and price >= 0.90:
        score += 10
        reasons.append("Setup historique positif : Reach/Above à forte probabilité")

    if market_type == "Dip" and outcome == "No":
        score -= 40
        reasons.append("Anti-signal fort : Dip + No")

    if outcome == "No" and 0.50 <= price < 0.90:
        score -= 30
        reasons.append("NO mid-price historiquement dangereux")

    if cumulative_size >= 5000:
        score += 15
        reasons.append("Très forte taille cumulée whale")
    elif cumulative_size >= 2000:
        score += 10
        reasons.append("Taille cumulée whale importante")
    elif cumulative_size >= 500:
        score += 5
        reasons.append("Taille cumulée modérée")

    if reinforcement_count >= 10:
        score += 15
        reasons.append("Renforcement très élevé")
    elif reinforcement_count >= 4:
        score += 10
        reasons.append("Renforcement élevé")
    elif reinforcement_count >= 2:
        score += 5
        reasons.append("Renforcement confirmé")

    score = max(0, min(100, score))

    if score >= 85:
        grade = "A+"
        expected_edge = "Very Strong"
    elif score >= 75:
        grade = "A"
        expected_edge = "Strong"
    elif score >= 65:
        grade = "B"
        expected_edge = "Positive"
    elif score >= 50:
        grade = "C"
        expected_edge = "Neutral"
    else:
        grade = "D"
        expected_edge = "Avoid"

    return score, grade, expected_edge, reasons



def parse_iso_datetime(value):
    if not value:
        return None

    try:
        value = value.replace("Z", "+00:00")
        return datetime.fromisoformat(value)
    except Exception:
        return None


def calculate_time_before_expiry_minutes(slug):
    market = get_market_data(slug)

    if not market:
        return None

    end_date = market.get("endDateIso") or market.get("endDate") or market.get("umaEndDate")
    end_dt = parse_iso_datetime(end_date)

    if not end_dt:
        return None

    now = datetime.now(timezone.utc)

    if end_dt.tzinfo is None:
        end_dt = end_dt.replace(tzinfo=timezone.utc)

    diff = end_dt - now
    return round(diff.total_seconds() / 60, 2)


def classify_entry_timing(minutes):
    if minutes is None:
        return "Unknown"
    if minutes < 0:
        return "Post Expiry API"
    if minutes <= 30:
        return "Very Late"
    if minutes <= 120:
        return "Late"
    if minutes <= 720:
        return "Mid"

    return "Early"


def calculate_reinforcement_features(title, outcome):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT COUNT(*), COALESCE(SUM(usdc_size), 0)
        FROM raw_trades
        WHERE title = ?
        AND outcome = ?
    """, (title, outcome))

    count, cumulative_size = cursor.fetchone()
    conn.close()

    return int(count) + 1, float(cumulative_size or 0)


def calculate_aggressiveness_score(title, outcome):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT COUNT(*)
        FROM raw_trades
        WHERE title = ?
        AND outcome = ?
        AND datetime(date_detected) >= datetime('now', '-10 minutes')
    """, (title, outcome))

    recent_count = cursor.fetchone()[0]
    conn.close()

    if recent_count >= 10:
        return 5
    if recent_count >= 5:
        return 4
    if recent_count >= 3:
        return 3
    if recent_count >= 1:
        return 2

    return 1


def backfill_clean_fields():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT id, title, outcome, price, reinforcement_count, cumulative_size
        FROM raw_trades
        WHERE market_type IS NULL
        OR market_type = ''
        OR quality_signal IS NULL
        OR probability_score IS NULL
        OR trade_grade IS NULL
        OR trade_grade = ''
    """)

    rows = cursor.fetchall()

    for raw_id, title, outcome, price, reinforcement_count, cumulative_size in rows:
        market_type = classify_market(title)
        quality_signal = 1 if is_quality_signal(title, outcome) else 0

        probability_score, trade_grade, expected_edge, _ = calculate_probability_score(
            title,
            outcome,
            price,
            reinforcement_count,
            cumulative_size,
            quality_signal
        )

        cursor.execute("""
            UPDATE raw_trades
            SET market_type = ?,
                quality_signal = ?,
                probability_score = ?,
                trade_grade = ?,
                expected_edge = ?
            WHERE id = ?
        """, (
            market_type,
            quality_signal,
            probability_score,
            trade_grade,
            expected_edge,
            raw_id
        ))

    conn.commit()
    conn.close()


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

    title = activity.get("title")
    slug = activity.get("slug") or ""
    outcome = activity.get("outcome")
    price = float(activity.get("price") or 0)
    usdc_size = float(activity.get("usdcSize") or 0)

    market_type = classify_market(title)
    quality_signal = 1 if is_quality_signal(title, outcome) else 0

    reinforcement_count, previous_cumulative_size = calculate_reinforcement_features(title, outcome)
    cumulative_size = previous_cumulative_size + usdc_size

    time_before_expiry = calculate_time_before_expiry_minutes(slug)
    entry_timing = classify_entry_timing(time_before_expiry)
    aggressiveness_score = calculate_aggressiveness_score(title, outcome)

    probability_score, trade_grade, expected_edge, probability_reasons = calculate_probability_score(
        title,
        outcome,
        price,
        reinforcement_count,
        cumulative_size,
        quality_signal
    )

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
            resolved_at,
            market_type,
            quality_signal,
            reinforcement_count,
            cumulative_size,
            time_before_expiry_minutes,
            aggressiveness_score,
            entry_timing,
            probability_score,
            trade_grade,
            expected_edge
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        tx_hash,
        title,
        slug,
        outcome,
        price,
        usdc_size,
        activity.get("side"),
        btc_price,
        "OPEN",
        "",
        None,
        None,
        None,
        market_type,
        quality_signal,
        reinforcement_count,
        cumulative_size,
        time_before_expiry,
        aggressiveness_score,
        entry_timing,
        probability_score,
        trade_grade,
        expected_edge
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
        """, (result, pnl, trade_id))

        print(f"✅ PAPER résolu : {result} | PnL {pnl} | {title}")

    conn.commit()
    conn.close()


def weighted_pnl_for_trade(result, usdc_size, roi):
    if result == "WIN":
        return float(usdc_size or 0) * float(roi or 0) / 100
    if result == "LOSS":
        return -float(usdc_size or 0)
    return 0


def get_category_stats(group_field):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            title,
            outcome,
            price,
            usdc_size,
            result,
            roi,
            market_type,
            quality_signal,
            entry_timing,
            aggressiveness_score,
            reinforcement_count,
            cumulative_size
        FROM raw_trades
        WHERE status = 'CLOSED'
    """)

    rows = cursor.fetchall()
    conn.close()

    groups = {}

    for (
        title,
        outcome,
        price,
        usdc_size,
        result,
        roi,
        market_type,
        quality_signal,
        entry_timing,
        aggressiveness_score,
        reinforcement_count,
        cumulative_size
    ) in rows:

        if group_field == "outcome":
            key = outcome
        elif group_field == "price":
            key = price_bucket(price)
        elif group_field == "market":
            key = market_type or classify_market(title)
        elif group_field == "quality":
            key = "Quality" if quality_signal == 1 else "Excluded"
        elif group_field == "timing":
            key = entry_timing or "Unknown"
        elif group_field == "aggressiveness":
            key = f"Score {aggressiveness_score}"
        elif group_field == "reinforcement":
            key = reinforcement_bucket(reinforcement_count)
        elif group_field == "cumulative_size":
            key = cumulative_size_bucket(cumulative_size)
        else:
            key = "Other"

        if key not in groups:
            groups[key] = {
                "count": 0,
                "wins": 0,
                "losses": 0,
                "roi_sum": 0,
                "weighted_pnl": 0,
                "total_size": 0
            }

        groups[key]["count"] += 1

        if result == "WIN":
            groups[key]["wins"] += 1
        elif result == "LOSS":
            groups[key]["losses"] += 1

        groups[key]["roi_sum"] += float(roi or 0)
        groups[key]["weighted_pnl"] += weighted_pnl_for_trade(result, usdc_size, roi)
        groups[key]["total_size"] += float(usdc_size or 0)

    final = []

    for key, data in groups.items():
        count = data["count"]
        wins = data["wins"]
        total_size = data["total_size"]

        winrate = wins / count * 100 if count else 0
        avg_roi = data["roi_sum"] / count if count else 0
        weighted_roi = data["weighted_pnl"] / total_size * 100 if total_size else 0

        final.append({
            "name": key,
            "count": count,
            "wins": wins,
            "losses": data["losses"],
            "winrate": winrate,
            "avg_roi": avg_roi,
            "weighted_pnl": data["weighted_pnl"],
            "weighted_roi": weighted_roi,
            "total_size": total_size
        })

    return sorted(final, key=lambda x: x["count"], reverse=True)



def get_probability_grade_stats():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT trade_grade, usdc_size, result, roi
        FROM raw_trades
        WHERE status = 'CLOSED'
        AND trade_grade IS NOT NULL
        AND trade_grade != ''
    """)

    rows = cursor.fetchall()
    conn.close()

    groups = {}

    for grade, usdc_size, result, roi in rows:
        key = grade or "Unknown"

        if key not in groups:
            groups[key] = {
                "count": 0,
                "wins": 0,
                "losses": 0,
                "roi_sum": 0,
                "weighted_pnl": 0,
                "total_size": 0
            }

        groups[key]["count"] += 1

        if result == "WIN":
            groups[key]["wins"] += 1
        elif result == "LOSS":
            groups[key]["losses"] += 1

        groups[key]["roi_sum"] += float(roi or 0)
        groups[key]["weighted_pnl"] += weighted_pnl_for_trade(result, usdc_size, roi)
        groups[key]["total_size"] += float(usdc_size or 0)

    final = []

    grade_order = {"A+": 0, "A": 1, "B": 2, "C": 3, "D": 4, "Unknown": 5}

    for key, data in groups.items():
        count = data["count"]
        wins = data["wins"]
        total_size = data["total_size"]

        winrate = wins / count * 100 if count else 0
        avg_roi = data["roi_sum"] / count if count else 0
        weighted_roi = data["weighted_pnl"] / total_size * 100 if total_size else 0

        final.append({
            "name": key,
            "count": count,
            "wins": wins,
            "losses": data["losses"],
            "winrate": winrate,
            "avg_roi": avg_roi,
            "weighted_pnl": data["weighted_pnl"],
            "weighted_roi": weighted_roi,
            "total_size": total_size
        })

    return sorted(final, key=lambda x: grade_order.get(x["name"], 99))




def get_validated_grade_stats():
    all_grades = get_probability_grade_stats()
    target_grades = ["A+", "A", "B"]

    rows = []
    for row in all_grades:
        if row["name"] in target_grades:
            validated = (
                row["count"] >= 10
                and row["weighted_roi"] > 0
                and row["winrate"] >= 60
            )

            row_copy = dict(row)
            row_copy["validated"] = validated
            rows.append(row_copy)

    return rows



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

    cursor.execute("""
        SELECT COALESCE(AVG(roi), 0)
        FROM raw_trades
        WHERE status = 'CLOSED'
    """)
    raw_avg_roi = cursor.fetchone()[0]

    raw_winrate = raw_wins / raw_closed * 100 if raw_closed else 0

    cursor.execute("""
        SELECT result, usdc_size, roi
        FROM raw_trades
        WHERE status = 'CLOSED'
    """)

    closed_rows = cursor.fetchall()

    weighted_pnl = sum(
        weighted_pnl_for_trade(result, usdc_size, roi)
        for result, usdc_size, roi in closed_rows
    )

    total_weight = sum(float(usdc_size or 0) for _, usdc_size, _ in closed_rows)
    weighted_roi = weighted_pnl / total_weight * 100 if total_weight > 0 else 0

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

    paper_winrate = paper_wins / paper_closed * 100 if paper_closed else 0

    cursor.execute("""
        SELECT
            date_detected,
            title,
            outcome,
            price,
            usdc_size,
            status,
            result,
            roi,
            market_type,
            quality_signal,
            reinforcement_count,
            cumulative_size,
            time_before_expiry_minutes,
            aggressiveness_score,
            entry_timing,
            probability_score,
            trade_grade,
            expected_edge
        FROM raw_trades
        ORDER BY id DESC
        LIMIT 20
    """)

    recent_raw = cursor.fetchall()

    cursor.execute("""
        SELECT
            date_opened,
            title,
            outcome,
            entry_price,
            edge_score,
            status,
            result,
            pnl
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
        "recent_paper": recent_paper,
        "by_outcome": get_category_stats("outcome"),
        "by_price": get_category_stats("price"),
        "by_market": get_category_stats("market"),
        "by_quality": get_category_stats("quality"),
        "by_timing": get_category_stats("timing"),
        "by_aggressiveness": get_category_stats("aggressiveness"),
        "by_reinforcement": get_category_stats("reinforcement"),
        "by_cumulative_size": get_category_stats("cumulative_size"),
        "by_probability_grade": get_probability_grade_stats(),
        "validated_grades": get_validated_grade_stats()
    }


def get_advanced_analytics():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            title,
            outcome,
            price,
            usdc_size,
            result,
            roi,
            market_type,
            quality_signal,
            reinforcement_count,
            cumulative_size
        FROM raw_trades
        WHERE status = 'CLOSED'
        ORDER BY id ASC
    """)

    rows = cursor.fetchall()
    conn.close()

    total_curve = []
    quality_curve = []
    excluded_curve = []

    total_pnl = 0
    quality_pnl = 0
    excluded_pnl = 0

    strategies = {}
    feature_combos = {}

    for i, row in enumerate(rows, start=1):
        (
            title,
            outcome,
            price,
            usdc_size,
            result,
            roi,
            market_type,
            quality_signal,
            reinforcement_count,
            cumulative_size
        ) = row

        bucket = price_bucket(price)
        size_bucket = cumulative_size_bucket(cumulative_size)
        quality = "Quality" if quality_signal == 1 else "Excluded"
        clean_market_type = market_type or classify_market(title)

        strategy = f"{quality} | {clean_market_type} | {outcome} | {bucket}"

        combo = (
            f"{quality} | "
            f"{clean_market_type} | "
            f"{outcome} | "
            f"{bucket} | "
            f"Reinforcement {reinforcement_bucket(reinforcement_count)} | "
            f"Size {size_bucket}"
        )

        pnl = weighted_pnl_for_trade(result, usdc_size, roi)

        total_pnl += pnl
        total_curve.append((i, round(total_pnl, 2)))

        if quality_signal == 1:
            quality_pnl += pnl
            quality_curve.append((i, round(quality_pnl, 2)))
        else:
            excluded_pnl += pnl
            excluded_curve.append((i, round(excluded_pnl, 2)))

        if strategy not in strategies:
            strategies[strategy] = {
                "count": 0,
                "wins": 0,
                "losses": 0,
                "weighted_pnl": 0,
                "total_size": 0
            }

        strategies[strategy]["count"] += 1
        strategies[strategy]["total_size"] += float(usdc_size or 0)
        strategies[strategy]["weighted_pnl"] += pnl

        if result == "WIN":
            strategies[strategy]["wins"] += 1
        elif result == "LOSS":
            strategies[strategy]["losses"] += 1

        if combo not in feature_combos:
            feature_combos[combo] = {
                "count": 0,
                "wins": 0,
                "losses": 0,
                "weighted_pnl": 0,
                "total_size": 0
            }

        feature_combos[combo]["count"] += 1
        feature_combos[combo]["total_size"] += float(usdc_size or 0)
        feature_combos[combo]["weighted_pnl"] += pnl

        if result == "WIN":
            feature_combos[combo]["wins"] += 1
        elif result == "LOSS":
            feature_combos[combo]["losses"] += 1

    top_strategies = []

    for name, data in strategies.items():
        count = data["count"]

        if count < MIN_STRATEGY_TRADES:
            continue

        total_size = data["total_size"]
        winrate = data["wins"] / count * 100 if count else 0
        weighted_roi = data["weighted_pnl"] / total_size * 100 if total_size else 0

        top_strategies.append({
            "name": name,
            "count": count,
            "wins": data["wins"],
            "losses": data["losses"],
            "winrate": winrate,
            "weighted_roi": weighted_roi,
            "weighted_pnl": data["weighted_pnl"]
        })

    top_strategies = sorted(top_strategies, key=lambda x: x["weighted_roi"], reverse=True)

    top_feature_combos = []

    for name, data in feature_combos.items():
        count = data["count"]

        if count < MIN_COMBO_TRADES:
            continue

        total_size = data["total_size"]
        winrate = data["wins"] / count * 100 if count else 0
        weighted_roi = data["weighted_pnl"] / total_size * 100 if total_size else 0

        top_feature_combos.append({
            "name": name,
            "count": count,
            "wins": data["wins"],
            "losses": data["losses"],
            "winrate": winrate,
            "weighted_roi": weighted_roi,
            "weighted_pnl": data["weighted_pnl"]
        })

    top_feature_combos = sorted(top_feature_combos, key=lambda x: x["weighted_roi"], reverse=True)

    def rolling_winrate(n):
        sample = rows[-n:]

        if not sample:
            return 0

        wins = sum(1 for r in sample if r[4] == "WIN")
        return wins / len(sample) * 100

    closed_count = len(rows)
    positive_strategies = len([s for s in top_strategies if s["weighted_roi"] > 0])
    best_roi = top_strategies[0]["weighted_roi"] if top_strategies else 0

    confidence_score = min(
        100,
        max(
            0,
            (closed_count / 20)
            + best_roi
            + positive_strategies * 2
        )
    )

    return {
        "top_strategies": top_strategies[:15],
        "top_feature_combos": top_feature_combos[:20],
        "total_curve": total_curve[-50:],
        "quality_curve": quality_curve[-50:],
        "excluded_curve": excluded_curve[-50:],
        "rolling_20": rolling_winrate(20),
        "rolling_50": rolling_winrate(50),
        "rolling_100": rolling_winrate(100),
        "confidence_score": confidence_score
    }


def whale_tracker_loop():
    global latest_edge_signals
    global last_scan_time

    init_db()
    backfill_clean_fields()

    while True:
        try:
            last_scan_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            print("\n" + "=" * 60)
            print("SCAN :", last_scan_time)
            print("=" * 60)

            backfill_clean_fields()
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

                edge_score = calculate_edge_score(outcome, price, usdc_size, btc_signal)
                paper_saved = save_paper_trade(activity, btc_price, edge_score)

                market_type = classify_market(title)
                quality = is_quality_signal(title, outcome)

                reinforcement_count, previous_cumulative_size = calculate_reinforcement_features(title, outcome)
                cumulative_size = previous_cumulative_size + usdc_size
                probability_score, trade_grade, expected_edge, probability_reasons = calculate_probability_score(
                    title,
                    outcome,
                    price,
                    reinforcement_count,
                    cumulative_size,
                    1 if quality else 0
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
                    "paper_trade": paper_saved,
                    "market_type": market_type,
                    "quality": quality,
                    "probability_score": probability_score,
                    "trade_grade": trade_grade,
                    "expected_edge": expected_edge
                }

                latest_edge_signals.append(signal_data)

                message = f"""
🧠 RAW WHALE TRADE

BTC LIVE : {btc_price}

Marché :
{title}

Outcome :
{outcome}

Market type :
{market_type}

Quality signal :
{quality}

AI Probability Score :
{probability_score}/100

Trade Grade :
{trade_grade}

Expected Edge :
{expected_edge}

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



# --------------------------
# PRODUCT ANALYTICS HELPERS
# --------------------------

def get_cross_feature_stats(cross_type):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            title,
            outcome,
            price,
            usdc_size,
            result,
            roi,
            market_type,
            quality_signal,
            reinforcement_count,
            cumulative_size,
            aggressiveness_score,
            probability_score,
            trade_grade
        FROM raw_trades
        WHERE status = 'CLOSED'
    """)

    rows = cursor.fetchall()
    conn.close()

    groups = {}

    for (
        title,
        outcome,
        price,
        usdc_size,
        result,
        roi,
        market_type,
        quality_signal,
        reinforcement_count,
        cumulative_size,
        aggressiveness_score,
        probability_score,
        trade_grade
    ) in rows:
        quality = "Quality" if quality_signal == 1 else "Excluded"

        if cross_type == "quality_reinforcement":
            key = f"{quality} | Reinforcement {reinforcement_bucket(reinforcement_count)}"

        elif cross_type == "quality_aggressiveness":
            key = f"{quality} | Aggressiveness {aggressiveness_score}"

        elif cross_type == "quality_market":
            clean_market_type = market_type or classify_market(title)
            key = f"{quality} | {clean_market_type}"

        elif cross_type == "quality_outcome":
            key = f"{quality} | {outcome}"

        elif cross_type == "grade_market":
            clean_market_type = market_type or classify_market(title)
            key = f"{trade_grade or 'Unknown'} | {clean_market_type}"

        else:
            key = "Other"

        if key not in groups:
            groups[key] = {
                "count": 0,
                "wins": 0,
                "losses": 0,
                "roi_sum": 0,
                "weighted_pnl": 0,
                "total_size": 0
            }

        groups[key]["count"] += 1

        if result == "WIN":
            groups[key]["wins"] += 1
        elif result == "LOSS":
            groups[key]["losses"] += 1

        groups[key]["roi_sum"] += float(roi or 0)
        groups[key]["weighted_pnl"] += weighted_pnl_for_trade(result, usdc_size, roi)
        groups[key]["total_size"] += float(usdc_size or 0)

    final = []

    for key, data in groups.items():
        count = data["count"]
        wins = data["wins"]
        total_size = data["total_size"]

        final.append({
            "name": key,
            "count": count,
            "wins": wins,
            "losses": data["losses"],
            "winrate": wins / count * 100 if count else 0,
            "avg_roi": data["roi_sum"] / count if count else 0,
            "weighted_pnl": data["weighted_pnl"],
            "weighted_roi": data["weighted_pnl"] / total_size * 100 if total_size else 0,
            "total_size": total_size
        })

    return sorted(final, key=lambda x: x["weighted_roi"], reverse=True)


def get_setup_historical_stats(market_type, outcome, price, quality_signal, reinforcement_count, cumulative_size, aggressiveness_score):
    quality_key = 1 if quality_signal else 0

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT result, usdc_size, roi
        FROM raw_trades
        WHERE status = 'CLOSED'
        AND COALESCE(market_type, '') = ?
        AND outcome = ?
        AND quality_signal = ?
    """, (
        market_type,
        outcome,
        quality_key
    ))

    rows = cursor.fetchall()
    conn.close()

    count = len(rows)
    wins = sum(1 for result, _, _ in rows if result == "WIN")
    losses = sum(1 for result, _, _ in rows if result == "LOSS")
    total_size = sum(float(usdc_size or 0) for _, usdc_size, _ in rows)
    weighted_pnl = sum(weighted_pnl_for_trade(result, usdc_size, roi) for result, usdc_size, roi in rows)

    if count == 0 or total_size == 0:
        return {
            "count": 0,
            "wins": 0,
            "losses": 0,
            "winrate": 0,
            "weighted_roi": 0,
            "weighted_pnl": 0,
            "label": "NO HISTORY"
        }

    winrate = wins / count * 100
    weighted_roi = weighted_pnl / total_size * 100

    if count >= 50 and weighted_roi > 5 and winrate >= 75:
        label = "VALIDATED"
    elif count >= 20 and weighted_roi > 0 and winrate >= 60:
        label = "PROMISING"
    elif weighted_roi < 0:
        label = "AVOID"
    else:
        label = "UNPROVEN"

    return {
        "count": count,
        "wins": wins,
        "losses": losses,
        "winrate": winrate,
        "weighted_roi": weighted_roi,
        "weighted_pnl": weighted_pnl,
        "label": label
    }



def calculate_price_risk(price, outcome):
    price = float(price or 0)

    if price <= 0:
        return {"score": 0, "label": "UNKNOWN", "expected_payout_roi": 0}

    expected_payout_roi = ((1 - price) / price) * 100

    if price >= 0.95:
        return {"score": 35, "label": "LOW UPSIDE", "expected_payout_roi": expected_payout_roi}
    if price >= 0.85:
        return {"score": 60, "label": "CONTROLLED", "expected_payout_roi": expected_payout_roi}
    if price >= 0.70:
        return {"score": 80, "label": "BALANCED", "expected_payout_roi": expected_payout_roi}
    if price >= 0.50:
        return {"score": 95, "label": "HIGH UPSIDE", "expected_payout_roi": expected_payout_roi}

    return {"score": 55, "label": "SPECULATIVE", "expected_payout_roi": expected_payout_roi}


def confidence_level(sample_size, historical_roi, winrate):
    sample_size = int(sample_size or 0)
    historical_roi = float(historical_roi or 0)
    winrate = float(winrate or 0)

    if sample_size >= 100 and historical_roi > 5 and winrate >= 75:
        return "🟢 HIGH"
    if sample_size >= 50 and historical_roi > 0 and winrate >= 65:
        return "🟡 MEDIUM"
    if sample_size >= 20 and historical_roi > 0:
        return "🟠 LOW"

    return "🔴 WEAK"


def estimate_expected_roi(historical_roi, probability_score, price, validation_label):
    historical_roi = float(historical_roi or 0)
    probability_score = float(probability_score or 0)
    price = float(price or 0)

    payout_roi = ((1 - price) / price) * 100 if price > 0 else 0
    probability_edge = (probability_score - 50) / 50
    probability_roi = payout_roi * probability_edge

    base = (0.70 * historical_roi) + (0.30 * probability_roi)

    if validation_label == "VALIDATED":
        base *= 1.10
    elif validation_label == "PROMISING":
        base *= 1.00
    elif validation_label == "UNPROVEN":
        base *= 0.70
    elif validation_label == "NO HISTORY":
        base *= 0.50
    elif validation_label == "AVOID":
        base *= 0.20

    return base



def calculate_live_setup_score(probability_score, quality_signal, reinforcement_count, cumulative_size, aggressiveness_score, historical_roi, historical_count, validation_label, price, expected_roi):
    probability_score = float(probability_score or 0)
    reinforcement_count = int(reinforcement_count or 1)
    cumulative_size = float(cumulative_size or 0)
    aggressiveness_score = int(aggressiveness_score or 1)
    historical_roi = float(historical_roi or 0)
    historical_count = int(historical_count or 0)
    expected_roi = float(expected_roi or 0)

    price_info = calculate_price_risk(price, None)
    quality_component = 100 if quality_signal else 0

    if reinforcement_count >= 50:
        reinforcement_component = 100
    elif reinforcement_count >= 20:
        reinforcement_component = 85
    elif reinforcement_count >= 10:
        reinforcement_component = 70
    elif reinforcement_count >= 4:
        reinforcement_component = 45
    elif reinforcement_count >= 2:
        reinforcement_component = 25
    else:
        reinforcement_component = 10

    if cumulative_size >= 50000:
        size_component = 100
    elif cumulative_size >= 20000:
        size_component = 85
    elif cumulative_size >= 5000:
        size_component = 70
    elif cumulative_size >= 2000:
        size_component = 45
    elif cumulative_size >= 500:
        size_component = 25
    else:
        size_component = 10

    aggressiveness_component = min(100, max(0, aggressiveness_score * 20))

    if historical_roi >= 20:
        historical_component = 100
    elif historical_roi >= 10:
        historical_component = 85
    elif historical_roi >= 5:
        historical_component = 70
    elif historical_roi > 0:
        historical_component = 55
    elif historical_roi == 0:
        historical_component = 35
    else:
        historical_component = 0

    if expected_roi >= 25:
        expected_roi_component = 100
    elif expected_roi >= 15:
        expected_roi_component = 85
    elif expected_roi >= 8:
        expected_roi_component = 70
    elif expected_roi > 0:
        expected_roi_component = 55
    else:
        expected_roi_component = 0

    if historical_count >= 300:
        sample_component = 100
    elif historical_count >= 100:
        sample_component = 85
    elif historical_count >= 50:
        sample_component = 70
    elif historical_count >= 20:
        sample_component = 45
    else:
        sample_component = 15

    validation_bonus = {
        "VALIDATED": 4,
        "PROMISING": 1,
        "UNPROVEN": -5,
        "NO HISTORY": -8,
        "AVOID": -25
    }.get(validation_label, 0)

    live_score = (
        0.18 * probability_score
        + 0.14 * quality_component
        + 0.14 * reinforcement_component
        + 0.10 * size_component
        + 0.08 * aggressiveness_component
        + 0.16 * historical_component
        + 0.12 * expected_roi_component
        + 0.05 * price_info["score"]
        + 0.03 * sample_component
        + validation_bonus
    )

    return max(0, min(100, live_score))


def calculate_kelly_fraction(expected_roi, confidence, validation, price_risk):
    expected_roi = float(expected_roi or 0)

    if expected_roi <= 0:
        return 0

    base = expected_roi / 100

    if confidence == "🟢 HIGH":
        confidence_mult = 0.35
    elif confidence == "🟡 MEDIUM":
        confidence_mult = 0.22
    elif confidence == "🟠 LOW":
        confidence_mult = 0.12
    else:
        confidence_mult = 0.05

    validation_mult = {
        "VALIDATED": 1.00,
        "PROMISING": 0.65,
        "UNPROVEN": 0.30,
        "NO HISTORY": 0.15,
        "AVOID": 0.00
    }.get(validation, 0.20)

    price_mult = {
        "HIGH UPSIDE": 1.00,
        "BALANCED": 0.85,
        "CONTROLLED": 0.60,
        "LOW UPSIDE": 0.35,
        "SPECULATIVE": 0.30,
        "UNKNOWN": 0.00
    }.get(price_risk, 0.30)

    kelly = base * confidence_mult * validation_mult * price_mult * 100

    return max(0, min(5, kelly))


def decide_trade_action(live_score, expected_roi, confidence, validation, historical_count, price_risk):
    live_score = float(live_score or 0)
    expected_roi = float(expected_roi or 0)
    historical_count = int(historical_count or 0)

    if (
        live_score >= 80
        and expected_roi >= 10
        and confidence in ["🟢 HIGH", "🟡 MEDIUM"]
        and validation == "VALIDATED"
        and historical_count >= 50
    ):
        return "BUY"

    if (
        live_score >= 72
        and expected_roi >= 6
        and validation in ["VALIDATED", "PROMISING"]
        and historical_count >= 20
    ):
        return "WATCH"

    return "SKIP"


def get_best_trade_right_now(live_setups):
    candidates = [
        setup for setup in live_setups
        if setup.get("action") == "BUY"
    ]

    if not candidates:
        candidates = [
            setup for setup in live_setups
            if setup.get("action") == "WATCH"
        ]

    if not candidates:
        return live_setups[0] if live_setups else None

    return sorted(
        candidates,
        key=lambda x: (
            x["action"] == "BUY",
            x["expected_roi"],
            x["live_score"],
            x["historical_roi"]
        ),
        reverse=True
    )[0]



def get_live_setup_ranking(limit=30):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            id,
            date_detected,
            title,
            outcome,
            price,
            usdc_size,
            market_type,
            quality_signal,
            reinforcement_count,
            cumulative_size,
            aggressiveness_score,
            probability_score,
            trade_grade,
            expected_edge
        FROM raw_trades
        WHERE status = 'OPEN'
        ORDER BY id DESC
        LIMIT 500
    """)

    rows = cursor.fetchall()
    conn.close()

    grouped = {}

    for row in rows:
        (
            raw_id,
            date_detected,
            title,
            outcome,
            price,
            usdc_size,
            market_type,
            quality_signal,
            reinforcement_count,
            cumulative_size,
            aggressiveness_score,
            probability_score,
            trade_grade,
            expected_edge
        ) = row

        clean_market_type = market_type or classify_market(title)
        key = f"{title} | {outcome}"

        if probability_score is None or trade_grade is None:
            probability_score, trade_grade, expected_edge, _ = calculate_probability_score(
                title,
                outcome,
                price,
                reinforcement_count,
                cumulative_size,
                quality_signal
            )

        historical = get_setup_historical_stats(
            clean_market_type,
            outcome,
            price,
            quality_signal,
            reinforcement_count,
            cumulative_size,
            aggressiveness_score
        )

        expected_roi = estimate_expected_roi(
            historical["weighted_roi"],
            probability_score,
            price,
            historical["label"]
        )

        price_info = calculate_price_risk(price, outcome)
        confidence = confidence_level(
            historical["count"],
            historical["weighted_roi"],
            historical["winrate"]
        )

        live_score = calculate_live_setup_score(
            probability_score,
            quality_signal,
            reinforcement_count,
            cumulative_size,
            aggressiveness_score,
            historical["weighted_roi"],
            historical["count"],
            historical["label"],
            price,
            expected_roi
        )

        kelly_fraction = calculate_kelly_fraction(
            expected_roi,
            confidence,
            historical["label"],
            price_info["label"]
        )

        action = decide_trade_action(
            live_score,
            expected_roi,
            confidence,
            historical["label"],
            historical["count"],
            price_info["label"]
        )

        if key not in grouped:
            grouped[key] = {
                "date": date_detected,
                "latest_raw_id": raw_id,
                "title": title,
                "outcome": outcome,
                "price": float(price or 0),
                "min_price": float(price or 0),
                "max_price": float(price or 0),
                "last_size": float(usdc_size or 0),
                "total_size_visible": float(usdc_size or 0),
                "market_type": clean_market_type,
                "quality": bool(quality_signal),
                "reinforcement": int(reinforcement_count or 1),
                "cumulative_size": float(cumulative_size or 0),
                "aggressiveness": int(aggressiveness_score or 1),
                "probability_score": float(probability_score or 0),
                "trade_grade": trade_grade or "N/A",
                "expected_edge": expected_edge or "N/A",
                "signals_visible": 1,
                "historical_count": historical["count"],
                "historical_winrate": historical["winrate"],
                "historical_roi": historical["weighted_roi"],
                "historical_pnl": historical["weighted_pnl"],
                "validation": historical["label"],
                "expected_roi": expected_roi,
                "price_risk": price_info["label"],
                "payout_roi": price_info["expected_payout_roi"],
                "confidence": confidence,
                "kelly_fraction": kelly_fraction,
                "action": action,
                "live_score": live_score
            }
        else:
            item = grouped[key]
            item["signals_visible"] += 1
            item["total_size_visible"] += float(usdc_size or 0)
            item["min_price"] = min(item["min_price"], float(price or 0))
            item["max_price"] = max(item["max_price"], float(price or 0))
            item["price"] = float(price or 0)
            item["last_size"] = float(usdc_size or 0)
            item["reinforcement"] = max(item["reinforcement"], int(reinforcement_count or 1))
            item["cumulative_size"] = max(item["cumulative_size"], float(cumulative_size or 0))
            item["aggressiveness"] = max(item["aggressiveness"], int(aggressiveness_score or 1))
            item["probability_score"] = max(item["probability_score"], float(probability_score or 0))

            if live_score > item["live_score"]:
                item["live_score"] = live_score
                item["expected_roi"] = expected_roi
                item["price_risk"] = price_info["label"]
                item["payout_roi"] = price_info["expected_payout_roi"]
                item["confidence"] = confidence
                item["kelly_fraction"] = kelly_fraction
                item["action"] = action

            if item["date"] < date_detected:
                item["date"] = date_detected
                item["latest_raw_id"] = raw_id

    live = list(grouped.values())

    priority = {
        "VALIDATED": 4,
        "PROMISING": 3,
        "UNPROVEN": 2,
        "NO HISTORY": 1,
        "AVOID": 0
    }

    action_priority = {
        "BUY": 3,
        "WATCH": 2,
        "SKIP": 1
    }

    live = sorted(
        live,
        key=lambda x: (
            action_priority.get(x.get("action"), 0),
            x["live_score"],
            x["expected_roi"],
            priority.get(x["validation"], 0),
            x["historical_roi"],
            x["cumulative_size"]
        ),
        reverse=True
    )

    return live[:limit]


def get_decision_cards():
    live = get_live_setup_ranking(50)
    stats = get_stats()

    best_setup = get_best_trade_right_now(live)
    by_market = stats["by_market"]
    best_market = by_market[0] if by_market else None

    strongest = None
    if live:
        strongest = sorted(live, key=lambda x: x["cumulative_size"], reverse=True)[0]

    return {
        "best_setup": best_setup,
        "best_market": best_market,
        "strongest": strongest
    }


def get_edge_health():
    stats = get_stats()

    quality_rows = {row["name"]: row for row in stats["by_quality"]}
    quality_roi = quality_rows.get("Quality", {}).get("weighted_roi", 0)
    excluded_roi = quality_rows.get("Excluded", {}).get("weighted_roi", 0)

    grade_rows = {row["name"]: row for row in stats.get("by_probability_grade", [])}
    a_roi = grade_rows.get("A", {}).get("weighted_roi", 0)
    ap_roi = grade_rows.get("A+", {}).get("weighted_roi", 0)
    b_roi = grade_rows.get("B", {}).get("weighted_roi", 0)

    score = 50
    score += min(20, max(-20, quality_roi * 2))
    score += min(15, max(-15, stats["weighted_roi"] * 2))
    score += 10 if a_roi > 0 and ap_roi > 0 and b_roi > 0 else -10
    score += 5 if excluded_roi < quality_roi else -5
    score = max(0, min(100, score))

    if score >= 80:
        status = "🟢 Edge robuste"
    elif score >= 60:
        status = "🟡 Edge correct"
    else:
        status = "🔴 Edge fragile"

    return {
        "score": score,
        "status": status,
        "quality_roi": quality_roi,
        "excluded_roi": excluded_roi,
        "weighted_roi": stats["weighted_roi"]
    }



# --------------------------
# PAPER TRADING VISIBILITY
# --------------------------

def get_paper_engine_stats():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) FROM paper_trades")
    total = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM paper_trades WHERE status = 'OPEN'")
    open_count = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM paper_trades WHERE status = 'CLOSED'")
    closed = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM paper_trades WHERE result = 'WIN'")
    wins = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM paper_trades WHERE result = 'LOSS'")
    losses = cursor.fetchone()[0]

    cursor.execute("""
        SELECT COALESCE(SUM(pnl), 0)
        FROM paper_trades
        WHERE status = 'CLOSED'
    """)
    pnl = cursor.fetchone()[0]

    cursor.execute("""
        SELECT date_opened, title, outcome, entry_price, edge_score, result, pnl
        FROM paper_trades
        WHERE status = 'CLOSED'
        ORDER BY id DESC
        LIMIT 1
    """)
    last_closed = cursor.fetchone()

    winrate = wins / closed * 100 if closed else 0
    roi = pnl / closed * 100 if closed else 0

    conn.close()

    return {
        "total": total,
        "open": open_count,
        "closed": closed,
        "wins": wins,
        "losses": losses,
        "winrate": winrate,
        "pnl": pnl,
        "roi": roi,
        "last_closed": last_closed
    }


def get_open_paper_trades(limit=100):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            date_opened,
            title,
            outcome,
            entry_price,
            trade_size,
            shares,
            edge_score,
            btc_live_open,
            status
        FROM paper_trades
        WHERE status = 'OPEN'
        ORDER BY id DESC
        LIMIT ?
    """, (limit,))

    rows = cursor.fetchall()
    conn.close()

    return rows


def get_recent_closed_paper_trades(limit=50):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            date_opened,
            title,
            outcome,
            entry_price,
            trade_size,
            shares,
            edge_score,
            result,
            pnl
        FROM paper_trades
        WHERE status = 'CLOSED'
        ORDER BY id DESC
        LIMIT ?
    """, (limit,))

    rows = cursor.fetchall()
    conn.close()

    return rows


def render_paper_engine_card():
    paper = get_paper_engine_stats()

    last = "Aucun trade fermé"
    if paper["last_closed"]:
        date_opened, title, outcome, entry_price, edge_score, result, pnl = paper["last_closed"]
        last = f"""
            {result} | PnL {float(pnl or 0):.2f} USDC<br>
            {title}<br>
            Outcome : {outcome} | Entry : {float(entry_price or 0):.3f}
        """

    html = f"""
    <div class="section">
        <h2>📄 Paper Trading Engine</h2>
        <div class="grid">
            {render_kpi("Total paper trades", paper["total"])}
            {render_kpi("Open paper trades", paper["open"])}
            {render_kpi("Closed paper trades", paper["closed"])}
            {render_kpi("Paper wins", paper["wins"])}
            {render_kpi("Paper losses", paper["losses"])}
            {render_kpi("Paper winrate", f"{paper["winrate"]:.2f}", "%")}
            {render_kpi("Paper PnL", f"{paper["pnl"]:.2f}", " USDC", roi_class(paper["pnl"]))}
            {render_kpi("Paper ROI approx.", f"{paper["roi"]:.2f}", "%", roi_class(paper["roi"]))}
        </div>
        <div class="kpi">
            <div class="label">Last closed paper trade</div>
            <div class="small">{last}</div>
        </div>
    </div>
    """

    return html


def render_open_paper_trades_table(rows):
    html = """
    <div class="section">
        <h2>📌 Paper Trades ouverts à suivre avec 1$</h2>
        <p class="small">
            Ces lignes correspondent aux trades que le bot a ouverts en paper trading.
            Tu peux les utiliser comme liste de suivi manuel.
        </p>
        <table>
            <tr>
                <th>Date</th>
                <th>Market</th>
                <th>Outcome</th>
                <th>Entry Price</th>
                <th>Paper Size</th>
                <th>Shares</th>
                <th>Edge Score</th>
                <th>BTC Open</th>
                <th>Status</th>
            </tr>
    """

    if not rows:
        html += """
            <tr>
                <td colspan="9">Aucun paper trade ouvert actuellement.</td>
            </tr>
        """

    for row in rows:
        (
            date_opened,
            title,
            outcome,
            entry_price,
            trade_size,
            shares,
            edge_score,
            btc_live_open,
            status
        ) = row

        html += f"""
            <tr>
                <td>{date_opened}</td>
                <td>{title}</td>
                <td><b>{outcome}</b></td>
                <td>{float(entry_price or 0):.3f}</td>
                <td>{float(trade_size or 0):.2f} USDC</td>
                <td>{float(shares or 0):.4f}</td>
                <td>{edge_score}/10</td>
                <td>{float(btc_live_open or 0):.2f}</td>
                <td>{status}</td>
            </tr>
        """

    html += """
        </table>
    </div>
    """

    return html


def render_closed_paper_trades_table(rows):
    html = """
    <div class="section">
        <h2>✅ Derniers Paper Trades fermés</h2>
        <table>
            <tr>
                <th>Date</th>
                <th>Market</th>
                <th>Outcome</th>
                <th>Entry Price</th>
                <th>Size</th>
                <th>Shares</th>
                <th>Edge Score</th>
                <th>Result</th>
                <th>PnL</th>
            </tr>
    """

    if not rows:
        html += """
            <tr>
                <td colspan="9">Aucun paper trade fermé.</td>
            </tr>
        """

    for row in rows:
        date_opened, title, outcome, entry_price, trade_size, shares, edge_score, result, pnl = row

        html += f"""
            <tr>
                <td>{date_opened}</td>
                <td>{title}</td>
                <td>{outcome}</td>
                <td>{float(entry_price or 0):.3f}</td>
                <td>{float(trade_size or 0):.2f}</td>
                <td>{float(shares or 0):.4f}</td>
                <td>{edge_score}/10</td>
                <td>{result}</td>
                <td class="{roi_class(pnl)}">{float(pnl or 0):.2f}</td>
            </tr>
        """

    html += """
        </table>
    </div>
    """

    return html


# --------------------------
# UI HELPERS
# --------------------------

def html_header(title):
    return f"""
    <html>
    <head>
        <title>{title}</title>
        <meta http-equiv="refresh" content="60">
        <style>
            body {{
                background-color: #0f0f0f;
                color: white;
                font-family: Arial, sans-serif;
                padding: 18px;
                margin: 0;
            }}
            .nav {{
                display: flex;
                gap: 10px;
                margin-bottom: 18px;
                flex-wrap: wrap;
            }}
            .nav a {{
                color: white;
                background: #242424;
                padding: 10px 14px;
                border-radius: 8px;
                text-decoration: none;
                font-weight: bold;
            }}
            .nav a:hover {{
                background: #333;
            }}
            .section {{
                background-color: #1b1b1b;
                padding: 16px;
                margin-bottom: 16px;
                border-radius: 12px;
                border: 1px solid #2c2c2c;
            }}
            .grid {{
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
                gap: 12px;
                margin-bottom: 16px;
            }}
            .kpi {{
                background: #181818;
                padding: 14px;
                border-radius: 10px;
                border: 1px solid #333;
            }}
            .kpi .label {{
                color: #bbb;
                font-size: 13px;
            }}
            .kpi .value {{
                font-size: 26px;
                font-weight: bold;
                margin-top: 6px;
            }}
            h1 {{
                color: orange;
                margin-top: 0;
            }}
            h2 {{
                margin-top: 0;
            }}
            table {{
                width: 100%;
                color: white;
                border-collapse: collapse;
                font-size: 14px;
            }}
            th, td {{
                border: 1px solid #555;
                padding: 7px;
                vertical-align: top;
            }}
            th {{
                background: #222;
            }}
            .positive {{
                color: #00ff99;
                font-weight: bold;
            }}
            .negative {{
                color: #ff7777;
                font-weight: bold;
            }}
            .neutral {{
                color: #ddd;
            }}
            .small {{
                color: #bbb;
                font-size: 13px;
            }}
            .tag {{
                display: inline-block;
                padding: 3px 8px;
                border-radius: 999px;
                background: #333;
                margin-right: 4px;
                font-size: 12px;
            }}
        </style>
    </head>
    <body>
        <div class="nav">
            <a href="/">Dashboard</a>
            <a href="/analytics">Analytics</a>
            <a href="/setups">Live Setups</a>
            <a href="/ml">ML</a>
            <a href="/ml-performance">ML Performance</a>
            <a href="/paper">Paper Trades</a>
        </div>
    """


def html_footer():
    return """
    </body>
    </html>
    """


def roi_class(value):
    try:
        value = float(value)
    except Exception:
        value = 0

    if value > 0:
        return "positive"
    if value < 0:
        return "negative"
    return "neutral"


def render_kpi(label, value, suffix="", css_class=""):
    return f"""
        <div class="kpi">
            <div class="label">{label}</div>
            <div class="value {css_class}">{value}{suffix}</div>
        </div>
    """


def render_category_table(title, rows, limit=None):
    display_rows = rows[:limit] if limit else rows

    html = f"""
    <div class="section">
        <h2>{title}</h2>
        <table>
            <tr>
                <th>Catégorie</th>
                <th>Trades</th>
                <th>Wins</th>
                <th>Losses</th>
                <th>Winrate</th>
                <th>Avg ROI</th>
                <th>Weighted ROI</th>
                <th>Weighted PnL</th>
            </tr>
    """

    if not display_rows:
        html += """
            <tr>
                <td colspan="8">Aucune donnée disponible.</td>
            </tr>
        """

    for row in display_rows:
        html += f"""
            <tr>
                <td>{row['name']}</td>
                <td>{row['count']}</td>
                <td>{row['wins']}</td>
                <td>{row['losses']}</td>
                <td>{row['winrate']:.2f}%</td>
                <td class="{roi_class(row['avg_roi'])}">{row['avg_roi']:.2f}%</td>
                <td class="{roi_class(row['weighted_roi'])}">{row['weighted_roi']:.2f}%</td>
                <td class="{roi_class(row['weighted_pnl'])}">{row['weighted_pnl']:.2f}</td>
            </tr>
        """

    html += """
        </table>
    </div>
    """

    return html


def render_validated_grades_table(rows):
    html = """
    <div class="section">
        <h2>✅ Validation A+ / A / B</h2>
        <table>
            <tr>
                <th>Grade</th>
                <th>Trades fermés</th>
                <th>Wins</th>
                <th>Losses</th>
                <th>Winrate</th>
                <th>Weighted ROI</th>
                <th>Weighted PnL</th>
                <th>Validation</th>
            </tr>
    """

    if not rows:
        html += """
            <tr>
                <td colspan="8">Pas encore assez de trades A+ / A / B fermés.</td>
            </tr>
        """

    for row in rows:
        validation = "✅ VALIDÉ" if row.get("validated") else "⏳ À CONFIRMER"

        html += f"""
            <tr>
                <td>{row['name']}</td>
                <td>{row['count']}</td>
                <td>{row['wins']}</td>
                <td>{row['losses']}</td>
                <td>{row['winrate']:.2f}%</td>
                <td class="{roi_class(row['weighted_roi'])}">{row['weighted_roi']:.2f}%</td>
                <td class="{roi_class(row['weighted_pnl'])}">{row['weighted_pnl']:.2f}</td>
                <td>{validation}</td>
            </tr>
        """

    html += """
        </table>
        <p class="small">Critère validation : minimum 10 trades fermés, Weighted ROI positif, Winrate ≥ 60%.</p>
    </div>
    """

    return html


def render_curve(title, curve):
    html = f"""
    <div class="section">
        <h2>{title}</h2>
    """

    if not curve:
        html += "<p>Aucune donnée</p>"
    else:
        for point, pnl in curve:
            html += f"<p>Trade {point} : <span class='{roi_class(pnl)}'>{pnl}</span></p>"

    html += """
    </div>
    """

    return html


def validation_badge(label):
    if label == "VALIDATED":
        return "🟢 VALIDATED"
    if label == "PROMISING":
        return "🟡 PROMISING"
    if label == "AVOID":
        return "🔴 AVOID"
    if label == "NO HISTORY":
        return "⚪ NO HISTORY"
    return "🟠 UNPROVEN"


def render_decision_cards(cards):
    best_setup = cards.get("best_setup")
    best_market = cards.get("best_market")
    strongest = cards.get("strongest")

    best_setup_html = "Aucun setup ouvert"
    if best_setup:
        best_setup_html = f"""
            <b>{best_setup['trade_grade']} — {best_setup['title']}</b><br>
            Outcome : {best_setup['outcome']}<br>
            Live Score : {best_setup['live_score']:.1f}/100<br>
            Action : <b>{best_setup['action']}</b><br>
            Kelly conseillé : {best_setup['kelly_fraction']:.2f}% bankroll<br>
            Expected ROI : {best_setup['expected_roi']:.2f}%<br>
            Historical ROI : {best_setup['historical_roi']:.2f}%<br>
            Confidence : {best_setup['confidence']}<br>
            Validation : {validation_badge(best_setup['validation'])}
        """

    best_market_html = "Aucune donnée"
    if best_market:
        best_market_html = f"""
            <b>{best_market['name']}</b><br>
            Weighted ROI : {best_market['weighted_roi']:.2f}%<br>
            Winrate : {best_market['winrate']:.2f}%<br>
            Trades : {best_market['count']}
        """

    strongest_html = "Aucun setup ouvert"
    if strongest:
        strongest_html = f"""
            <b>{strongest['title']}</b><br>
            Outcome : {strongest['outcome']}<br>
            Cum Size : {strongest['cumulative_size']:.2f} USDC<br>
            Reinforcement : {strongest['reinforcement']}
        """

    return f"""
    <div class="grid">
        <div class="kpi">
            <div class="label">🔥 Best Setup Now</div>
            <div class="small">{best_setup_html}</div>
        </div>
        <div class="kpi">
            <div class="label">📈 Best Market Type</div>
            <div class="small">{best_market_html}</div>
        </div>
        <div class="kpi">
            <div class="label">🐋 Strongest Whale Conviction</div>
            <div class="small">{strongest_html}</div>
        </div>
    </div>
    """



def action_badge(action):
    if action == "BUY":
        return "🟢 BUY"
    if action == "WATCH":
        return "🟡 WATCH"
    return "⚪ SKIP"



def render_live_setups_table(rows):
    html = """
    <div class="section">
        <h2>🔥 Top Live Setups V4</h2>
        <p class="small">
            Classement final : Action, Kelly %, Live Score, Expected ROI, Confidence, Price Risk,
            ROI historique et validation statistique.
        </p>
        <table>
            <tr>
                <th>Rank</th>
                <th>Action</th>
                <th>Kelly %</th>
                <th>Live Score</th>
                <th>Expected ROI</th>
                <th>Confidence</th>
                <th>Validation</th>
                <th>Grade</th>
                <th>Market</th>
                <th>Outcome</th>
                <th>Prix</th>
                <th>Price Risk</th>
                <th>Type</th>
                <th>Quality</th>
                <th>Reinf.</th>
                <th>Cum Size</th>
                <th>Agg.</th>
                <th>Hist. Trades</th>
                <th>Hist. ROI</th>
                <th>Hist. Winrate</th>
            </tr>
    """

    if not rows:
        html += """
            <tr>
                <td colspan="20">Aucun trade ouvert actuellement.</td>
            </tr>
        """

    for idx, row in enumerate(rows, start=1):
        quality = "✅" if row["quality"] else "❌"
        price_display = f"{row['price']:.3f}"

        if row["min_price"] != row["max_price"]:
            price_display = f"{row['min_price']:.3f} → {row['max_price']:.3f}"

        html += f"""
            <tr>
                <td>{idx}</td>
                <td><b>{action_badge(row['action'])}</b></td>
                <td><b>{row['kelly_fraction']:.2f}%</b></td>
                <td><b>{row['live_score']:.1f}</b></td>
                <td class="{roi_class(row['expected_roi'])}"><b>{row['expected_roi']:.2f}%</b></td>
                <td>{row['confidence']}</td>
                <td>{validation_badge(row['validation'])}</td>
                <td><b>{row['trade_grade']}</b></td>
                <td>{row['title']}<br><span class="small">Signals visibles : {row['signals_visible']} | Dernier : {row['date']}</span></td>
                <td>{row['outcome']}</td>
                <td>{price_display}</td>
                <td>{row['price_risk']}<br><span class="small">Payout ROI : {row['payout_roi']:.2f}%</span></td>
                <td>{row['market_type']}</td>
                <td>{quality}</td>
                <td>{row['reinforcement']}</td>
                <td>{row['cumulative_size']:.2f}</td>
                <td>{row['aggressiveness']}/5</td>
                <td>{row['historical_count']}</td>
                <td class="{roi_class(row['historical_roi'])}">{row['historical_roi']:.2f}%</td>
                <td>{row['historical_winrate']:.2f}%</td>
            </tr>
        """

    html += """
        </table>
        <p class="small">
            Règle Action : BUY = Score ≥80, Expected ROI ≥10%, HIGH/MEDIUM confidence, VALIDATED et ≥50 trades historiques.
            WATCH = signal intéressant mais pas encore optimal. SKIP = ignorer.
        </p>
    </div>
    """

    return html


# --------------------------
# ROUTES# --------------------------
# ROUTES
# --------------------------

@app.get("/", response_class=HTMLResponse)
def dashboard():
    init_db()
    backfill_clean_fields()

    btc_price = get_btc_price()
    btc_signal = get_model_signal(btc_price)
    stats = get_stats()
    health = get_edge_health()
    live_setups = get_live_setup_ranking(10)
    decision_cards = get_decision_cards()

    html = html_header("Whale Dashboard")

    html += f"""
        <h1>🐋 Whale Dashboard</h1>

        <div class="grid">
            {render_kpi("BTC LIVE", f"{btc_price:.2f}")}
            {render_kpi("Model Signal", btc_signal)}
            {render_kpi("Edge Health", health["status"])}
            {render_kpi("Edge Score", f"{health['score']:.0f}", "/100")}
        </div>

        <div class="section">
            <h2>📌 Résumé décisionnel</h2>
            <div class="grid">
                {render_kpi("Raw closed", stats["raw_closed"])}
                {render_kpi("Raw winrate", f"{stats['raw_winrate']:.2f}", "%")}
                {render_kpi("Weighted whale ROI", f"{stats['weighted_roi']:.2f}", "%", roi_class(stats["weighted_roi"]))}
                {render_kpi("Weighted whale PnL", f"{stats['weighted_pnl']:.2f}", "", roi_class(stats["weighted_pnl"]))}
                {render_kpi("Paper PnL", f"{stats['paper_pnl']:.2f}", " USDC", roi_class(stats["paper_pnl"]))}
                {render_kpi("Last scan", last_scan_time)}
            </div>
        </div>
    """

    html += render_paper_engine_card()

    html += render_decision_cards(decision_cards)
    html += render_live_setups_table(live_setups)

    html += render_category_table("✅ Quality Signal — validation principale", stats["by_quality"])
    html += render_category_table("🧠 Probability Grade Analytics", stats["by_probability_grade"])
    html += render_validated_grades_table(stats["validated_grades"])

    html += """
        <div class="section">
            <h2>📊 Marché & Prix</h2>
            <p class="small">Ces tableaux servent à comprendre le régime dominant. Les décisions doivent surtout venir des combinaisons de features.</p>
        </div>
    """

    html += render_category_table("📊 Analyse par type de marché", stats["by_market"])
    html += render_category_table("📊 Analyse par prix", stats["by_price"])

    html += html_footer()
    return html


@app.get("/analytics", response_class=HTMLResponse)
def analytics():
    init_db()
    backfill_clean_fields()

    stats = get_stats()
    data = get_advanced_analytics()

    html = html_header("Whale Analytics")

    html += """
        <h1>📊 Advanced Whale Analytics</h1>
        <div class="section">
            <h2>Ordre de lecture</h2>
            <p>1. Valider que Quality reste positif.</p>
            <p>2. Vérifier les grades A+ / A / B.</p>
            <p>3. Lire les matrices Quality × Reinforcement et Quality × Aggressiveness.</p>
            <p>4. Utiliser les Top Live Setups pour prioriser les opportunités ouvertes.</p>
        </div>
    """

    html += f"""
        <div class="grid">
            {render_kpi("Confidence Score", f"{data["confidence_score"]:.1f}", "/100")}
            {render_kpi("Rolling 20", f"{data["rolling_20"]:.2f}", "%")}
            {render_kpi("Rolling 50", f"{data["rolling_50"]:.2f}", "%")}
            {render_kpi("Rolling 100", f"{data["rolling_100"]:.2f}", "%")}
        </div>
    """

    html += render_category_table("🥇 P1 — Quality × Reinforcement", get_cross_feature_stats("quality_reinforcement"))
    html += render_category_table("🥇 P1 — Quality × Aggressiveness", get_cross_feature_stats("quality_aggressiveness"))
    html += render_category_table("🥇 P1 — Quality × Market Type", get_cross_feature_stats("quality_market"))
    html += render_category_table("🥇 P1 — Quality × Outcome", get_cross_feature_stats("quality_outcome"))

    html += render_category_table("🔁 Reinforcement Analytics", stats["by_reinforcement"])
    html += render_category_table("🔥 Aggressiveness Analytics", stats["by_aggressiveness"])
    html += render_category_table("💰 Cumulative Size Analytics", stats["by_cumulative_size"])
    html += render_category_table("🕒 Entry Timing Analytics", stats["by_timing"])

    html += render_curve("📉 Total Cumulative PnL — last 50", data["total_curve"])
    html += render_curve("✅ Quality Cumulative PnL — last 50", data["quality_curve"])
    html += render_curve("❌ Excluded Cumulative PnL — last 50", data["excluded_curve"])

    html += """
        <div class="section">
            <h2>🏆 Top Strategies min 20 trades</h2>
            <table>
                <tr>
                    <th>Strategy</th>
                    <th>Trades</th>
                    <th>Wins</th>
                    <th>Losses</th>
                    <th>Winrate</th>
                    <th>Weighted ROI</th>
                    <th>Weighted PnL</th>
                </tr>
    """

    for s in data["top_strategies"]:
        html += f"""
                <tr>
                    <td>{s["name"]}</td>
                    <td>{s["count"]}</td>
                    <td>{s["wins"]}</td>
                    <td>{s["losses"]}</td>
                    <td>{s["winrate"]:.2f}%</td>
                    <td class="{roi_class(s["weighted_roi"])}">{s["weighted_roi"]:.2f}%</td>
                    <td class="{roi_class(s["weighted_pnl"])}">{s["weighted_pnl"]:.2f}</td>
                </tr>
        """

    html += """
            </table>
        </div>
    """

    html += """
        <div class="section">
            <h2>🧠 Feature Combination Analytics min 10 trades</h2>
            <table>
                <tr>
                    <th>Combination</th>
                    <th>Trades</th>
                    <th>Wins</th>
                    <th>Losses</th>
                    <th>Winrate</th>
                    <th>Weighted ROI</th>
                    <th>Weighted PnL</th>
                </tr>
    """

    for s in data["top_feature_combos"]:
        html += f"""
                <tr>
                    <td>{s["name"]}</td>
                    <td>{s["count"]}</td>
                    <td>{s["wins"]}</td>
                    <td>{s["losses"]}</td>
                    <td>{s["winrate"]:.2f}%</td>
                    <td class="{roi_class(s["weighted_roi"])}">{s["weighted_roi"]:.2f}%</td>
                    <td class="{roi_class(s["weighted_pnl"])}">{s["weighted_pnl"]:.2f}</td>
                </tr>
        """

    html += """
            </table>
            <p class="small">
                Final Score = 35% Live Score V4 + 35% ML Win calibré + 15% Expected ROI + 15% Historical ROI.
                ML Edge = ML Win % - Winrate historique du setup.
            </p>
        </div>
    """

    html += html_footer()
    return html


@app.get("/setups", response_class=HTMLResponse)
def setups():
    init_db()
    backfill_clean_fields()

    live_setups = get_live_setup_ranking(50)
    decision_cards = get_decision_cards()

    html = html_header("Live Setups")
    html += """
        <h1>🔥 Live Setup Ranking</h1>
        <div class="section">
            <h2>Objectif</h2>
            <p>Cette page classe les trades ouverts selon le Probability Score, la qualité, le reinforcement, la taille cumulée et l'agressivité.</p>
            <p class="small">À utiliser en priorité pour voir les opportunités actuellement les plus intéressantes.</p>
        </div>
    """
    html += render_paper_engine_card()

    html += render_decision_cards(decision_cards)
    html += render_live_setups_table(live_setups)
    html += html_footer()

    return html


# --------------------------
# XGBOOST ML SHADOW MODE
# --------------------------

def encode_market_type(value):
    mapping = {
        "Dip": 1,
        "Range": 2,
        "Reach": 3,
        "Above": 4,
        "Below": 5,
        "Other": 0
    }
    return mapping.get(value or "Other", 0)


def encode_outcome(value):
    mapping = {
        "Yes": 1,
        "No": 0,
        "Down": 2,
        "Up": 3
    }
    return mapping.get(value, -1)


def encode_entry_timing(value):
    mapping = {
        "Very Late": 5,
        "Late": 4,
        "Mid": 3,
        "Early": 2,
        "Post Expiry API": 1,
        "Unknown": 0
    }
    return mapping.get(value or "Unknown", 0)


def build_ml_dataset():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            title,
            outcome,
            price,
            usdc_size,
            market_type,
            quality_signal,
            reinforcement_count,
            cumulative_size,
            aggressiveness_score,
            entry_timing,
            probability_score,
            result
        FROM raw_trades
        WHERE status = 'CLOSED'
        AND result IN ('WIN', 'LOSS')
    """)

    rows = cursor.fetchall()
    conn.close()

    X = []
    y = []

    for row in rows:
        (
            title,
            outcome,
            price,
            usdc_size,
            market_type,
            quality_signal,
            reinforcement_count,
            cumulative_size,
            aggressiveness_score,
            entry_timing,
            probability_score,
            result
        ) = row

        inferred_market_type = market_type or classify_market(title)
        p = float(price or 0)
        size = float(usdc_size or 0)
        reinforcement = int(reinforcement_count or 1)
        cumulative = float(cumulative_size or 0)
        aggressive = int(aggressiveness_score or 1)
        prob = float(probability_score or 50)

        features = [
            p,
            size,
            encode_market_type(inferred_market_type),
            encode_outcome(outcome),
            int(quality_signal or 0),
            reinforcement,
            cumulative,
            aggressive,
            encode_entry_timing(entry_timing),
            prob,
            1 if p < 0.70 else 0,
            1 if 0.70 <= p < 0.90 else 0,
            1 if p >= 0.90 else 0,
            1 if inferred_market_type == "Dip" and outcome == "Yes" else 0,
            1 if inferred_market_type == "Dip" and outcome == "No" else 0,
            1 if inferred_market_type == "Range" and outcome == "Yes" else 0,
            1 if inferred_market_type == "Above" and outcome == "No" else 0,
            1 if inferred_market_type == "Reach" and outcome == "No" else 0
        ]

        X.append(features)
        y.append(1 if result == "WIN" else 0)

    return X, y


def get_ml_feature_names():
    return [
        "price",
        "usdc_size",
        "market_type",
        "outcome",
        "quality_signal",
        "reinforcement_count",
        "cumulative_size",
        "aggressiveness_score",
        "entry_timing",
        "probability_score",
        "price_lt_070",
        "price_070_090",
        "price_gte_090",
        "dip_yes",
        "dip_no",
        "range_yes",
        "above_no",
        "reach_no"
    ]


def features_from_live_setup(setup):
    p = float(setup.get("price") or 0)
    market_type = setup.get("market_type") or "Other"
    outcome = setup.get("outcome")

    return [
        p,
        float(setup.get("last_size") or 0),
        encode_market_type(market_type),
        encode_outcome(outcome),
        1 if setup.get("quality") else 0,
        int(setup.get("reinforcement") or 1),
        float(setup.get("cumulative_size") or 0),
        int(setup.get("aggressiveness") or 1),
        0,
        float(setup.get("probability_score") or 50),
        1 if p < 0.70 else 0,
        1 if 0.70 <= p < 0.90 else 0,
        1 if p >= 0.90 else 0,
        1 if market_type == "Dip" and outcome == "Yes" else 0,
        1 if market_type == "Dip" and outcome == "No" else 0,
        1 if market_type == "Range" and outcome == "Yes" else 0,
        1 if market_type == "Above" and outcome == "No" else 0,
        1 if market_type == "Reach" and outcome == "No" else 0
    ]



def ensure_ml_predictions_table():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS ml_predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            prediction_date TEXT,
            raw_trade_id INTEGER UNIQUE,
            title TEXT,
            outcome TEXT,
            market_type TEXT,
            price REAL,
            probability_score REAL,
            live_score REAL,
            expected_roi REAL,
            kelly_fraction REAL,
            historical_roi REAL,
            historical_winrate REAL,
            historical_count INTEGER,
            stat_action TEXT,
            ml_action TEXT,
            ml_win_probability REAL,
            ml_edge REAL,
            ml_grade TEXT,
            final_score REAL,
            validation TEXT,
            confidence TEXT,
            status TEXT DEFAULT 'OPEN',
            result TEXT,
            actual_roi REAL,
            resolved_at TEXT
        )
    """)

    conn.commit()
    conn.close()


def save_ml_prediction(row):
    ensure_ml_predictions_table()

    raw_trade_id = row.get("latest_raw_id")

    if not raw_trade_id:
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        INSERT OR REPLACE INTO ml_predictions (
            prediction_date,
            raw_trade_id,
            title,
            outcome,
            market_type,
            price,
            probability_score,
            live_score,
            expected_roi,
            kelly_fraction,
            historical_roi,
            historical_winrate,
            historical_count,
            stat_action,
            ml_action,
            ml_win_probability,
            ml_edge,
            ml_grade,
            final_score,
            validation,
            confidence,
            status
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        raw_trade_id,
        row.get("title"),
        row.get("outcome"),
        row.get("market_type"),
        float(row.get("price") or 0),
        float(row.get("probability_score") or 0),
        float(row.get("live_score") or 0),
        float(row.get("expected_roi") or 0),
        float(row.get("kelly_fraction") or 0),
        float(row.get("historical_roi") or 0),
        float(row.get("historical_winrate") or 0),
        int(row.get("historical_count") or 0),
        row.get("action"),
        row.get("ml_action"),
        float(row.get("ml_win_probability") or 0),
        float(row.get("ml_edge") or 0),
        row.get("ml_grade"),
        float(row.get("final_score") or 0),
        row.get("validation"),
        row.get("confidence"),
        "OPEN"
    ))

    conn.commit()
    conn.close()

def resolve_ml_predictions():
    ensure_ml_predictions_table()

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            p.id,
            p.raw_trade_id,
            r.result,
            r.roi,
            r.resolved_at
        FROM ml_predictions p
        JOIN raw_trades r ON r.id = p.raw_trade_id
        WHERE p.status = 'OPEN'
        AND r.status = 'CLOSED'
        AND r.result IN ('WIN', 'LOSS')
    """)

    rows = cursor.fetchall()

    for prediction_id, raw_trade_id, result, roi, resolved_at in rows:
        cursor.execute("""
            UPDATE ml_predictions
            SET status = 'CLOSED',
                result = ?,
                actual_roi = ?,
                resolved_at = ?
            WHERE id = ?
        """, (
            result,
            float(roi or 0),
            resolved_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            prediction_id
        ))

    conn.commit()
    conn.close()


def weighted_pnl_for_prediction(result, expected_roi, actual_roi, kelly_fraction):
    # Evaluation normalized on 1 unit stake, not actual execution.
    if result == "WIN":
        return float(actual_roi or 0) / 100

    if result == "LOSS":
        return -1

    return 0


def get_ml_prediction_group_stats(group_field):
    ensure_ml_predictions_table()
    resolve_ml_predictions()

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            ml_action,
            stat_action,
            ml_grade,
            validation,
            confidence,
            result,
            actual_roi,
            expected_roi,
            final_score,
            ml_win_probability,
            kelly_fraction
        FROM ml_predictions
        WHERE status = 'CLOSED'
        AND result IN ('WIN', 'LOSS')
    """)

    rows = cursor.fetchall()
    conn.close()

    groups = {}

    for (
        ml_action,
        stat_action,
        ml_grade,
        validation,
        confidence,
        result,
        actual_roi,
        expected_roi,
        final_score,
        ml_win_probability,
        kelly_fraction
    ) in rows:

        if group_field == "ml_action":
            key = ml_action or "Unknown"
        elif group_field == "stat_action":
            key = stat_action or "Unknown"
        elif group_field == "ml_grade":
            key = ml_grade or "Unknown"
        elif group_field == "validation":
            key = validation or "Unknown"
        elif group_field == "confidence":
            key = confidence or "Unknown"
        else:
            key = "All"

        if key not in groups:
            groups[key] = {
                "count": 0,
                "wins": 0,
                "losses": 0,
                "roi_sum": 0,
                "pnl_sum": 0,
                "expected_roi_sum": 0,
                "final_score_sum": 0,
                "ml_probability_sum": 0,
                "kelly_sum": 0
            }

        groups[key]["count"] += 1

        if result == "WIN":
            groups[key]["wins"] += 1
        elif result == "LOSS":
            groups[key]["losses"] += 1

        groups[key]["roi_sum"] += float(actual_roi or 0)
        groups[key]["expected_roi_sum"] += float(expected_roi or 0)
        groups[key]["final_score_sum"] += float(final_score or 0)
        groups[key]["ml_probability_sum"] += float(ml_win_probability or 0)
        groups[key]["kelly_sum"] += float(kelly_fraction or 0)
        groups[key]["pnl_sum"] += weighted_pnl_for_prediction(result, expected_roi, actual_roi, kelly_fraction)

    final = []

    for key, data in groups.items():
        count = data["count"]
        wins = data["wins"]

        final.append({
            "name": key,
            "count": count,
            "wins": wins,
            "losses": data["losses"],
            "winrate": wins / count * 100 if count else 0,
            "avg_roi": data["roi_sum"] / count if count else 0,
            "expected_roi": data["expected_roi_sum"] / count if count else 0,
            "avg_final_score": data["final_score_sum"] / count if count else 0,
            "avg_ml_probability": data["ml_probability_sum"] / count if count else 0,
            "avg_kelly": data["kelly_sum"] / count if count else 0,
            "paper_pnl": data["pnl_sum"]
        })

    return sorted(final, key=lambda x: x["paper_pnl"], reverse=True)


def get_recent_ml_predictions(limit=50):
    ensure_ml_predictions_table()
    resolve_ml_predictions()

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            prediction_date,
            title,
            outcome,
            ml_action,
            stat_action,
            final_score,
            ml_win_probability,
            expected_roi,
            kelly_fraction,
            validation,
            confidence,
            status,
            result,
            actual_roi,
            resolved_at
        FROM ml_predictions
        ORDER BY id DESC
        LIMIT ?
    """, (limit,))

    rows = cursor.fetchall()
    conn.close()

    return rows



def run_xgboost_shadow_model():
    try:
        from xgboost import XGBClassifier
        from sklearn.model_selection import train_test_split
        from sklearn.metrics import accuracy_score, precision_score, recall_score, roc_auc_score
        from sklearn.calibration import CalibratedClassifierCV
    except Exception as e:
        return {
            "available": False,
            "error": str(e),
            "message": "XGBoost ou scikit-learn n'est pas installé. Ajoute xgboost et scikit-learn dans requirements.txt."
        }

    X, y = build_ml_dataset()

    if len(X) < 200:
        return {
            "available": True,
            "enough_data": False,
            "rows": len(X),
            "message": "Pas encore assez de trades fermés pour entraîner proprement XGBoost. Objectif minimum : 200."
        }

    positive = sum(y)
    negative = len(y) - positive

    if positive == 0 or negative == 0:
        return {
            "available": True,
            "enough_data": False,
            "rows": len(X),
            "message": "Le dataset doit contenir des WIN et des LOSS."
        }

    test_size = 0.25

    X_train, X_test, y_train, y_test = train_test_split(
        X,
        y,
        test_size=test_size,
        random_state=42,
        stratify=y
    )

    base_model = XGBClassifier(
        n_estimators=120,
        max_depth=3,
        learning_rate=0.06,
        subsample=0.90,
        colsample_bytree=0.90,
        eval_metric="logloss",
        random_state=42
    )

    model = CalibratedClassifierCV(
        estimator=base_model,
        method="sigmoid",
        cv=3
    )

    model.fit(X_train, y_train)

    preds = model.predict(X_test)
    probs = model.predict_proba(X_test)[:, 1]

    accuracy = accuracy_score(y_test, preds)
    precision = precision_score(y_test, preds, zero_division=0)
    recall = recall_score(y_test, preds, zero_division=0)

    try:
        auc = roc_auc_score(y_test, probs)
    except Exception:
        auc = 0

    feature_names = get_ml_feature_names()

    try:
        importances = model.calibrated_classifiers_[0].estimator.feature_importances_
    except Exception:
        importances = [0 for _ in feature_names]

    top_features = sorted(
        zip(feature_names, importances),
        key=lambda x: x[1],
        reverse=True
    )[:12]

    live_setups = get_live_setup_ranking(50)
    ml_predictions = []

    for setup in live_setups:
        features = features_from_live_setup(setup)
        ml_win_probability = float(model.predict_proba([features])[0][1]) * 100

        historical_winrate = float(setup.get("historical_winrate") or 0)
        historical_roi = float(setup.get("historical_roi") or 0)
        expected_roi = float(setup.get("expected_roi") or 0)

        ml_edge = ml_win_probability - historical_winrate

        roi_component = max(0, min(100, expected_roi * 3))
        historical_roi_component = max(0, min(100, historical_roi * 3))

        final_score = (
            0.35 * float(setup.get("live_score") or 0)
            + 0.35 * ml_win_probability
            + 0.15 * roi_component
            + 0.15 * historical_roi_component
        )

        combined_score = final_score

        if ml_win_probability >= 85:
            ml_grade = "ML A+"
        elif ml_win_probability >= 75:
            ml_grade = "ML A"
        elif ml_win_probability >= 65:
            ml_grade = "ML B"
        elif ml_win_probability >= 55:
            ml_grade = "ML C"
        else:
            ml_grade = "ML D"

        if (
            setup.get("action") == "BUY"
            and ml_win_probability >= 70
            and final_score >= 78
            and expected_roi > 8
        ):
            ml_action = "ML CONFIRMED BUY"
        elif (
            setup.get("action") in ["BUY", "WATCH"]
            and ml_win_probability >= 60
            and final_score >= 68
        ):
            ml_action = "ML WATCH"
        else:
            ml_action = "ML SKIP"

        item = dict(setup)
        item["ml_win_probability"] = ml_win_probability
        item["ml_edge"] = ml_edge
        item["ml_grade"] = ml_grade
        item["combined_score"] = combined_score
        item["final_score"] = final_score
        item["ml_action"] = ml_action

        save_ml_prediction(item)

        ml_predictions.append(item)

    ml_predictions = sorted(
        ml_predictions,
        key=lambda x: (
            x["ml_action"] == "ML CONFIRMED BUY",
            x["final_score"],
            x["expected_roi"]
        ),
        reverse=True
    )

    return {
        "available": True,
        "enough_data": True,
        "rows": len(X),
        "train_rows": len(X_train),
        "test_rows": len(X_test),
        "wins": positive,
        "losses": negative,
        "accuracy": accuracy,
        "precision": precision,
        "recall": recall,
        "auc": auc,
        "top_features": top_features,
        "ml_predictions": ml_predictions[:30]
    }


def ml_action_badge(action):
    if action == "ML CONFIRMED BUY":
        return "🟢 ML CONFIRMED BUY"
    if action == "ML WATCH":
        return "🟡 ML WATCH"
    return "⚪ ML SKIP"


@app.get("/ml", response_class=HTMLResponse)
def ml_dashboard():
    init_db()
    backfill_clean_fields()

    ensure_ml_predictions_table()
    resolve_ml_predictions()

    result = run_xgboost_shadow_model()

    html = html_header("XGBoost ML Shadow Mode")

    html += """
        <h1>🧠 XGBoost ML Shadow Mode</h1>
        <div class="section">
            <h2>Objectif</h2>
            <p>Le modèle ML prédit WIN / LOSS sur les trades ouverts, mais ne décide pas encore seul.</p>
            <p class="small">On compare : Action statistique V4, XGBoost calibré, ML Edge et Final Hybrid Score.</p>
        </div>
    """

    if not result.get("available"):
        html += f"""
        <div class="section">
            <h2>Installation nécessaire</h2>
            <p>{result.get("message")}</p>
            <p>Erreur : {result.get("error")}</p>
            <p>Ajoute dans requirements.txt :</p>
            <pre>xgboost
scikit-learn</pre>
        </div>
        """
        html += html_footer()
        return html

    if not result.get("enough_data"):
        html += f"""
        <div class="section">
            <h2>Pas encore assez de données</h2>
            <p>{result.get("message")}</p>
            <p>Trades fermés disponibles : {result.get("rows")}</p>
        </div>
        """
        html += html_footer()
        return html

    html += f"""
        <div class="grid">
            {render_kpi("Trades utilisés", result["rows"])}
            {render_kpi("Accuracy", f"{result["accuracy"] * 100:.2f}", "%")}
            {render_kpi("Precision WIN", f"{result["precision"] * 100:.2f}", "%")}
            {render_kpi("Recall WIN", f"{result["recall"] * 100:.2f}", "%")}
            {render_kpi("AUC", f"{result["auc"]:.3f}")}
            {render_kpi("Calibration", "Sigmoid")}
        </div>
    """

    html += """
        <div class="section">
            <h2>Top ML Features</h2>
            <table>
                <tr>
                    <th>Feature</th>
                    <th>Importance</th>
                </tr>
    """

    for name, importance in result["top_features"]:
        html += f"""
                <tr>
                    <td>{name}</td>
                    <td>{importance:.4f}</td>
                </tr>
        """

    html += """
            </table>
        </div>
    """

    html += """
        <div class="section">
            <h2>🔥 Live Setups — ML Confirmation</h2>
            <table>
                <tr>
                    <th>Rank</th>
                    <th>ML Action</th>
                    <th>Stat Action</th>
                    <th>Final Score</th>
                    <th>ML Win % calibré</th>
                    <th>ML Edge</th>
                    <th>ML Grade</th>
                    <th>Expected ROI</th>
                    <th>Kelly %</th>
                    <th>Market</th>
                    <th>Outcome</th>
                    <th>Prix</th>
                    <th>Validation</th>
                    <th>Confidence</th>
                    <th>Hist ROI</th>
                    <th>Hist Trades</th>
                </tr>
    """

    if not result["ml_predictions"]:
        html += """
                <tr>
                    <td colspan="16">Aucun trade ouvert actuellement.</td>
                </tr>
        """

    for idx, row in enumerate(result["ml_predictions"], start=1):
        price_display = f"{row['price']:.3f}"

        if row["min_price"] != row["max_price"]:
            price_display = f"{row['min_price']:.3f} → {row['max_price']:.3f}"

        html += f"""
                <tr>
                    <td>{idx}</td>
                    <td><b>{ml_action_badge(row["ml_action"])}</b></td>
                    <td>{action_badge(row["action"])}</td>
                    <td><b>{row["final_score"]:.1f}</b></td>
                    <td><b>{row["ml_win_probability"]:.2f}%</b></td>
                    <td class="{roi_class(row["ml_edge"])}">{row["ml_edge"]:.2f}%</td>
                    <td>{row["ml_grade"]}</td>
                    <td class="{roi_class(row["expected_roi"])}">{row["expected_roi"]:.2f}%</td>
                    <td>{row["kelly_fraction"]:.2f}%</td>
                    <td>{row["title"]}<br><span class="small">Signals visibles : {row["signals_visible"]}</span></td>
                    <td>{row["outcome"]}</td>
                    <td>{price_display}</td>
                    <td>{validation_badge(row["validation"])}</td>
                    <td>{row["confidence"]}</td>
                    <td class="{roi_class(row["historical_roi"])}">{row["historical_roi"]:.2f}%</td>
                    <td>{row["historical_count"]}</td>
                </tr>
        """

    html += """
            </table>
            <p class="small">
                Final Score = 35% Live Score V4 + 35% ML Win calibré + 15% Expected ROI + 15% Historical ROI.
                ML Edge = ML Win % - Winrate historique du setup.
            </p>
        </div>
    """

    html += html_footer()
    return html



def render_ml_prediction_stats_table(title, rows):
    html = f"""
    <div class="section">
        <h2>{title}</h2>
        <table>
            <tr>
                <th>Catégorie</th>
                <th>Trades fermés</th>
                <th>Wins</th>
                <th>Losses</th>
                <th>Winrate</th>
                <th>Avg Actual ROI</th>
                <th>Avg Expected ROI</th>
                <th>Avg ML %</th>
                <th>Avg Final Score</th>
                <th>Avg Kelly</th>
                <th>Paper PnL 1u</th>
            </tr>
    """

    if not rows:
        html += """
            <tr>
                <td colspan="11">Aucune prédiction ML fermée pour le moment.</td>
            </tr>
        """

    for row in rows:
        html += f"""
            <tr>
                <td>{row['name']}</td>
                <td>{row['count']}</td>
                <td>{row['wins']}</td>
                <td>{row['losses']}</td>
                <td>{row['winrate']:.2f}%</td>
                <td class="{roi_class(row['avg_roi'])}">{row['avg_roi']:.2f}%</td>
                <td class="{roi_class(row['expected_roi'])}">{row['expected_roi']:.2f}%</td>
                <td>{row['avg_ml_probability']:.2f}%</td>
                <td>{row['avg_final_score']:.2f}</td>
                <td>{row['avg_kelly']:.2f}%</td>
                <td class="{roi_class(row['paper_pnl'])}">{row['paper_pnl']:.2f}</td>
            </tr>
        """

    html += """
        </table>
    </div>
    """

    return html


@app.get("/ml-performance", response_class=HTMLResponse)
def ml_performance_dashboard():
    init_db()
    backfill_clean_fields()
    ensure_ml_predictions_table()
    resolve_ml_predictions()

    by_ml_action = get_ml_prediction_group_stats("ml_action")
    by_stat_action = get_ml_prediction_group_stats("stat_action")
    by_ml_grade = get_ml_prediction_group_stats("ml_grade")
    by_validation = get_ml_prediction_group_stats("validation")
    recent = get_recent_ml_predictions(50)

    html = html_header("ML Performance")

    html += """
        <h1>📈 ML Performance Tracking</h1>
        <div class="section">
            <h2>Objectif</h2>
            <p>Cette page mesure les prédictions ML en conditions réelles une fois les trades fermés.</p>
            <p class="small">C'est le test décisif : ML CONFIRMED BUY doit battre ML WATCH et ML SKIP.</p>
        </div>
    """

    html += render_ml_prediction_stats_table("🧠 Performance par ML Action", by_ml_action)
    html += render_ml_prediction_stats_table("📊 Performance par Stat Action", by_stat_action)
    html += render_ml_prediction_stats_table("🏷️ Performance par ML Grade", by_ml_grade)
    html += render_ml_prediction_stats_table("✅ Performance par Validation", by_validation)

    html += """
        <div class="section">
            <h2>🕒 Dernières prédictions ML</h2>
            <table>
                <tr>
                    <th>Date prédiction</th>
                    <th>Market</th>
                    <th>Outcome</th>
                    <th>ML Action</th>
                    <th>Stat Action</th>
                    <th>Final Score</th>
                    <th>ML Win %</th>
                    <th>Expected ROI</th>
                    <th>Kelly</th>
                    <th>Validation</th>
                    <th>Confidence</th>
                    <th>Status</th>
                    <th>Result</th>
                    <th>Actual ROI</th>
                    <th>Resolved At</th>
                </tr>
    """

    if not recent:
        html += """
                <tr>
                    <td colspan="15">Aucune prédiction enregistrée pour le moment. Ouvre /ml pour générer les premières prédictions.</td>
                </tr>
        """

    for row in recent:
        (
            prediction_date,
            title,
            outcome,
            ml_action,
            stat_action,
            final_score,
            ml_win_probability,
            expected_roi,
            kelly_fraction,
            validation,
            confidence,
            status,
            result,
            actual_roi,
            resolved_at
        ) = row

        html += f"""
                <tr>
                    <td>{prediction_date}</td>
                    <td>{title}</td>
                    <td>{outcome}</td>
                    <td>{ml_action}</td>
                    <td>{stat_action}</td>
                    <td>{float(final_score or 0):.2f}</td>
                    <td>{float(ml_win_probability or 0):.2f}%</td>
                    <td class="{roi_class(expected_roi)}">{float(expected_roi or 0):.2f}%</td>
                    <td>{float(kelly_fraction or 0):.2f}%</td>
                    <td>{validation}</td>
                    <td>{confidence}</td>
                    <td>{status}</td>
                    <td>{result or ''}</td>
                    <td class="{roi_class(actual_roi)}">{'' if actual_roi is None else f'{float(actual_roi):.2f}%'}</td>
                    <td>{resolved_at or ''}</td>
                </tr>
        """

    html += """
            </table>
        </div>
    """

    html += html_footer()
    return html



@app.get("/paper", response_class=HTMLResponse)
def paper_trades_dashboard():
    init_db()
    backfill_clean_fields()
    resolve_paper_trades()

    open_rows = get_open_paper_trades(200)
    closed_rows = get_recent_closed_paper_trades(75)

    html = html_header("Paper Trades")

    html += """
        <h1>📄 Paper Trades</h1>
        <div class="section">
            <h2>Objectif</h2>
            <p>Voir exactement quels trades le bot a décidé d'ouvrir en paper trading.</p>
            <p class="small">Tu peux suivre manuellement les trades ouverts avec 1$ si tu veux tester le système en réel.</p>
        </div>
    """

    html += render_paper_engine_card()
    html += render_open_paper_trades_table(open_rows)
    html += render_closed_paper_trades_table(closed_rows)

    html += html_footer()
    return html



init_db()
backfill_clean_fields()

tracker_thread = threading.Thread(
    target=whale_tracker_loop,
    daemon=True
)

tracker_thread.start()
