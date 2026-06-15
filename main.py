import json
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

APP_NAME = "BTC AI Trader V2 - Autonomous"
DB_PATH = os.getenv("DB_PATH", "btc_ai_trader_v2.db")
PAPER_TRADE_SIZE = float(os.getenv("PAPER_TRADE_SIZE", "1.0"))
SCAN_INTERVAL_SECONDS = int(os.getenv("SCAN_INTERVAL_SECONDS", "60"))
MIN_OPEN_SCORE = float(os.getenv("MIN_OPEN_SCORE", "82"))
MAX_EXPIRY_MINUTES = float(os.getenv("MAX_EXPIRY_MINUTES", "2880"))
MIN_PRICE = float(os.getenv("MIN_PRICE", "0.04"))
MAX_PRICE = float(os.getenv("MAX_PRICE", "0.96"))

app = FastAPI(title=APP_NAME)
STATE = {"last_scan": "-", "last_error": "-", "last_result": None}


def db_connect():
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    c = conn.cursor()
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    c.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db():
    conn = db_connect()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date_opened TEXT,
            date_closed TEXT,
            slug TEXT,
            title TEXT,
            market_type TEXT,
            outcome TEXT,
            entry_price REAL,
            btc_open REAL,
            btc_close REAL,
            trend_15m TEXT,
            trend_1h TEXT,
            rsi_15m REAL,
            rsi_1h REAL,
            volume_signal TEXT,
            fear_greed INTEGER,
            threshold REAL,
            distance_pct REAL,
            minutes_left REAL,
            score REAL,
            reasons TEXT,
            trade_size REAL,
            shares REAL,
            status TEXT,
            result TEXT,
            pnl REAL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT,
            btc_live REAL,
            trend_15m TEXT,
            trend_1h TEXT,
            rsi_15m REAL,
            rsi_1h REAL,
            volume_signal TEXT,
            fear_greed INTEGER,
            markets_found INTEGER,
            candidates_count INTEGER,
            opened_count INTEGER,
            rejected_json TEXT
        )
    """)
    conn.commit()
    conn.close()


def safe_float(v, default=0.0):
    try:
        if v is None:
            return default
        return float(v)
    except Exception:
        return default


def get_json(url, params=None, timeout=20):
    try:
        r = requests.get(url, params=params or {}, timeout=timeout)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def get_btc_price():
    data = get_json("https://api.coinbase.com/v2/prices/BTC-USD/spot", timeout=10)
    try:
        return float(data["data"]["amount"])
    except Exception:
        pass
    data = get_json("https://api.binance.com/api/v3/ticker/price", {"symbol": "BTCUSDT"}, timeout=10)
    try:
        return float(data["price"])
    except Exception:
        return 0.0


def get_candles(granularity=900, limit=100):
    data = get_json("https://api.exchange.coinbase.com/products/BTC-USD/candles", {"granularity": granularity}, timeout=15)
    if not isinstance(data, list):
        return []
    return sorted(data[:limit], key=lambda x: x[0])


def rsi(closes, period=14):
    if len(closes) <= period:
        return 50.0
    gains, losses = [], []
    for i in range(1, period + 1):
        d = closes[-i] - closes[-i - 1]
        gains.append(max(d, 0))
        losses.append(abs(min(d, 0)))
    ag = sum(gains) / period
    al = sum(losses) / period
    if al == 0:
        return 100.0
    rs = ag / al
    return round(100 - 100 / (1 + rs), 2)


def ema(values, period):
    if not values:
        return 0.0
    k = 2 / (period + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


def trend(closes):
    if len(closes) < 50:
        return "NEUTRAL"
    e9 = ema(closes[-50:], 9)
    e20 = ema(closes[-50:], 20)
    e50 = ema(closes[-80:] if len(closes) >= 80 else closes, 50)
    last = closes[-1]
    if last > e9 > e20 and e20 >= e50 * 0.998:
        return "BULLISH"
    if last < e9 < e20 and e20 <= e50 * 1.002:
        return "BEARISH"
    if e9 > e20 and last > e20:
        return "BULLISH"
    if e9 < e20 and last < e20:
        return "BEARISH"
    return "NEUTRAL"


def volume_signal(candles):
    vols = [safe_float(c[5]) for c in candles]
    if len(vols) < 20:
        return "UNKNOWN"
    cur = vols[-1]
    avg = sum(vols[-20:]) / 20
    if avg <= 0:
        return "UNKNOWN"
    if cur > avg * 1.7:
        return "HIGH"
    if cur < avg * 0.6:
        return "LOW"
    return "NORMAL"


def fear_greed():
    data = get_json("https://api.alternative.me/fng/", {"limit": 1, "format": "json"}, timeout=15)
    try:
        return int(data["data"][0]["value"])
    except Exception:
        return None


def btc_context():
    c15 = get_candles(900, 100)
    c1h = get_candles(3600, 100)
    closes15 = [safe_float(c[4]) for c in c15]
    closes1h = [safe_float(c[4]) for c in c1h]
    return {
        "btc_live": get_btc_price(),
        "trend_15m": trend(closes15),
        "trend_1h": trend(closes1h),
        "rsi_15m": rsi(closes15),
        "rsi_1h": rsi(closes1h),
        "volume_signal": volume_signal(c15),
        "fear_greed": fear_greed(),
    }


def parse_outcomes_prices(market):
    try:
        outcomes = market.get("outcomes")
        prices = market.get("outcomePrices")
        if isinstance(outcomes, str):
            outcomes = json.loads(outcomes)
        if isinstance(prices, str):
            prices = json.loads(prices)
        if not outcomes or not prices:
            return []
        out = []
        for i, o in enumerate(outcomes):
            try:
                out.append((str(o), float(prices[i])))
            except Exception:
                continue
        return out
    except Exception:
        return []


def is_btc_market(title, slug):
    text = f"{title} {slug}".lower()
    if "bitcoin" not in text and "btc" not in text:
        return False
    blocked = ["before gta", "reserve", "unban", "$1m", "$1 m", "million", "150k", "2027", "2028"]
    if any(b in text for b in blocked):
        return False
    allowed = ["price of bitcoin", "bitcoin be above", "bitcoin be below", "bitcoin be less", "bitcoin be between", "bitcoin dip", "bitcoin reach", "btc above", "btc below", "btc between"]
    return any(a in text for a in allowed)


def classify_market(title):
    t = (title or "").lower()
    if "between" in t:
        return "Range"
    if "above" in t:
        return "Above"
    if "less than" in t or "below" in t:
        return "Below"
    if "dip" in t:
        return "Dip"
    if "reach" in t or "hit" in t:
        return "Reach"
    return "Other"


def extract_threshold(title):
    m = re.search(r"\$([0-9]+(?:\.[0-9]+)?)(?:,([0-9]{3}))?\s*([km])?", (title or "").lower())
    if not m:
        return None
    try:
        first, comma, suffix = m.group(1), m.group(2), m.group(3)
        val = float(first + comma) if comma else float(first)
        if suffix == "k":
            val *= 1000
        elif suffix == "m":
            val *= 1000000
        return float(val)
    except Exception:
        return None


def expiry_from_title(title):
    m = re.search(r"\bon\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2})\b", title or "", re.I)
    if not m:
        return None
    months = {"january":1,"february":2,"march":3,"april":4,"may":5,"june":6,"july":7,"august":8,"september":9,"october":10,"november":11,"december":12}
    month = months.get(m.group(1).lower())
    day = int(m.group(2))
    year = datetime.now(timezone.utc).year
    paris = ZoneInfo("Europe/Paris")
    dt = datetime(year, month, day, 18, 0, 0, tzinfo=paris).astimezone(timezone.utc)
    if (dt - datetime.now(timezone.utc)).days < -300:
        dt = datetime(year + 1, month, day, 18, 0, 0, tzinfo=paris).astimezone(timezone.utc)
    return dt


def minutes_left(title):
    dt = expiry_from_title(title)
    if not dt:
        return None
    return round((dt - datetime.now(timezone.utc)).total_seconds() / 60, 2)


def normalize_gamma_response(data):
    if not data:
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("markets") or data.get("data") or data.get("results") or data.get("items") or []
    return []


def fetch_raw_markets(limit=300):
    raw = []
    seen = set()

    queries = [
        "",
        "Bitcoin",
        "BTC",
        "price of Bitcoin",
        "Will the price of Bitcoin",
        "Bitcoin above",
        "Bitcoin below",
        "Bitcoin less",
        "Bitcoin between",
        "Bitcoin dip",
        "Bitcoin reach",
    ]

    for q in queries:
        for offset in range(0, 1500, 100):
            variants = [
                {"closed": "false", "active": "true", "limit": 100, "offset": offset},
                {"closed": "false", "active": "true", "limit": 100, "offset": offset, "q": q},
                {"closed": "false", "active": "true", "limit": 100, "offset": offset, "search": q},
                {"closed": "false", "active": "true", "limit": 100, "offset": offset, "query": q},
                {"limit": 100, "offset": offset, "q": q},
                {"limit": 100, "offset": offset, "search": q},
            ]

            for params in variants:
                if not q:
                    params = {k: v for k, v in params.items() if k not in ["q", "search", "query"]}

                data = get_json("https://gamma-api.polymarket.com/markets", params=params, timeout=20)
                markets = normalize_gamma_response(data)

                if not markets:
                    continue

                for m in markets:
                    title = m.get("question") or m.get("title") or m.get("name") or ""
                    slug = m.get("slug") or ""
                    key = slug or title

                    if not key or key in seen:
                        continue

                    seen.add(key)
                    raw.append(m)

                    if len(raw) >= limit:
                        return raw

    return raw


def is_btc_market(title, slug):
    text = f"{title} {slug}".lower()
    if "bitcoin" not in text and "btc" not in text:
        return False

    blocked = ["before gta", "reserve", "unban", "$1m", "$1 m", "million", "150k", "2027", "2028"]
    if any(b in text for b in blocked):
        return False

    allowed = [
        "price of bitcoin",
        "bitcoin be above",
        "bitcoin be below",
        "bitcoin be less",
        "bitcoin be between",
        "bitcoin above",
        "bitcoin below",
        "bitcoin less",
        "bitcoin between",
        "bitcoin dip",
        "bitcoin reach",
        "btc above",
        "btc below",
        "btc between",
        "above-",
        "below-",
        "between-",
        "less-than",
    ]

    return any(a in text for a in allowed)


def fetch_markets():
    rows, seen = [], set()
    rejection = {
        "raw_seen": 0,
        "not_btc": 0,
        "bad_threshold": 0,
        "bad_expiry": 0,
        "bad_outcomes": 0,
        "accepted": 0,
    }

    raw_markets = fetch_raw_markets(limit=1200)

    for m in raw_markets:
        rejection["raw_seen"] += 1

        title = m.get("question") or m.get("title") or m.get("name") or ""
        slug = m.get("slug") or ""

        if not is_btc_market(title, slug):
            rejection["not_btc"] += 1
            continue

        th = extract_threshold(title + " " + slug)

        if not th or th < 40000 or th > 200000:
            rejection["bad_threshold"] += 1
            continue

        mins = minutes_left(title)

        if mins is None:
            mins = minutes_left(slug.replace("-", " "))

        if mins is None or mins <= 0 or mins > MAX_EXPIRY_MINUTES:
            rejection["bad_expiry"] += 1
            continue

        outcomes_prices = parse_outcomes_prices(m)

        if not outcomes_prices:
            rejection["bad_outcomes"] += 1
            continue

        for outcome, price in outcomes_prices:
            key = (slug, outcome)
            if key in seen:
                continue

            seen.add(key)

            rows.append({
                "title": title,
                "slug": slug,
                "outcome": outcome,
                "price": price,
                "market_type": classify_market(title + " " + slug),
                "threshold": th,
                "minutes_left": mins
            })

    rejection["accepted"] = len(rows)
    STATE["source_rejection"] = rejection
    STATE["raw_market_sample"] = raw_markets[:200]
    return rows



def distance_and_favorable(market_type, outcome, btc, threshold):
    if not btc or not threshold:
        return None, False
    dist = abs(btc - threshold) / threshold * 100
    fav = False
    if market_type == "Above":
        fav = (outcome == "Yes" and btc > threshold) or (outcome == "No" and btc < threshold)
    elif market_type == "Below":
        fav = (outcome == "Yes" and btc < threshold) or (outcome == "No" and btc > threshold)
    elif market_type == "Dip":
        fav = (outcome == "Yes" and btc <= threshold) or (outcome == "No" and btc > threshold)
    elif market_type == "Reach":
        fav = (outcome == "Yes" and btc >= threshold) or (outcome == "No" and btc < threshold)
    return round(dist, 2), fav


def wants_up(market_type, outcome):
    return (market_type == "Above" and outcome == "Yes") or (market_type == "Below" and outcome == "No") or (market_type == "Dip" and outcome == "No") or (market_type == "Reach" and outcome == "Yes")


def wants_down(market_type, outcome):
    return (market_type == "Above" and outcome == "No") or (market_type == "Below" and outcome == "Yes") or (market_type == "Dip" and outcome == "Yes") or (market_type == "Reach" and outcome == "No")


def score_candidate(row, ctx):
    btc = ctx["btc_live"]
    price = safe_float(row["price"])
    dist, fav = distance_and_favorable(row["market_type"], row["outcome"], btc, row["threshold"])
    if dist is None or price <= 0 or price >= 1:
        return 0, dist, "invalid"
    payout = ((1 - price) / price) * 100
    score, reasons = 50, []
    if price > 0.95:
        score -= 25; reasons.append("prix trop haut")
    elif price < 0.05:
        score -= 8; reasons.append("très spéculatif")
    if payout >= 100:
        score += 12; reasons.append("très gros payout")
    elif payout >= 30:
        score += 8; reasons.append("payout intéressant")
    elif payout < 5:
        score -= 18; reasons.append("faible upside")
    mins = row["minutes_left"]
    if mins <= 60:
        score += 14; reasons.append("expiration <1h")
    elif mins <= 360:
        score += 10; reasons.append("expiration <6h")
    elif mins <= 1440:
        score += 4; reasons.append("expiration <24h")
    else:
        score -= 5; reasons.append("expiration >24h")
    if fav:
        score += 18 if dist >= 3 else 10 if dist >= 1 else 3
        reasons.append("résultat favorable")
    else:
        if dist >= 5 and mins < 720:
            score -= 25; reasons.append("objectif trop éloigné")
        elif dist >= 3 and mins < 360:
            score -= 20; reasons.append("objectif loin à court terme")
        elif dist >= 3:
            score -= 8; reasons.append("objectif éloigné")
        else:
            score += 3; reasons.append("seuil proche")
    up, down = wants_up(row["market_type"], row["outcome"]), wants_down(row["market_type"], row["outcome"])
    t15, t1h = ctx["trend_15m"], ctx["trend_1h"]
    rsi15, vol, fg = ctx["rsi_15m"], ctx["volume_signal"], ctx["fear_greed"]
    if up:
        if t15 == "BULLISH" and t1h in ["BULLISH", "NEUTRAL"]:
            score += 14; reasons.append("tendance haussière")
        elif t15 == "BEARISH" and t1h == "BEARISH":
            score -= 24; reasons.append("contre tendance baissière")
        if rsi15 >= 75:
            score -= 10; reasons.append("RSI suracheté")
        elif rsi15 <= 35:
            score += 5; reasons.append("RSI bas rebond")
    if down:
        if t15 == "BEARISH" and t1h in ["BEARISH", "NEUTRAL"]:
            score += 14; reasons.append("tendance baissière")
        elif t15 == "BULLISH" and t1h == "BULLISH":
            score -= 24; reasons.append("contre pump")
        if rsi15 <= 25:
            score -= 10; reasons.append("RSI survendu")
        elif rsi15 >= 65:
            score += 5; reasons.append("RSI haut rejet")
    if vol == "HIGH":
        if (up and t15 == "BULLISH") or (down and t15 == "BEARISH"):
            score += 8; reasons.append("volume confirme")
        else:
            score -= 5; reasons.append("volume contre signal")
    if fg is not None:
        if fg <= 15 and up:
            score += 4; reasons.append("fear extrême rebond")
        elif fg <= 15 and down:
            score -= 4; reasons.append("fear extrême prudence short")
        elif fg >= 75 and up:
            score -= 4; reasons.append("greed prudence long")
        elif fg >= 75 and down:
            score += 4; reasons.append("greed contrarian")
    if row["market_type"] == "Dip" and row["outcome"] == "Yes" and t15 == "BULLISH" and t1h == "BULLISH":
        score -= 35; reasons.append("bloqué dip yes pendant pump")
    return max(0, min(100, round(score, 1))), dist, " | ".join(reasons)


def action(score):
    return "BUY" if score >= MIN_OPEN_SCORE else "WATCH" if score >= 75 else "WEAK" if score >= 60 else "SKIP"


def trade_open(slug, outcome):
    conn = db_connect(); c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM trades WHERE slug=? AND outcome=? AND status='OPEN'", (slug, outcome))
    ok = c.fetchone()[0] > 0
    conn.close(); return ok


def open_trade(row, ctx, score, dist, reasons):
    if trade_open(row["slug"], row["outcome"]):
        return False
    price = safe_float(row["price"])
    if price <= 0 or price >= 1:
        return False
    shares = PAPER_TRADE_SIZE / price
    conn = db_connect(); c = conn.cursor()
    c.execute("""
        INSERT INTO trades (date_opened,date_closed,slug,title,market_type,outcome,entry_price,btc_open,btc_close,trend_15m,trend_1h,rsi_15m,rsi_1h,volume_signal,fear_greed,threshold,distance_pct,minutes_left,score,reasons,trade_size,shares,status,result,pnl)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), None, row["slug"], row["title"], row["market_type"], row["outcome"], price, ctx["btc_live"], None, ctx["trend_15m"], ctx["trend_1h"], ctx["rsi_15m"], ctx["rsi_1h"], ctx["volume_signal"], ctx["fear_greed"], row["threshold"], dist, row["minutes_left"], score, reasons, PAPER_TRADE_SIZE, shares, "OPEN", "", None))
    conn.commit(); conn.close(); return True


