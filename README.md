# TradingView CEX Screener Monitor Agent

Überwacht den TradingView Coin Screener (CEX) und benachrichtigt dich per Email bei Änderungen.

## Was wird getrackt?

- 🟢 **Neue Coins** tauchen im Screener auf
- 🔴 **Coins verschwinden** aus dem Screener
- 📊 **Ranking-Shifts** (default: ≥10 Plätze)
- 💰 **Große Preisbewegungen** zwischen Checks (default: ≥10%)
- 📈 **Volume Spikes** (default: ≥3x)

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env   # dann editieren
python screener_agent.py
```

## Email Setup (Gmail)

1. Google Account → Security → 2-Step Verification aktivieren
2. Google Account → Security → App Passwords → generieren
3. In `.env` eintragen:

```
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=deine.email@gmail.com
SMTP_PASSWORD=abcd efgh ijkl mnop
FROM_EMAIL=deine.email@gmail.com
TO_EMAIL=deine.email@gmail.com
```

## Nutzung

```bash
python screener_agent.py                              # einmal (für cron)
python screener_agent.py --daemon                     # dauerhaft, alle 5 min
python screener_agent.py --daemon --interval 120      # alle 2 min
python screener_agent.py --limit 500 --rank-threshold 5 --price-threshold 5
```

## Cron

```bash
*/5 * * * * cd /pfad/zu/tv-screener-agent && python3 screener_agent.py >> cron.log 2>&1
```
