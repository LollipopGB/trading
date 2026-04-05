"""
TradingView Alert System — yfinance + Pre/Post Market
=====================================================
Features:
  - yfinance for prices (pre-market, regular, after-hours)
  - RSI calculated locally via pandas_ta (no Apify needed)
  - Auto market session detection: PRE_MARKET | REGULAR | AFTER_HOURS | CLOSED
  - Per-session alert thresholds (pre-market is more volatile → wider bands)
  - State persisted to GitHub Gist between GitHub Actions runs
  - HTML email alerts with session badge

Requirements:
    pip install yfinance pandas pandas-ta python-dotenv requests
"""

import os
import json
import logging
import time
import requests
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from dataclasses import dataclass, field
from enum import Enum
import base64
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from dotenv import load_dotenv
import yfinance as yf

try:
    import pandas_ta as ta
    TA_AVAILABLE = True
except ImportError:
    TA_AVAILABLE = False

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

SCOPES = ["https://www.googleapis.com/auth/gmail.send"]

# ---------------------------------------------------------------------------
# Market session
# ---------------------------------------------------------------------------

class Session(str, Enum):
    PRE_MARKET  = "Pre-Market"
    REGULAR     = "Regular"
    AFTER_HOURS = "After-Hours"
    CLOSED      = "Closed"

    @property
    def color(self) -> str:
        return {
            Session.PRE_MARKET:  "#7B1FA2",
            Session.REGULAR:     "#1565C0",
            Session.AFTER_HOURS: "#E65100",
            Session.CLOSED:      "#546E7A",
        }[self]

    @property
    def emoji(self) -> str:
        return {
            Session.PRE_MARKET:  "🌅",
            Session.REGULAR:     "📈",
            Session.AFTER_HOURS: "🌙",
            Session.CLOSED:      "💤",
        }[self]


def get_session(now_et: datetime | None = None) -> Session:
    """Return current US equity market session based on ET time."""
    now = now_et or datetime.now(ET)
    weekday = now.weekday()  # 0=Mon … 6=Sun
    if weekday >= 5:
        return Session.CLOSED
    t = now.time()
    if dtime(4, 0)  <= t < dtime(9, 30):  return Session.PRE_MARKET
    if dtime(9, 30) <= t < dtime(16, 0):  return Session.REGULAR
    if dtime(16, 0) <= t < dtime(20, 0):  return Session.AFTER_HOURS
    return Session.CLOSED

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class SessionThresholds:
    """Alert thresholds can differ per session.
    Pre/after-market is thinner — wider % swings are normal noise."""
    pct_change: float   # fire if |Δ%| >= this since last run
    rsi_ob: float       # RSI overbought level
    rsi_os: float       # RSI oversold level


@dataclass
class Config:
    # GitHub Gist for state persistence across GitHub Actions runs
    github_token: str  = os.getenv("GITHUB_TOKEN", "")
    gist_id: str       = os.getenv("GIST_ID", "")      # leave blank to auto-create

    # Email
    smtp_host: str       = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port: int       = int(os.getenv("SMTP_PORT", 587))
    email_sender: str    = os.getenv("EMAIL_SENDER", "")
    email_password: str  = os.getenv("EMAIL_PASSWORD", "")
    email_recipient: str = os.getenv("EMAIL_RECIPIENT", "")

    # Stocks only — yfinance ticker format ("AAPL", "NVDA")
    stock_symbols: list = field(default_factory=lambda: ["AAPL", "NVDA", "TSLA", "MSFT"])

    # Price-level alerts
    price_alerts: dict = field(default_factory=lambda: {
        "AAPL": [(">=", 250), ("<=", 180)],
        "NVDA": [(">=", 1000)],
    })

    # Per-session thresholds
    thresholds: dict = field(default_factory=lambda: {
        Session.PRE_MARKET:  SessionThresholds(pct_change=1.5, rsi_ob=75, rsi_os=25),
        Session.REGULAR:     SessionThresholds(pct_change=3.0, rsi_ob=70, rsi_os=30),
        Session.AFTER_HOURS: SessionThresholds(pct_change=2.0, rsi_ob=73, rsi_os=27),
        Session.CLOSED:      SessionThresholds(pct_change=5.0, rsi_ob=70, rsi_os=30),
    })

    cooldown_sec: int = int(os.getenv("COOLDOWN_SEC", "300"))