def infer_result(title, market_type, outcome, btc_close, threshold):
    yes_wins = None
    if market_type == "Above": yes_wins = btc_close > threshold
    elif market_type == "Below": yes_wins = btc_close < threshold
    elif market_type == "Dip": yes_wins = btc_close <= threshold
    elif market_type == "Reach": yes_wins = btc_close >= threshold
    elif market_type == "Range":
        vals = re.findall(r"\$([0-9,]+)", title)
        if len(vals) >= 2:
            low, high = float(vals[0].replace(',', '')), float(vals[1].replace(',', ''))
            yes_wins = low <= btc_close <= high
    if yes_wins is None:
        return None
    return "WIN" if (outcome == "Yes" and yes_wins) or (outcome == "No" and not yes_wins) else "LOSS"


def resolve_trades():
    conn = db_connect(); c = conn.cursor()
    c.execute("SELECT id,title,market_type,outcome,entry_price,shares,trade_size,threshold FROM trades WHERE status='OPEN'")
    rows = c.fetchall(); btc = get_btc_price(); closed = 0
    for tid, title, mt, outcome, entry, shares, size, th in rows:
        mins = minutes_left(title)
        if mins is None or mins > 0:
            continue
        res = infer_result(title, mt, outcome, btc, th)
        if not res:
            continue
        pnl = round(float(shares) - float(size), 2) if res == "WIN" else -float(size)
        c.execute("UPDATE trades SET status='CLOSED', result=?, pnl=?, btc_close=?, date_closed=? WHERE id=?", (res, pnl, btc, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), tid))
        closed += 1
    conn.commit(); conn.close(); return closed


