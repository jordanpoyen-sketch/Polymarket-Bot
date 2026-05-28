from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import requests
import threading
import time
import os
import sqlite3
import json
from datetime import datetime, timezone
from math import isnan


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


def render_category_table(title, rows):
    html = f"""
    <div class="card">
        <h2>{title}</h2>
        <table border="1" cellpadding="6" cellspacing="0" style="width:100%; color:white; border-collapse:collapse;">
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

    for row in rows:
        html += f"""
            <tr>
                <td>{row['name']}</td>
                <td>{row['count']}</td>
                <td>{row['wins']}</td>
                <td>{row['losses']}</td>
                <td>{row['winrate']:.2f}%</td>
                <td>{row['avg_roi']:.2f}%</td>
                <td>{row['weighted_roi']:.2f}%</td>
                <td>{row['weighted_pnl']:.2f}</td>
            </tr>
        """

    html += """
        </table>
    </div>
    """

    return html



def render_validated_grades_table(rows):
    html = """
    <div class="card">
        <h2>✅ Validation A+ / A / B</h2>
        <table border="1" cellpadding="6" cellspacing="0" style="width:100%; color:white; border-collapse:collapse;">
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
                <td>{row['weighted_roi']:.2f}%</td>
                <td>{row['weighted_pnl']:.2f}</td>
                <td>{validation}</td>
            </tr>
        """

    html += """
        </table>
        <p>
            Critère validation : minimum 10 trades fermés, Weighted ROI positif, Winrate ≥ 60%.
        </p>
    </div>
    """

    return html



def render_curve(title, curve):
    html = f"""
    <div class="card">
        <h2>{title}</h2>
    """

    if not curve:
        html += "<p>Aucune donnée</p>"
    else:
        for point, pnl in curve:
            html += f"<p>Trade {point} : {pnl}</p>"

    html += """
    </div>
    """

    return html


@app.get("/", response_class=HTMLResponse)
def dashboard():
    init_db()
    backfill_clean_fields()

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
            table {{
                font-size: 14px;
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
    """

    html += render_category_table("✅ Analyse Quality Signals", stats["by_quality"])
    html += render_category_table("📊 Analyse YES vs NO", stats["by_outcome"])
    html += render_category_table("📊 Analyse par prix", stats["by_price"])
    html += render_category_table("📊 Analyse par type de marché", stats["by_market"])
    html += render_category_table("🕒 Analyse Entry Timing", stats["by_timing"])
    html += render_category_table("🔥 Analyse Aggressiveness", stats["by_aggressiveness"])
    html += render_category_table("🔁 Analyse Reinforcement", stats["by_reinforcement"])
    html += render_category_table("💰 Cumulative Size Analytics", stats["by_cumulative_size"])
    html += render_category_table("🧠 Probability Grade Analytics", stats["by_probability_grade"])
    html += render_validated_grades_table(stats["validated_grades"])

    html += """
    </body>
    </html>
    """

    return html


@app.get("/analytics", response_class=HTMLResponse)
def analytics():
    init_db()
    backfill_clean_fields()

    data = get_advanced_analytics()

    html = """
    <html>
    <head>
        <title>Whale Analytics</title>
        <meta http-equiv="refresh" content="60">
        <style>
            body {
                background-color: #111;
                color: white;
                font-family: Arial;
                padding: 20px;
            }
            .card {
                background-color: #1c1c1c;
                padding: 15px;
                margin-bottom: 15px;
                border-radius: 10px;
            }
            h1 {
                color: orange;
            }
            table {
                width: 100%;
                color: white;
                border-collapse: collapse;
            }
            th, td {
                border: 1px solid #555;
                padding: 6px;
            }
        </style>
    </head>
    <body>
        <h1>📊 Advanced Whale Analytics</h1>
    """

    html += f"""
        <div class="card">
            <h2>🧠 Confidence Score</h2>
            <h1>{data["confidence_score"]:.1f}/100</h1>
        </div>

        <div class="card">
            <h2>📈 Rolling Winrate</h2>
            <p>Last 20 trades : {data["rolling_20"]:.2f}%</p>
            <p>Last 50 trades : {data["rolling_50"]:.2f}%</p>
            <p>Last 100 trades : {data["rolling_100"]:.2f}%</p>
        </div>
    """

    html += render_curve("📉 Total Cumulative PnL — last 50", data["total_curve"])
    html += render_curve("✅ Quality Cumulative PnL — last 50", data["quality_curve"])
    html += render_curve("❌ Excluded Cumulative PnL — last 50", data["excluded_curve"])

    html += render_category_table("🔁 Reinforcement Analytics", get_category_stats("reinforcement"))
    html += render_category_table("💰 Cumulative Size Analytics", get_category_stats("cumulative_size"))
    html += render_category_table("🧠 Probability Grade Analytics", get_probability_grade_stats())
    html += render_validated_grades_table(get_validated_grade_stats())

    html += """
        <div class="card">
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
                    <td>{s["weighted_roi"]:.2f}%</td>
                    <td>{s["weighted_pnl"]:.2f}</td>
                </tr>
        """

    html += """
            </table>
        </div>
    """

    html += """
        <div class="card">
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
                    <td>{s["weighted_roi"]:.2f}%</td>
                    <td>{s["weighted_pnl"]:.2f}</td>
                </tr>
        """

    html += """
            </table>
        </div>
    </body>
    </html>
    """

    return html