cfg     = Config()
session = get_session()
thresholds = cfg.thresholds[session]

log.info(f"Session: {session.emoji} {session.value} | "
         f"Δ%≥{thresholds.pct_change} | RSI OB>{thresholds.rsi_ob} OS<{thresholds.rsi_os}")

# ---------------------------------------------------------------------------
# State — persisted to GitHub Gist
# ---------------------------------------------------------------------------

GIST_FILENAME = "tv_alert_state.json"

def _gist_headers() -> dict:
    return {"Authorization": f"token {cfg.github_token}",
            "Accept": "application/vnd.github+json"}


def load_state() -> dict:
    """Load state from GitHub Gist, fall back to local file."""
    if cfg.github_token and cfg.gist_id:
        try:
            r = requests.get(f"https://api.github.com/gists/{cfg.gist_id}",
                             headers=_gist_headers(), timeout=10)
            r.raise_for_status()
            content = r.json()["files"][GIST_FILENAME]["content"]
            log.info("State loaded from GitHub Gist.")
            return json.loads(content)
        except Exception as e:
            log.warning(f"Could not load Gist state: {e}")
    # fallback: local file
    try:
        return json.loads(open(".alert_state.json").read())
    except Exception:
        return {"prices": {}, "cooldowns": {}}


def save_state(state: dict):
    """Save state to GitHub Gist (auto-creates if GIST_ID is blank)."""
    payload = json.dumps(state, indent=2)
    if cfg.github_token:
        body = {"files": {GIST_FILENAME: {"content": payload}},
                "description": "TV alert state", "public": False}
        try:
            if cfg.gist_id:
                r = requests.patch(f"https://api.github.com/gists/{cfg.gist_id}",
                                   headers=_gist_headers(), json=body, timeout=10)
            else:
                r = requests.post("https://api.github.com/gists",
                                  headers=_gist_headers(), json=body, timeout=10)
                new_id = r.json().get("id", "")
                if new_id:
                    log.info(f"Created new Gist. Add GIST_ID={new_id} to your secrets.")
            r.raise_for_status()
            log.info("State saved to GitHub Gist.")
            return
        except Exception as e:
            log.warning(f"Could not save Gist state: {e}")
    # fallback: local file
    open(".alert_state.json", "w").write(payload)


state = load_state()

# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def _get_gmail_service():
    """Load credentials from token.json, refresh if expired."""
    creds = None
    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open("token.json", "w") as f:
                f.write(creds.to_json())
        else:
            raise RuntimeError("token.json missing or invalid. Run gmail_auth.py first.")
    return build("gmail", "v1", credentials=creds)


def send_email(subject: str, html_body: str) -> bool:
    if not cfg.email_recipient:
        log.warning("EMAIL_RECIPIENT not set — skipping.")
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = cfg.email_sender
        msg["To"]      = cfg.email_recipient
        msg.attach(MIMEText(html_body, "html"))

        raw     = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        service = _get_gmail_service()
        service.users().messages().send(
            userId="me",
            body={"raw": raw}
        ).execute()

        log.info(f"✉️  Sent: {subject}")
        return True
    except Exception as e:
        log.error(f"Gmail API error: {e}")
        return False