def run_scan(open_new=True):
    ctx = btc_context(); markets = fetch_markets(); candidates = []
    rejected = {"markets_found": len(markets), "bad_price": 0, "already_open": 0, "score_below_open": 0, "opened": 0}
    for row in markets:
        price = safe_float(row["price"])
        if price <= MIN_PRICE or price >= MAX_PRICE:
            rejected["bad_price"] += 1; continue
        score, dist, reasons = score_candidate(row, ctx)
        act = action(score); already = trade_open(row["slug"], row["outcome"])
        r = dict(row); r.update({"score": score, "distance_pct": dist, "reasons": reasons, "action": act, "already_open": already})
        candidates.append(r)
        if act == "BUY":
            if already: rejected["already_open"] += 1
            elif open_new and open_trade(row, ctx, score, dist, reasons): rejected["opened"] += 1
        else:
            rejected["score_below_open"] += 1
    candidates = sorted(candidates, key=lambda x: x["score"], reverse=True)
    conn = db_connect(); c = conn.cursor()
    c.execute("INSERT INTO snapshots (created_at,btc_live,trend_15m,trend_1h,rsi_15m,rsi_1h,volume_signal,fear_greed,markets_found,candidates_count,opened_count,rejected_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), ctx["btc_live"], ctx["trend_15m"], ctx["trend_1h"], ctx["rsi_15m"], ctx["rsi_1h"], ctx["volume_signal"], ctx["fear_greed"], len(markets), len(candidates), rejected["opened"], json.dumps(rejected)))
    conn.commit(); conn.close()
    result = {"context": ctx, "markets": markets, "candidates": candidates[:100], "rejected": rejected}
    STATE["last_scan"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S"); STATE["last_result"] = result; STATE["last_error"] = "-"
    return result

@app.get("/source-debug", response_class=HTMLResponse)
def source_debug():
    init_db()
    accepted = fetch_markets()
    raw = STATE.get("raw_market_sample", [])
    rejection = STATE.get("source_rejection", {})

    html = header("Source Debug")
    html += "<h1>🧪 Source Debug</h1>"

    html += "<div class='grid'>"
    html += kpi("Raw seen", rejection.get("raw_seen", 0))
    html += kpi("Accepted", len(accepted))
    html += kpi("Not BTC", rejection.get("not_btc", 0))
    html += kpi("Bad threshold", rejection.get("bad_threshold", 0))
    html += kpi("Bad expiry", rejection.get("bad_expiry", 0))
    html += kpi("Bad outcomes", rejection.get("bad_outcomes", 0))
    html += "</div>"

    html += "<div class='section'><h2>Accepted BTC Markets</h2>"
    html += "<table><tr><th>Market</th><th>Outcome</th><th>Price</th><th>Type</th><th>Threshold</th><th>Time</th><th>Slug</th></tr>"

    if not accepted:
        html += "<tr><td colspan='7'>Aucun marché accepté.</td></tr>"

    for r in accepted[:100]:
        html += f"""
        <tr>
            <td>{r['title']}</td>
            <td><b>{r['outcome']}</b></td>
            <td>{r['price']:.3f}</td>
            <td>{r['market_type']}</td>
            <td>{r['threshold']:.0f}</td>
            <td>{r['minutes_left']:.1f}</td>
            <td class='small'>{r['slug']}</td>
        </tr>
        """

    html += "</table></div>"

    html += "<div class='section'><h2>Raw Gamma Sample</h2>"
    html += "<table><tr><th>Title</th><th>Slug</th><th>Active</th><th>Closed</th><th>EndDate</th><th>Outcomes</th><th>Prices</th></tr>"

    for m in raw[:200]:
        title = m.get("question") or m.get("title") or m.get("name") or ""
        slug = m.get("slug") or ""
        html += f"""
        <tr>
            <td>{title}</td>
            <td class='small'>{slug}</td>
            <td>{m.get('active')}</td>
            <td>{m.get('closed')}</td>
            <td>{m.get('endDate') or m.get('end_date') or m.get('endDateIso')}</td>
            <td class='small'>{m.get('outcomes')}</td>
            <td class='small'>{m.get('outcomePrices')}</td>
        </tr>
        """

    html += "</table></div>"
    html += footer()
    return html



def scanner_loop():
    init_db()
    while True:
        try:
            closed = resolve_trades(); result = run_scan(open_new=True)
            print(f"SCAN OK markets={len(result['markets'])} candidates={len(result['candidates'])} opened={result['rejected']['opened']} closed={closed}")
        except Exception as e:
            STATE["last_error"] = str(e); print("SCAN ERROR", e)
        time.sleep(SCAN_INTERVAL_SECONDS)


def stats():
    conn = db_connect(); c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM trades"); total = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM trades WHERE status='OPEN'"); op = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM trades WHERE status='CLOSED'"); cl = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM trades WHERE result='WIN'"); wins = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM trades WHERE result='LOSS'"); losses = c.fetchone()[0]
    c.execute("SELECT COALESCE(SUM(pnl),0) FROM trades WHERE status='CLOSED'"); pnl = c.fetchone()[0]
    wr = wins / cl * 100 if cl else 0; roi = pnl / (cl * PAPER_TRADE_SIZE) * 100 if cl else 0
    c.execute("SELECT date_opened,title,outcome,entry_price,btc_open,trend_15m,trend_1h,rsi_15m,volume_signal,fear_greed,distance_pct,minutes_left,score,status,result,pnl,reasons FROM trades ORDER BY id DESC LIMIT 100")
    recent = c.fetchall(); conn.close()
    return {"total": total, "open": op, "closed": cl, "wins": wins, "losses": losses, "pnl": pnl, "winrate": wr, "roi": roi, "recent": recent}