@app.get("/ml", response_class=HTMLResponse)
def ml_dashboard():
    init_db()
    backfill_clean_fields()

    result = run_xgboost_shadow_model()

    html = """
    <html>
    <head>
        <title>Whale ML</title>
        <meta http-equiv="refresh" content="60">
        <style>
            body {
                background-color: #111;
                color: white;
                font-family: Arial;
                padding: 20px;
            }
            .card {
                background-color: #1c1c1c;
                padding: 15px;
                margin-bottom: 15px;
                border-radius: 10px;
            }
            h1 {
                color: orange;
            }
            table {
                width: 100%;
                color: white;
                border-collapse: collapse;
            }
            th, td {
                border: 1px solid #555;
                padding: 6px;
            }
        </style>
    </head>
    <body>
        <h1>🧠 XGBoost ML Shadow Mode</h1>
    """

    if not result.get("available"):
        html += f"""
        <div class="card">
            <h2>Installation nécessaire</h2>
            <p>{result.get("message")}</p>
            <p>Erreur : {result.get("error")}</p>
            <p>Ajoute dans requirements.txt :</p>
            <pre>xgboost
scikit-learn</pre>
        </div>
        </body></html>
        """
        return html

    if not result.get("enough_data"):
        html += f"""
        <div class="card">
            <h2>Pas encore assez de données</h2>
            <p>{result.get("message")}</p>
            <p>Trades fermés disponibles : {result.get("rows")}</p>
        </div>
        </body></html>
        """
        return html

    html += f"""
        <div class="card">
            <h2>Dataset</h2>
            <p>Trades utilisés : {result["rows"]}</p>
            <p>Train : {result["train_rows"]}</p>
            <p>Test : {result["test_rows"]}</p>
            <p>Wins : {result["wins"]}</p>
            <p>Losses : {result["losses"]}</p>
        </div>

        <div class="card">
            <h2>Performance ML</h2>
            <p>Accuracy : {result["accuracy"] * 100:.2f}%</p>
            <p>Precision WIN : {result["precision"] * 100:.2f}%</p>
            <p>Recall WIN : {result["recall"] * 100:.2f}%</p>
        </div>

        <div class="card">
            <h2>Top Features</h2>
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

        <div class="card">
            <h2>Open Trades — ML Predictions</h2>
            <table>
                <tr>
                    <th>Market</th>
                    <th>Outcome</th>
                    <th>Price</th>
                    <th>Size</th>
                    <th>Type</th>
                    <th>Quality</th>
                    <th>Reinf.</th>
                    <th>Cum Size</th>
                    <th>Rule Score</th>
                    <th>Rule Grade</th>
                    <th>ML Win %</th>
                    <th>ML Grade</th>
                </tr>
    """

    if not result["open_predictions"]:
        html += """
                <tr>
                    <td colspan="12">Aucun trade ouvert actuellement.</td>
                </tr>
        """

    for pred in result["open_predictions"]:
        html += f"""
                <tr>
                    <td>{pred["title"]}</td>
                    <td>{pred["outcome"]}</td>
                    <td>{float(pred["price"] or 0):.3f}</td>
                    <td>{float(pred["usdc_size"] or 0):.2f}</td>
                    <td>{pred["market_type"]}</td>
                    <td>{bool(pred["quality_signal"])}</td>
                    <td>{pred["reinforcement_count"]}</td>
                    <td>{float(pred["cumulative_size"] or 0):.2f}</td>
                    <td>{float(pred["probability_score"] or 0):.1f}</td>
                    <td>{pred["trade_grade"]}</td>
                    <td>{pred["ml_win_probability"]:.2f}%</td>
                    <td>{pred["ml_grade"]}</td>
                </tr>
        """

    html += """
            </table>
        </div>

        <div class="card">
            <h2>Mode</h2>
            <p>Le ML est en shadow mode : il prédit, mais ne décide pas encore.</p>
            <p>On compare ses prédictions aux grades A+ / A / B du probability model.</p>
        </div>
    </body>
    </html>
    """

    return html


init_db()
backfill_clean_fields()

tracker_thread = threading.Thread(
    target=whale_tracker_loop,
    daemon=True
)

tracker_thread.start()