def email_html(title: str, rows: list[tuple], color: str = "#1565C0",
               session: Session = Session.REGULAR) -> str:
    rows_html = "".join(
        f"<tr><td style='padding:6px 14px;color:#666;font-size:13px;'>{k}</td>"
        f"<td style='padding:6px 14px;font-weight:600;color:#111;font-size:13px;'>{v}</td></tr>"
        for k, v in rows
    )
    badge_color = session.color
    return f"""
    <html><body style='font-family:Arial,sans-serif;background:#f0f0f0;padding:24px;'>
      <div style='max-width:520px;margin:auto;background:#fff;border-radius:10px;
                  box-shadow:0 2px 12px rgba(0,0,0,.12);overflow:hidden;'>
        <div style='background:{color};padding:18px 22px;display:flex;align-items:center;gap:12px;'>
          <div>
            <h2 style='color:#fff;margin:0;font-size:17px;'>{session.emoji} {title}</h2>
          </div>
          <span style='margin-left:auto;background:{badge_color};color:#fff;
                       padding:3px 10px;border-radius:20px;font-size:11px;font-weight:700;
                       white-space:nowrap;'>
            {session.value}
          </span>
        </div>
        <table style='width:100%;border-collapse:collapse;'>{rows_html}</table>
        <div style='padding:10px 14px;color:#aaa;font-size:11px;border-top:1px solid #f0f0f0;'>
          {datetime.now(ET).strftime("%Y-%m-%d %H:%M ET")} · yfinance + pandas_ta
        </div>
      </div>
    </body></html>"""

# ---------------------------------------------------------------------------
# Cooldown
# ---------------------------------------------------------------------------

def is_cooled(key: str) -> bool:
    return (time.time() - state["cooldowns"].get(key, 0)) > cfg.cooldown_sec

def mark_fired(key: str):
    state["cooldowns"][key] = time.time()

# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

def fetch_yfinance_prices(tickers: list[str]) -> dict[str, dict]:
    """
    Fetch prices from yfinance with pre/post market support.
    Returns {ticker: {"price": float, "session": Session, "prev_close": float}}
    """
    result = {}
    for ticker in tickers:
        try:
            t    = yf.Ticker(ticker)
            info = t.fast_info
            now_session = get_session()

            if now_session == Session.PRE_MARKET:
                price = getattr(info, "pre_market_price", None) or info.last_price
                log.info(f"  {ticker}: pre-market ${price:.2f}")
            elif now_session == Session.AFTER_HOURS:
                price = getattr(info, "post_market_price", None) or info.last_price
                log.info(f"  {ticker}: after-hours ${price:.2f}")
            else:
                price = info.last_price
                log.info(f"  {ticker}: regular ${price:.2f}")

            result[ticker] = {
                "price":      float(price),
                "prev_close": float(info.previous_close or price),
                "session":    now_session,
            }
        except Exception as e:
            log.warning(f"yfinance error for {ticker}: {e}")
    return result


def fetch_rsi(tickers: list[str]) -> dict[str, float]:
    """
    Calculate RSI locally via yfinance + pandas_ta. Completely free.
    Returns {ticker: rsi_value}
    """
    if not TA_AVAILABLE:
        log.warning("pandas_ta not installed — skipping RSI checks.")
        return {}

    result = {}
    for ticker in tickers:
        try:
            df = yf.download(ticker, period="30d", interval="1d", progress=False)
            if df.empty or len(df) < 15:
                log.warning(f"  Not enough data for RSI: {ticker}")
                continue
            close      = df["Close"].squeeze()
            rsi_series = ta.rsi(close, length=14)
            if rsi_series is None or rsi_series.dropna().empty:
                continue
            rsi_val        = float(rsi_series.dropna().iloc[-1])
            result[ticker] = rsi_val
            log.info(f"  RSI {ticker}: {rsi_val:.1f}")
        except Exception as e:
            log.warning(f"RSI error for {ticker}: {e}")
    return result

# ---------------------------------------------------------------------------
# Alert checks
# ---------------------------------------------------------------------------

def check_price_level(symbol: str, price: float, current_session: Session):
    for op, threshold in cfg.price_alerts.get(symbol, []):
        hit = (
            (op == ">=" and price >= threshold) or
            (op == "<=" and price <= threshold) or
            (op == ">"  and price >  threshold) or
            (op == "<"  and price <  threshold)
        )
        if hit:
            key = f"{symbol}:price:{op}{threshold}"
            if is_cooled(key):
                mark_fired(key)
                log.info(f"🔔 Price level: {symbol} {op} ${threshold:,} @ ${price:,.2f}")
                #send_email(
                #    f"[{current_session.value}] {symbol} {op} ${threshold:,}",
                #    email_html(
                #        f"Price Level — {symbol}",
                #        [("Symbol", symbol), ("Condition", f"{op} ${threshold:,}"),
                #         ("Current Price", f"${price:,.4f}"), ("Session", current_session.value)],
                #        color="#F57F17", session=current_session,
                #    )
                #)