def num_cls(v):
    v = safe_float(v)
    return "pos" if v > 0 else "neg" if v < 0 else ""


def header(title):
    return f"""<!doctype html><html><head><meta charset='utf-8'><title>{title}</title><meta http-equiv='refresh' content='60'><style>
    body{{font-family:Arial;margin:22px;background:#0f172a;color:#e5e7eb}}a{{color:#93c5fd;margin-right:14px;text-decoration:none}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin:18px 0}}.card,.section{{background:#111827;border:1px solid #1f2937;border-radius:10px;padding:14px;margin:14px 0}}.label{{color:#9ca3af;font-size:12px}}.value{{font-size:22px;font-weight:bold;margin-top:6px}}table{{width:100%;border-collapse:collapse;font-size:13px}}th,td{{border-bottom:1px solid #1f2937;padding:8px;vertical-align:top}}th{{background:#0b1220;color:#cbd5e1;text-align:left}}.small{{font-size:12px;color:#94a3b8}}.pos{{color:#22c55e}}.neg{{color:#ef4444}}.buy{{color:#22c55e;font-weight:bold}}.watch{{color:#f59e0b;font-weight:bold}}.skip{{color:#94a3b8}}
    </style></head><body><div><a href='/'>Dashboard</a><a href='/candidates'>Candidates</a><a href='/trades'>Paper Trades</a><a href='/source'>Market Source</a><a href='/source-debug'>Source Debug</a></div>"""


def footer(): return "</body></html>"
def kpi(label, value, suffix='', css=''): return f"<div class='card'><div class='label'>{label}</div><div class='value {css}'>{value}{suffix}</div></div>"


def candidate_table(cands, limit=100):
    html = "<table><tr><th>Rank</th><th>Action</th><th>Score</th><th>Market</th><th>Outcome</th><th>Price</th><th>Type</th><th>Threshold</th><th>Distance</th><th>Time</th><th>Open</th><th>Reasons</th></tr>"
    if not cands: html += "<tr><td colspan='12'>Aucun candidat actif.</td></tr>"
    for i, r in enumerate(cands[:limit], 1):
        css = 'buy' if r['action']=='BUY' else 'watch' if r['action']=='WATCH' else 'skip'
        html += f"<tr><td>{i}</td><td class='{css}'>{r['action']}</td><td><b>{r['score']:.1f}</b></td><td>{r['title']}</td><td><b>{r['outcome']}</b></td><td>{r['price']:.3f}</td><td>{r['market_type']}</td><td>{r['threshold']:.0f}</td><td>{safe_float(r['distance_pct']):.2f}%</td><td>{r['minutes_left']:.1f} min</td><td>{'✅' if r.get('already_open') else ''}</td><td class='small'>{r['reasons']}</td></tr>"
    return html + "</table>"