def check_pct_change(symbol: str, price: float, current_session: Session):
    prev = state["prices"].get(symbol)
    if prev is None:
        return
    pct       = ((price - prev) / prev) * 100
    threshold = thresholds.pct_change
    if abs(pct) >= threshold:
        key = f"{symbol}:pct:{current_session.value}"
        if is_cooled(key):
            mark_fired(key)
            direction = "▲" if pct > 0 else "▼"
            color     = "#2E7D32" if pct > 0 else "#C62828"
            log.info(f"🔔 % change [{current_session.value}]: {symbol} {pct:+.2f}%")
            """
            send_email(
                f"[{current_session.value}] {symbol} moved {pct:+.2f}%",
                email_html(
                    f"Price Move — {symbol}",
                    [("Symbol", symbol), ("Move", f"{direction} {abs(pct):.2f}%"),
                     ("Previous", f"${prev:,.4f}"), ("Current", f"${price:,.4f}"),
                     ("Session", current_session.value),
                     ("Threshold", f"≥{threshold}% ({current_session.value})")],
                    color=color, session=current_session,
                )
            )
            """


def check_rsi(symbol: str, rsi: float, current_session: Session):
    ob  = thresholds.rsi_ob
    os_ = thresholds.rsi_os
    if rsi >= ob:
        key = f"{symbol}:rsi:ob:{current_session.value}"
        if is_cooled(key):
            mark_fired(key)
            log.info(f"🔔 RSI OB [{current_session.value}]: {symbol} {rsi:.1f}")
            """
            send_email(
                f"[{current_session.value}] RSI Overbought — {symbol} ({rsi:.1f})",
                email_html(
                    f"RSI Overbought — {symbol}",
                    [("Symbol", symbol), ("RSI", f"{rsi:.2f}"),
                     ("Threshold", f">{ob} ({current_session.value})"),
                     ("Session", current_session.value)],
                    color="#E53935", session=current_session,
                )
            )
            """
    elif rsi <= os_:
        key = f"{symbol}:rsi:os:{current_session.value}"
        if is_cooled(key):
            mark_fired(key)
            log.info(f"🔔 RSI OS [{current_session.value}]: {symbol} {rsi:.1f}")
            """
            send_email(
                f"[{current_session.value}] RSI Oversold — {symbol} ({rsi:.1f})",
                email_html(
                    f"RSI Oversold — {symbol}",
                    [("Symbol", symbol), ("RSI", f"{rsi:.2f}"),
                     ("Threshold", f"<{os_} ({current_session.value})"),
                     ("Session", current_session.value)],
                    color="#1565C0", session=current_session,
                )
            )
            """

# ---------------------------------------------------------------------------
# Main run
# ---------------------------------------------------------------------------

def run():
    log.info(f"=== Alert Run | {session.emoji} {session.value} ===")
    print('A')
    all_prices: dict[str, float] = {}

    # ── Prices via yfinance (pre/regular/after-hours aware) ─────────────────
    yf_data = fetch_yfinance_prices(cfg.stock_symbols)
    for ticker, data in yf_data.items():
        price          = data["price"]
        ticker_session = data["session"]
        all_prices[ticker] = price
        print('B -> {}'.format(all_prices))
        check_price_level(ticker, price, ticker_session)
        check_pct_change(ticker, price, ticker_session)

    # ── RSI via yfinance + pandas_ta (free, no Apify) ───────────────────────
    rsi_map = fetch_rsi(cfg.stock_symbols)
    for ticker, rsi in rsi_map.items():
        check_rsi(ticker, rsi, session)

    # ── Save updated state ───────────────────────────────────────────────────
    state["prices"].update(all_prices)
    save_state(state)

    log.info(f"Run complete — tracked {len(all_prices)} symbols.")
    for sym, px in all_prices.items():
        log.info(f"  {sym}: ${px:,.4f}")


if __name__ == "__main__":
    run()