@app.get('/', response_class=HTMLResponse)
def dashboard():
    init_db(); result = run_scan(open_new=False); st = stats(); c = result['context']
    html = header('BTC AI Trader V2') + "<h1>🤖 BTC AI Trader V2 — Autonome</h1>"
    html += f"<p class='small'>Dernier scan : {STATE.get('last_scan')} | Erreur : {STATE.get('last_error')}</p><div class='grid'>"
    for label, val in [('BTC Live', f"{c['btc_live']:.2f}"), ('Trend 15m', c['trend_15m']), ('Trend 1h', c['trend_1h']), ('RSI 15m', c['rsi_15m']), ('RSI 1h', c['rsi_1h']), ('Volume', c['volume_signal']), ('Fear & Greed', c['fear_greed'] if c['fear_greed'] is not None else '-'), ('Markets found', len(result['markets']))]: html += kpi(label, val)
    html += "</div><div class='grid'>"
    html += kpi('Paper total', st['total']) + kpi('Open', st['open']) + kpi('Closed', st['closed']) + kpi('Wins', st['wins']) + kpi('Losses', st['losses']) + kpi('Winrate', f"{st['winrate']:.2f}", '%') + kpi('PnL', f"{st['pnl']:.2f}", ' USDC', num_cls(st['pnl'])) + kpi('ROI', f"{st['roi']:.2f}", '%', num_cls(st['roi']))
    html += "</div><div class='section'><h2>🔥 Top Candidates</h2>" + candidate_table(result['candidates'], 20) + "</div>" + footer()
    return html


@app.get('/candidates', response_class=HTMLResponse)
def candidates():
    result = run_scan(open_new=False); r = result['rejected']; html = header('Candidates') + '<h1>🔍 BTC Candidates</h1><div class="grid">'
    for label, key in [('Markets found','markets_found'),('Bad price','bad_price'),('Already open','already_open'),('Score below open','score_below_open'),('Opened last scan','opened')]: html += kpi(label, r.get(key,0))
    html += "</div><div class='section'>" + candidate_table(result['candidates'], 100) + "</div>" + footer(); return html


@app.get('/trades', response_class=HTMLResponse)
def trades():
    st = stats(); html = header('Trades') + '<h1>📄 Paper Trades autonomes</h1><div class="grid">'
    html += kpi('Total', st['total']) + kpi('Open', st['open']) + kpi('Closed', st['closed']) + kpi('Winrate', f"{st['winrate']:.2f}", '%') + kpi('PnL', f"{st['pnl']:.2f}", ' USDC', num_cls(st['pnl'])) + kpi('ROI', f"{st['roi']:.2f}", '%', num_cls(st['roi'])) + '</div>'
    html += "<div class='section'><table><tr><th>Date</th><th>Market</th><th>Outcome</th><th>Entry</th><th>BTC</th><th>Trend</th><th>RSI</th><th>Volume</th><th>F&G</th><th>Distance</th><th>Time</th><th>Score</th><th>Status</th><th>Result</th><th>PnL</th><th>Reasons</th></tr>"
    if not st['recent']: html += "<tr><td colspan='16'>Aucun trade.</td></tr>"
    for row in st['recent']:
        d,t,o,e,b,t15,t1h,rs,vol,fg,dist,mins,sc,status,res,pnl,reasons = row
        html += f"<tr><td>{d}</td><td>{t}</td><td><b>{o}</b></td><td>{safe_float(e):.3f}</td><td>{safe_float(b):.2f}</td><td>{t15}/{t1h}</td><td>{safe_float(rs):.1f}</td><td>{vol}</td><td>{fg}</td><td>{safe_float(dist):.2f}%</td><td>{safe_float(mins):.1f}</td><td><b>{safe_float(sc):.1f}</b></td><td>{status}</td><td>{res}</td><td class='{num_cls(pnl)}'>{safe_float(pnl):.2f}</td><td class='small'>{reasons}</td></tr>"
    return html + '</table></div>' + footer()


@app.get('/source', response_class=HTMLResponse)
def source():
    rows = fetch_markets(); html = header('Source') + '<h1>🧪 BTC Market Source</h1>' + f"<div class='grid'>{kpi('Markets active', len(rows))}</div>"
    html += "<div class='section'><table><tr><th>Market</th><th>Outcome</th><th>Price</th><th>Type</th><th>Threshold</th><th>Time</th><th>Slug</th></tr>"
    if not rows: html += "<tr><td colspan='7'>Aucun marché trouvé.</td></tr>"
    for r in rows[:200]: html += f"<tr><td>{r['title']}</td><td><b>{r['outcome']}</b></td><td>{r['price']:.3f}</td><td>{r['market_type']}</td><td>{r['threshold']:.0f}</td><td>{r['minutes_left']:.1f} min</td><td class='small'>{r['slug']}</td></tr>"
    return html + '</table></div>' + footer()


init_db()
threading.Thread(target=scanner_loop, daemon=True).start()
