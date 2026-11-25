#!/usr/bin/env python3
"""
Monitor Harga Emas — robot pengambil harga harian (dijalankan GitHub Actions).
v1.8.0  © 2026 Faiq Aziz

Mengambil:
  - Harga emas dunia + riwayat s.d. 20 tahun          : Bundesbank (cadangan: LBMA, Yahoo, freegoldapi, stooq, datahub bulanan, gold-api)
  - Kurs USD/IDR + riwayat                            : ECB via frankfurter.app (cadangan: Yahoo, stooq, FRED bulanan, open.er-api)
  - Riwayat Antam                                     : grafik logammulia.com (jika tersedia)
  - Harga Antam & UBS (jual + buyback, 1 gram)        : galeri24.co.id (cadangan Antam: logammulia.com)
Lalu menghitung skor analisa, menyimpan ke prices.json, dan mengirim notifikasi ntfy.

Hanya memakai pustaka bawaan Python (tanpa pip install).
"""
import csv
import html
import io
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

VERSION = "1.8.0"
DATA_FILE = os.environ.get("DATA_FILE", "prices.json")
WIB = timezone(timedelta(hours=7))
GRAM_PER_OZ = 31.1034768
UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1")

# ---------- pengaturan (bisa diubah lewat Variables/Secrets repo) ----------
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
TARGET_UBS = float(os.environ.get("TARGET_UBS", "0") or 0)        # notif bila UBS 1 g <= ini (0 = mati)
TARGET_ANTAM = float(os.environ.get("TARGET_ANTAM", "0") or 0)
DAILY_DIGEST = os.environ.get("DAILY_DIGEST", "0") == "1"          # ringkasan tiap pagi (default mati)
NOTIF_CICIL = os.environ.get("NOTIF_CICIL", "0") == "1"            # notif zona CICIL (default mati)
NOTIF_DROP = os.environ.get("NOTIF_DROP", "1") == "1"              # notif harga turun tajam dalam sehari
DROP_PCT = float(os.environ.get("DROP_PCT", "2") or 2)             # ambang turun (%)
NOTIF_EVENT = os.environ.get("NOTIF_EVENT", "1") == "1"            # pengingat H-1 keputusan The Fed / data inflasi AS
NOTIF_WEEKLY = os.environ.get("NOTIF_WEEKLY", "1") == "1"          # ringkasan mingguan tiap Senin pagi
BUY_SCORE = 70                                                     # skor >= ini = zona BELI


def log(*a):
    print(*a, file=sys.stderr)


def get(url, timeout=30):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/json,text/csv,*/*",
        "Accept-Language": "id-ID,id;q=0.9,en;q=0.8",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def rp_to_int(s):
    digits = re.sub(r"[^\d]", "", s or "")
    return int(digits) if digits else None


# ======================= HARGA DUNIA & KURS =======================
def stooq_history(symbol):
    """Kembalikan dict {YYYY-MM-DD: close}."""
    txt = get(f"https://stooq.com/q/d/l/?s={symbol}&i=d")
    out = {}
    for row in csv.DictReader(io.StringIO(txt)):
        try:
            out[row["Date"]] = float(row["Close"])
        except (KeyError, ValueError, TypeError):
            continue
    if len(out) < 10:
        raise RuntimeError(f"stooq {symbol}: data terlalu sedikit")
    return out


def yahoo_history(symbol):
    """Riwayat harian Yahoo Finance (GC=F emas berjangka sejak 2000, IDR=X kurs). {YYYY-MM-DD: close}"""
    last_err = None
    for host in ("query1", "query2"):
        try:
            j = json.loads(get(f"https://{host}.finance.yahoo.com/v8/finance/chart/{symbol}"
                               f"?range=max&interval=1d&includePrePost=false", timeout=40))
            res = j["chart"]["result"][0]
            ts, cl = res["timestamp"], res["indicators"]["quote"][0]["close"]
            out = {}
            for t, c in zip(ts, cl):
                if c:
                    out[datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d")] = float(c)
            if len(out) > 100:
                return out
            last_err = RuntimeError("data sedikit")
        except Exception as e:  # noqa: BLE001
            last_err = e
    raise RuntimeError(f"yahoo {symbol}: {last_err}")


def parse_date_value_csv(txt):
    """Ambil pasangan tanggal,nilai dari CSV apa pun (SDMX/CSV biasa). {YYYY-MM-DD: float}"""
    out = {}
    rows = list(csv.reader(io.StringIO(txt.replace(";", ","))))
    hdr_i, ti, vi = None, None, None
    for i, r in enumerate(rows[:30]):
        up = [c.strip().upper() for c in r]
        if "TIME_PERIOD" in up and "OBS_VALUE" in up:
            hdr_i, ti, vi = i, up.index("TIME_PERIOD"), up.index("OBS_VALUE")
            break
    for r in rows[(hdr_i + 1) if hdr_i is not None else 0:]:
        try:
            if ti is not None:
                d, v = r[ti].strip(), r[vi].strip()
            else:
                if len(r) < 2 or not re.match(r"^\d{4}-\d{2}-\d{2}$", r[0].strip()):
                    continue
                d, v = r[0].strip(), r[1].strip()
            if re.match(r"^\d{4}-\d{2}-\d{2}$", d) and v not in ("", ".", "NaN"):
                out[d] = float(v)
        except (ValueError, IndexError):
            continue
    return out


def bundesbank_history():
    """Harga emas London (USD/oz) harian dari statistik resmi Deutsche Bundesbank (gratis, tanpa kunci)."""
    start = f"{datetime.now(WIB).year - 21}-01-01"
    urls = [
        f"https://api.statistiken.bundesbank.de/rest/data/BBEX3/D.XAU.USD.EA.AC.C04?format=sdmx_csv&startPeriod={start}",
        f"https://api.statistiken.bundesbank.de/rest/download/BBEX3/D.XAU.USD.EA.AC.C04?format=csv&lang=en",
        f"https://api.statistiken.bundesbank.de/rest/data/BBEX3/D.XAU.USD.EA.AC.C05?format=sdmx_csv&startPeriod={start}",
    ]
    last_err = None
    for u in urls:
        try:
            out = parse_date_value_csv(get(u, timeout=60))
            if len(out) > 500:
                return out
            last_err = RuntimeError(f"data sedikit ({len(out)})")
        except Exception as e:  # noqa: BLE001
            last_err = e
    raise RuntimeError(f"Bundesbank: {last_err}")


def freegold_history():
    """Cadangan: dataset terbuka freegoldapi.com."""
    out = parse_date_value_csv(get("https://freegoldapi.com/data/latest.csv", timeout=60))
    if len(out) < 500:
        raise RuntimeError(f"freegoldapi: data sedikit ({len(out)})")
    return out


def lbma_history():
    """Harga resmi LBMA Gold Price PM (USD/oz) harian sejak 1968 — sumber resmi pasar emas London."""
    last_err = None
    for kind in ("gold_pm", "gold_am"):
        try:
            rows = json.loads(get(f"https://prices.lbma.org.uk/json/{kind}.json", timeout=60))
            out = {}
            for r in rows:
                v = (r.get("v") or [None])[0]
                if r.get("d") and v:
                    out[r["d"][:10]] = float(v)
            if len(out) > 1000:
                return out
            last_err = RuntimeError("data sedikit")
        except Exception as e:  # noqa: BLE001
            last_err = e
    raise RuntimeError(f"LBMA: {last_err}")


def frankfurter_idr(start_year):
    """Kurs referensi ECB USD/IDR harian via api.frankfurter.app (gratis, tanpa kunci)."""
    out = {}
    this_year = datetime.now(WIB).year
    for y in range(start_year, this_year + 1):
        end = f"{y}-12-31" if y < this_year else datetime.now(WIB).strftime("%Y-%m-%d")
        j = json.loads(get(f"https://api.frankfurter.app/{y}-01-01..{end}?from=USD&to=IDR", timeout=40))
        for d, v in (j.get("rates") or {}).items():
            if v.get("IDR"):
                out[d] = float(v["IDR"])
    if len(out) < 100:
        raise RuntimeError("frankfurter: data sedikit")
    return out


def monthly_gold_history():
    """Cadangan riwayat panjang (bulanan, USD/oz) dari dataset publik datasets/gold-prices."""
    txt = get("https://raw.githubusercontent.com/datasets/gold-prices/main/data/monthly.csv")
    out = {}
    for row in csv.DictReader(io.StringIO(txt)):
        try:
            d = row.get("Date") or row.get("date")
            out[(d + "-15")[:10] if len(d) == 7 else d[:10]] = float(row.get("Price") or row.get("price"))
        except (TypeError, ValueError):
            continue
    return out


def monthly_idr_history():
    """Cadangan kurs bulanan USD/IDR dari FRED (St. Louis Fed)."""
    txt = get("https://fred.stlouisfed.org/graph/fredgraph.csv?id=CCUSMA02IDM618N")
    out = {}
    for row in csv.reader(io.StringIO(txt)):
        try:
            out[row[0][:8] + "15"] = float(row[1])
        except (IndexError, ValueError):
            continue
    return out


def logammulia_history():
    """Coba ambil riwayat harga Antam dari grafik situs Logam Mulia. {d: {'jual':x,'bb':y}}"""
    out = {}
    for kind, key in (("sell", "jual"), ("buy", "bb")):
        # 'sell' = harga jual Antam ke konsumen, 'buy' = buyback
        try:
            raw = json.loads(get(f"https://www.logammulia.com/data-base-price/gold/{kind}", timeout=40))
            rows = raw.get("data", raw) if isinstance(raw, dict) else raw
            for r in rows:
                if isinstance(r, (list, tuple)) and len(r) >= 2:
                    t, v = r[0], r[1]
                elif isinstance(r, dict):
                    t = r.get("x") or r.get("date") or r.get("tanggal") or r.get("created_at")
                    v = r.get("y") or r.get("price") or r.get("harga")
                else:
                    continue
                if isinstance(t, (int, float)):
                    d = datetime.fromtimestamp(t / (1000 if t > 1e11 else 1), WIB).strftime("%Y-%m-%d")
                else:
                    d = str(t)[:10]
                v = float(v)
                if 100_000 < v < 20_000_000:
                    out.setdefault(d, {})[key] = int(v)
        except Exception as e:  # noqa: BLE001
            log(f"logammulia riwayat {kind} gagal:", e)
    return out


def spot_now():
    j = json.loads(get("https://api.gold-api.com/price/XAU"))
    return float(j["price"])


def kurs_now():
    j = json.loads(get("https://open.er-api.com/v6/latest/USD"))
    return float(j["rates"]["IDR"])


# ======================= HARGA DOMESTIK =======================
def html_to_lines(raw):
    raw = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</(div|p|tr|td|th|li|h\d|span|section)>", "\n", raw)
    raw = re.sub(r"<[^>]+>", " ", raw)
    raw = html.unescape(raw)
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in raw.split("\n")]
    return [ln for ln in lines if ln]


RP_RE = re.compile(r"Rp\.?\s?[\d\.\,]{5,}")
ONE_GRAM_RE = re.compile(r"^(1|1[\.,]0+)(\s?(gr|gram|g))?$", re.I)


def parse_vendor_1g(lines, vendor_names, stop_names):
    """Cari blok vendor (judul persis), lalu baris berat 1 gram + dua harga (jual, buyback)."""
    vset = {v.upper() for v in vendor_names}
    sset = {s.upper() for s in stop_names}
    starts = [i for i, ln in enumerate(lines)
              if ln.upper() in vset or ln.upper().replace("HARGA ", "") in vset]
    for st in starts:
        for i in range(st + 1, min(st + 400, len(lines))):
            up = lines[i].upper().replace("HARGA ", "")
            if up in sset and up not in vset:
                break
            if ONE_GRAM_RE.match(lines[i]):
                prices = []
                for j in range(i + 1, min(i + 8, len(lines))):
                    m = RP_RE.search(lines[j])
                    if m:
                        prices.append(rp_to_int(m.group()))
                    elif ONE_GRAM_RE.match(lines[j]) or re.match(r"^\d+([\.,]\d+)?$", lines[j]):
                        break
                    if len(prices) == 2:
                        break
                if prices and 500_000 < prices[0] < 20_000_000:
                    jual = prices[0]
                    bb = prices[1] if len(prices) > 1 and 400_000 < prices[1] <= jual else None
                    return jual, bb
    return None, None


GALERI24_VENDORS = ["GALERI 24", "DINAR G24", "BABY GALERI 24", "ANTAM", "UBS", "ANTAM MULIA RETRO",
                    "ANTAM NON PEGADAIAN", "UBS DISNEY", "UBS ELSA", "UBS ANNA", "UBS MICKEY FULLBODY",
                    "UBS MICKEY FULL BODY", "LOTUS ARCHI", "UBS HELLO KITTY", "BABY SERIES TUMBUHAN",
                    "BABY SERIES INVESTASI", "BATIK SERIES", "LOTUS ARCHI GIFT"]


def galeri24_prices():
    lines = html_to_lines(get("https://galeri24.co.id/harga-emas"))
    antam = parse_vendor_1g(lines, ["ANTAM"], GALERI24_VENDORS)
    ubs = parse_vendor_1g(lines, ["UBS"], GALERI24_VENDORS)
    g24 = parse_vendor_1g(lines, ["GALERI 24", "GALERI24"], GALERI24_VENDORS)
    return antam, ubs, g24


def logammulia_antam():
    lines = html_to_lines(get("https://www.logammulia.com/id/harga-emas-hari-ini"))
    jual = None
    for i, ln in enumerate(lines):
        if re.match(r"^1\s?gr(am)?$", ln, re.I):
            for j in range(i + 1, min(i + 4, len(lines))):
                v = rp_to_int(lines[j])
                if v and 500_000 < v < 20_000_000:
                    jual = v
                    break
            if jual:
                break
    bb = None
    try:
        t = " ".join(html_to_lines(get("https://www.logammulia.com/id/sell/gold")))
        m = re.search(r"(?i)buyback[^R]{0,60}(Rp\.?\s?[\d\.\,]{7,})", t)
        if m:
            bb = rp_to_int(m.group(1))
    except Exception as e:  # noqa: BLE001
        log("logammulia buyback gagal:", e)
    return jual, bb


# ======================= ANALISA =======================
def sma(vals, n):
    return sum(vals[-n:]) / n if len(vals) >= n else None


def rsi(vals, n=14):
    if len(vals) < n + 1:
        return None
    gains, losses = 0.0, 0.0
    for a, b in zip(vals[-n - 1:-1], vals[-n:]):
        d = b - a
        gains += max(d, 0)
        losses += max(-d, 0)
    if losses == 0:
        return 100.0
    rs = (gains / n) / (losses / n)
    return 100 - 100 / (1 + rs)


def lin(x, x0, x1, p0, p1):
    """Interpolasi linear x dari [x0,x1] ke [p0,p1], dibatasi."""
    if x is None:
        return None
    t = (x - x0) / (x1 - x0)
    t = max(0.0, min(1.0, t))
    return p0 + t * (p1 - p0)


def interpolate_daily(world):
    """Isi celah antar titik (mis. data bulanan) menjadi deret hari kerja dengan interpolasi linear."""
    out = []
    for a, b in zip(world, world[1:]):
        da = datetime.strptime(a["d"], "%Y-%m-%d")
        db = datetime.strptime(b["d"], "%Y-%m-%d")
        out.append({"d": a["d"], "idr_g": a["idr_g"], "usd": a["usd"]})
        days = (db - da).days
        for k in range(1, days):
            dk = da + timedelta(days=k)
            if dk.weekday() < 5:
                t = k / days
                out.append({"d": dk.strftime("%Y-%m-%d"), "idr_g": a["idr_g"] + t * (b["idr_g"] - a["idr_g"]),
                            "usd": a["usd"] + t * (b["usd"] - a["usd"])})
    if world:
        out.append({"d": world[-1]["d"], "idr_g": world[-1]["idr_g"], "usd": world[-1]["usd"]})
    return out


def median(v):
    s = sorted(v)
    n = len(s)
    return None if n == 0 else (s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2)


# ======================= KALENDER & SUKU BUNGA =======================
# Jadwal resmi The Fed (revisi 12 Nov 2026) & BLS (CPI), dalam WIB.
EVENTS = [
    ("2026-12-10 02:00", "fomc"), ("2026-12-10 20:30", "cpi"), ("2027-01-13 20:30", "cpi"), ("2027-01-28 02:00", "fomc"),
    ("2027-02-11 20:30", "cpi"), ("2027-03-10 20:30", "cpi"), ("2027-03-18 01:00", "fomc"), ("2027-04-13 19:30", "cpi"),
    ("2027-04-29 01:00", "fomc"), ("2027-05-12 19:30", "cpi"), ("2027-06-10 01:00", "fomc"), ("2027-06-10 19:30", "cpi"),
    ("2027-07-14 19:30", "cpi"), ("2027-07-29 01:00", "fomc"), ("2027-08-11 19:30", "cpi"), ("2027-09-14 19:30", "cpi"),
    ("2027-09-16 01:00", "fomc"), ("2027-10-14 19:30", "cpi"), ("2027-11-10 20:30", "cpi"), ("2027-11-18 02:00", "fomc"),
    ("2027-12-10 20:30", "cpi"), ("2028-01-27 02:00", "fomc"),
]
EV_NAME = {"fomc": "Keputusan suku bunga The Fed", "cpi": "Rilis data inflasi AS (CPI)"}


def deposit_rates_worldbank():
    """Suku bunga deposito rata-rata Indonesia per tahun (%), World Bank FR.INR.DPST. {tahun: persen}"""
    j = json.loads(get("https://api.worldbank.org/v2/country/IDN/indicator/FR.INR.DPST?format=json&per_page=100", timeout=40))
    out = {}
    for r in (j[1] if isinstance(j, list) and len(j) > 1 and j[1] else []):
        if r.get("value") is not None:
            out[str(r["date"])] = round(float(r["value"]), 3)
    if len(out) < 5:
        raise RuntimeError("data sedikit")
    return out


# ======================= UJI HISTORIS SINYAL =======================
def backtest(series, approx=False):
    """Uji historis model yang sama (valuasi x tren) untuk setiap hari, lalu ukur hasil 6 & 12 bulan kemudian.
    Catatan: komponen premi & risiko peristiwa tidak tersedia untuk masa lalu (premi dianggap netral)."""
    idr = [w["idr_g"] for w in series]
    dts = [w["d"] for w in series]
    n = len(idr)
    if n < 300:
        return None
    zones, scores = [None] * n, [None] * n
    for i in range(252, n):
        value, trend, _, _, _ = score_core(idr, i, approx)
        z, _, _ = zone_from(value, trend, approx)
        zones[i] = {"BELI": "B", "CICIL": "C", "TUNGGU": "T", "MAHAL": "M"}[z]
        lo_b, hi_b = BANDS[z]
        scores[i] = max(lo_b, min(hi_b, round(0.6 * value + 0.4 * trend)))
    H6, H12 = 126, 252

    def fwd(i, h):
        return round(idr[i + h] / idr[i] - 1, 4) if i + h < n else None

    sig = []
    for i in range(253, n):
        if zones[i] in ("B", "C") and zones[i - 1] != zones[i] and not (zones[i] == "C" and zones[i - 1] == "B"):
            sig.append({"d": dts[i], "z": zones[i], "s": scores[i], "p": round(idr[i]), "r6": fwd(i, H6), "r12": fwd(i, H12)})

    def stats(lst, key):
        v = [x[key] for x in lst if x[key] is not None]
        if not v:
            return None
        return {"n": len(v), "avg": round(sum(v) / len(v), 4), "win": round(sum(1 for x in v if x > 0) / len(v), 3)}

    all_idx = list(range(252, n))
    base = {k: stats([{"r": fwd(i, h)} for i in all_idx], "r") for k, h in (("r6", H6), ("r12", H12))}
    by_zone = {}
    for zc in ("B", "C", "T", "M"):
        lst = [{"r6": fwd(i, H6), "r12": fwd(i, H12)} for i in all_idx if zones[i] == zc]
        by_zone[zc] = {"days": len(lst), "r6": stats(lst, "r6"), "r12": stats(lst, "r12")}
    monthly, seen = [], set()
    for i in range(252, n):
        ym = dts[i][:7]
        if ym not in seen:
            seen.add(ym)
            monthly.append([dts[i], zones[i], scores[i]])
    return {"model": 2, "approx": approx, "from": dts[252], "signals": sig[-80:], "n_signals": len(sig),
            "stats": {"base": base, "zone": by_zone}, "monthly": monthly}


# ======================= ANALISA v2 (valuasi x tren x risiko) =======================
def zone_from(value, trend, approx=False):
    """Matriks keputusan ala investor: VALUASI (murah?) x TREN (sudah berbalik?).
    Kembalikan (zona, kunci_saran, dibatasi?)"""
    if value >= 65:
        if trend >= 50:
            return "BELI", "BELI", False                 # murah + tren sudah berbalik naik
        if trend >= 25:
            return "CICIL", "CICIL_bottom", False        # murah, sedang membentuk dasar
        return "CICIL", "CICIL_cap", True                # murah tapi masih jatuh -> cicil kecil
    if value >= 45:
        return ("CICIL", "CICIL_trend", False) if trend >= 60 else ("TUNGGU", "TUNGGU", False)
    if value >= 25:
        return "TUNGGU", "TUNGGU", False
    return "MAHAL", "MAHAL", False


BANDS = {"BELI": (70, 100), "CICIL": (50, 69), "TUNGGU": (35, 49), "MAHAL": (0, 34)}


def score_core(idr, i, approx=False, prem_pts=5.0):
    """Hitung skor VALUASI & TREN pada indeks i dari deret harian Rp/gram (minimal 253 titik)."""
    last = idr[i]
    ma20 = sum(idr[i - 19:i + 1]) / 20
    ma20_prev = sum(idr[i - 29:i - 9]) / 20                     # rata-rata 20 hari, 10 hari lalu
    ma50 = sum(idr[i - 49:i + 1]) / 50
    ma200 = sum(idr[i - 199:i + 1]) / 200
    win = idr[i - 251:i + 1]
    hi, lo = max(win), min(win)
    lo_idx = i - 251 + win.index(lo)
    days_low = i - lo_idx                                        # hari sejak titik terendah 1 tahun
    pos = (last - lo) / (hi - lo) if hi > lo else 0.5
    dd = last / hi - 1
    dev200 = last / ma200 - 1
    chg20 = last / idr[i - 20] - 1
    slope = ma20 / ma20_prev - 1
    if approx:
        r = None
    else:
        g = l_ = 0.0
        for a, b in zip(idr[i - 14:i], idr[i - 13:i + 1]):
            d = b - a
            g += max(d, 0)
            l_ += max(-d, 0)
        r = 100.0 if l_ == 0 else 100 - 100 / (1 + g / l_)
    # ---- VALUASI 0-100: seberapa murah dibanding sejarahnya ----
    v = [("v_dev200", lin(dev200, 0.15, -0.15, 0, 35), 35),
         ("v_pos52", lin(pos, 1.0, 0.0, 0, 25), 25),
         ("v_dd52", lin(-dd, 0.0, 0.30, 0, 20), 20),
         ("v_rsi", lin(r, 70, 30, 0, 10) if r is not None else 5, 10),
         ("v_prem", prem_pts, 10)]
    # ---- TREN 0-100: apakah penurunan sudah berhenti / berbalik ----
    t = [("t_ma50", 25 if last > ma50 else (12 if last > ma50 * 0.97 else 0), 25),
         ("t_slope", 20 if slope > 0.005 else (10 if slope > -0.005 else 0), 20),
         ("t_low", 25 if days_low >= 20 else 15 if days_low >= 10 else 8 if days_low >= 5 else 0, 25),
         ("t_mom", 15 if chg20 > 0.02 else (8 if chg20 > -0.02 else 0), 15),
         ("t_ma200", 15 if last > ma200 else 0, 15)]
    value = sum(p for _, p, _ in v)
    trend = sum(p for _, p, _ in t)
    ind = {"last": last, "ma20": ma20, "ma50": ma50, "ma200": ma200, "rsi": r, "hi": hi, "lo": lo, "pos": pos,
           "dd": dd, "dev200": dev200, "chg20": chg20, "chg5": last / idr[i - 5] - 1, "slope": slope, "days_low": days_low}
    return value, trend, v, t, ind


def vol_ratio(idr, i):
    """Volatilitas 20 hari dibanding median volatilitas 1 tahun (>1,5 = pasar sedang bergejolak)."""
    def vol(a, b):
        rets = [idr[k] / idr[k - 1] - 1 for k in range(a, b)]
        m = sum(rets) / len(rets)
        return (sum((x - m) ** 2 for x in rets) / len(rets)) ** 0.5
    if i < 272:
        return None
    now_v = vol(i - 19, i + 1)
    hist = [vol(k - 19, k + 1) for k in range(i - 251, i + 1, 10)]
    med = median(hist)
    return now_v / med if med else None


def next_event(now):
    for ts, kind in EVENTS:
        ev = datetime.strptime(ts, "%Y-%m-%d %H:%M").replace(tzinfo=WIB)
        if ev > now:
            return ev, kind
    return None, None


def analyze(world, domestic, approx=False, now=None):
    """world: deret hari kerja {d, idr_g, usd}. Analisa 3 lapis: VALUASI, TREN, RISIKO."""
    now = now or datetime.now(WIB)
    idr = [w["idr_g"] for w in world]
    usd = [w["usd"] for w in world]
    i = len(idr) - 1
    # premi UBS dibanding biasanya (komponen valuasi)
    prem_hist = [x["ubs_jual"] / x["idr_g"] - 1 for x in domestic if x.get("ubs_jual") and x.get("idr_g")]
    prem_now = prem_hist[-1] if prem_hist else None
    med = median(prem_hist[:-1]) if len(prem_hist) >= 10 else None
    prem_pts = lin(prem_now - med, 0.03, -0.03, 0, 10) if med is not None else 5.0
    value, trend, vc, tc, I = score_core(idr, i, approx, prem_pts)
    zone, akey, capped = zone_from(value, trend, approx)
    rc = []
    # ---- RISIKO: peristiwa besar & volatilitas ----
    ev, kind = next_event(now)
    ev_days = (ev - now).total_seconds() / 86400 if ev else None
    risk = {"event": None, "vol": None}
    if ev and ev_days <= 7:
        risk["event"] = {"kind": kind, "at": ev.strftime("%Y-%m-%d %H:%M"), "days": round(ev_days, 1)}
        rc.append({"k": "r_event", "v": round(ev_days, 1), "e": kind, "at": ev.strftime("%Y-%m-%d %H:%M")})
        if zone == "BELI":
            zone, akey, capped = "CICIL", "CICIL_event", True
    vr = vol_ratio(idr, i)
    if vr is not None:
        risk["vol"] = round(vr, 2)
        if vr > 1.5:
            rc.append({"k": "r_vol", "v": round(vr, 2)})
    # ---- skor gabungan untuk jarum (dijepit ke rentang zona) ----
    raw = round(0.6 * value + 0.4 * trend)
    lo_b, hi_b = BANDS[zone]
    score_eff = max(lo_b, min(hi_b, raw))
    # ---- alasan terstruktur ----
    rc.append({"k": "dev200", "v": round(I["dev200"], 4)})
    if I["rsi"] is not None:
        rc.append({"k": "rsi", "v": round(I["rsi"], 1)})
    rc.append({"k": "pos52", "v": round(I["pos"], 3)})
    rc.append({"k": "dd52", "v": round(I["dd"], 4)})
    rc.append({"k": "t_low", "v": I["days_low"]})
    rc.append({"k": "t_ma50", "v": round(I["last"] / I["ma50"] - 1, 4)})
    rc.append({"k": "t_slope", "v": round(I["slope"], 4)})
    if prem_now is not None:
        rc.append({"k": "prem_cmp", "v": round(prem_now, 4), "m": round(med, 4)} if med is not None
                  else {"k": "prem", "v": round(prem_now, 4)})
    spread = None
    if domestic and domestic[-1].get("ubs_jual") and domestic[-1].get("ubs_bb"):
        spread = 1 - domestic[-1]["ubs_bb"] / domestic[-1]["ubs_jual"]
        rc.append({"k": "spread", "v": round(spread, 4)})
    if approx:
        rc.append({"k": "approx"})
    names = {"v_dev200": "Harga vs rata-rata 200 hari", "v_pos52": "Posisi di rentang 1 tahun", "v_dd52": "Diskon dari puncak 1 tahun",
             "v_rsi": "RSI 14 hari (jenuh jual)", "v_prem": "Premi UBS vs biasanya", "t_ma50": "Harga vs rata-rata 50 hari",
             "t_slope": "Arah rata-rata 20 hari", "t_low": "Tidak lagi membuat titik terendah baru",
             "t_mom": "Momentum 1 bulan", "t_ma200": "Di atas rata-rata 200 hari (tren besar)"}
    comps = [{"name": names[k], "pts": round(p, 1), "max": m, "key": k, "g": k[0]} for k, p, m in vc + tc]
    labels = {"BELI": "Waktu yang baik untuk beli", "CICIL": "Cicil bertahap", "TUNGGU": "Tunggu dulu", "MAHAL": "Mahal — tunda"}
    advice = {"BELI": "Harga murah dan tren sudah berbalik. Layak beli — tetap bagi 2–3 tahap.",
              "CICIL_bottom": "Harga murah dan mulai membentuk dasar. Beli setengah, sisanya saat tren terkonfirmasi naik.",
              "CICIL_cap": "Harga murah tapi masih terus jatuh. Beli kecil saja (±1/3), tunggu harga berhenti membuat titik terendah baru.",
              "CICIL_event": "Secara harga layak beli, tapi ada peristiwa besar dalam ≤7 hari. Beli sebagian sekarang, sisanya setelah peristiwa.",
              "CICIL_trend": "Harga wajar dengan tren naik kuat. Cicil bertahap.",
              "TUNGGU": "Harga belum cukup murah. Tunggu koreksi atau beli rutin kecil saja.",
              "MAHAL": "Harga mahal dibanding trennya. Tunda pembelian."}
    reasons = [advice[akey]]
    return {
        "model": 2, "score": raw, "score_eff": score_eff, "capped": capped, "approx": approx, "akey": akey,
        "value": round(value), "trend": round(trend), "risk": risk,
        "zone": zone, "label": labels[zone], "advice": advice[akey],
        "components": comps, "reasons": reasons, "rcodes": rc,
        "ind": {
            "last_idr_g": round(I["last"]), "last_usd": usd[-1],
            "ma20": round(I["ma20"]), "ma50": round(I["ma50"]), "ma200": round(I["ma200"]),
            "rsi": I["rsi"] and round(I["rsi"], 1), "hi52": round(I["hi"]), "lo52": round(I["lo"]), "pos52": round(I["pos"], 3),
            "dd52": round(I["dd"], 4), "chg5": round(I["chg5"], 4), "chg30": round(I["chg20"], 4),
            "dev200": round(I["dev200"], 4), "premium_ubs": prem_now and round(prem_now, 4),
            "spread_ubs": spread and round(spread, 4), "days_low": I["days_low"], "slope20": round(I["slope"], 4),
            "vol_ratio": risk["vol"],
        },
    }


# ======================= NOTIFIKASI =======================
def ntfy(title, msg, priority="default", tags="moneybag"):
    if not NTFY_TOPIC:
        log("NTFY_TOPIC kosong, notif dilewati:", title)
        return
    # Header HTTP harus latin-1: judul dibuat ASCII
    safe_title = title.encode("ascii", "ignore").decode()
    req = urllib.request.Request(f"https://ntfy.sh/{NTFY_TOPIC}", data=msg.encode("utf-8"), method="POST",
                                 headers={"Title": safe_title, "Priority": priority, "Tags": tags})
    try:
        urllib.request.urlopen(req, timeout=20).read()
        log("notif terkirim:", title)
    except Exception as e:  # noqa: BLE001
        log("notif gagal:", e)


def rp(v):
    return "-" if v is None else "Rp" + f"{int(round(v)):,}".replace(",", ".")


# ======================= UTAMA =======================
def load():
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"world": [], "domestic": [], "state": {}}


def main():
    data = load()
    now = datetime.now(WIB)
    today = now.strftime("%Y-%m-%d")
    status = {}

    # ---- dunia (riwayat sampai 20 tahun) ----
    world_map = {w["d"]: w for w in data.get("world", [])}
    xau, fx, src = {}, {}, []
    errs = []
    start_year = now.year - 20
    for name, fn in (("Bundesbank", bundesbank_history), ("LBMA", lbma_history),
                     ("yahoo", lambda: yahoo_history("GC=F")), ("freegoldapi", freegold_history),
                     ("stooq", lambda: stooq_history("xauusd"))):
        try:
            xau = fn(); src.append(f"emas harian {name} ({len(xau)} hari)"); break
        except Exception as e:  # noqa: BLE001
            log(name, "emas gagal:", e); errs.append(f"{name}: {str(e)[:60]}")
    for name, fn in (("ECB/frankfurter", lambda: frankfurter_idr(start_year)),
                     ("yahoo", lambda: yahoo_history("IDR=X")), ("stooq", lambda: stooq_history("usdidr"))):
        try:
            fx = fn(); src.append(f"kurs harian {name}"); break
        except Exception as e:  # noqa: BLE001
            log(name, "kurs gagal:", e); errs.append(f"{name}: {str(e)[:60]}")
    if errs:
        status["error sumber"] = "; ".join(errs)
    if today not in xau:
        try:
            xau[today] = spot_now(); src.append("spot hari ini gold-api")
        except Exception as e:  # noqa: BLE001
            log("gold-api gagal:", e)
    if today not in fx:
        try:
            fx[today] = kurs_now()
        except Exception as e:  # noqa: BLE001
            log("kurs hari ini gagal:", e)
    # cadangan riwayat panjang bulanan bila riwayat harian belum panjang
    n_daily = len([w for w in world_map.values() if not w.get("m")]) + len(xau)
    has_monthly = any(w.get("m") for w in world_map.values())
    if n_daily < 2000 and not has_monthly:
        try:
            mg = monthly_gold_history()
            mf = monthly_idr_history()
            last = None
            first_daily = min([d for d in xau] + [d for d, w in world_map.items() if not w.get("m")] or [today])
            for d in sorted(set(mg) | set(mf)):
                if d in mf:
                    last = mf[d]
                if d in mg and last and d < first_daily:
                    world_map[d] = {"d": d, "usd": round(mg[d], 2), "kurs": round(last, 1),
                                    "idr_g": round(mg[d] * last / GRAM_PER_OZ), "m": 1}
            src.append("riwayat bulanan datahub+FRED")
        except Exception as e:  # noqa: BLE001
            log("riwayat bulanan gagal:", e)
    last_fx = None
    for d in sorted(set(xau) | set(fx)):
        if d in fx:
            last_fx = fx[d]
        if d in xau and last_fx:
            world_map[d] = {"d": d, "usd": round(xau[d], 2), "kurs": round(last_fx, 1),
                            "idr_g": round(xau[d] * last_fx / GRAM_PER_OZ)}
    # hapus titik bulanan yang sudah tertutup data harian
    first_daily = min([d for d, w in world_map.items() if not w.get("m")] or [today])
    cutoff = (now - timedelta(days=365 * 20 + 5)).strftime("%Y-%m-%d")
    world = [world_map[d] for d in sorted(world_map)
             if d >= cutoff and not (world_map[d].get("m") and d >= first_daily)]
    status["dunia"] = ", ".join(src) if src else "gagal semua sumber"
    # tandai titik yang jarang (jarak ke titik berikutnya > 6 hari) sebagai data bulanan/jarang
    for a_, b_ in zip(world, world[1:]):
        gap = (datetime.strptime(b_["d"], "%Y-%m-%d") - datetime.strptime(a_["d"], "%Y-%m-%d")).days
        if gap > 6:
            a_["m"] = 1
        else:
            a_.pop("m", None)
    dense = 0
    for w_ in reversed(world):
        if w_.get("m"):
            break
        dense += 1
    status["data harian"] = f"{dense} hari terakhir harian, {len(world) - dense} titik lama bulanan/jarang"
    world_daily = [w for w in world if not w.get("m")]
    series_full = interpolate_daily(world) if len(world) >= 2 else []

    # ---- riwayat Antam (grafik Logam Mulia) ----
    antam_hist = {x["d"]: x for x in data.get("antam_hist", [])}
    if len(antam_hist) < 200 or now.weekday() == 0:
        lm = logammulia_history()
        for d, v in lm.items():
            vals = [x for x in (v.get("jual"), v.get("bb")) if x]
            if not vals:
                continue
            e = {"d": d, "jual": max(vals)}
            if len(vals) == 2:
                e["bb"] = min(vals)
            antam_hist[d] = e
        status["riwayat antam"] = f"{len(antam_hist)} hari" + ("" if lm else " (sumber Logam Mulia tidak tersedia)")
    antam_hist = [antam_hist[d] for d in sorted(antam_hist)]

    # ---- domestik ----
    manual = {k: rp_to_int(os.environ.get(k.upper(), "")) for k in
              ("antam_jual", "antam_bb", "ubs_jual", "ubs_bb")}
    rec = {"d": today}
    try:
        (aj, ab), (uj, ub), (gj, gb) = galeri24_prices()
        rec.update(antam_jual=aj, antam_bb=ab, ubs_jual=uj, ubs_bb=ub, g24_jual=gj, g24_bb=gb)
        status["galeri24"] = (f"antam={'ok' if aj else 'tidak ketemu'}, ubs={'ok' if uj else 'tidak ketemu'}, "
                              f"galeri24={'ok' if gj else 'tidak ketemu'}")
    except Exception as e:  # noqa: BLE001
        status["galeri24"] = f"gagal: {e}"
    if not rec.get("antam_jual"):
        try:
            aj, ab = logammulia_antam()
            rec["antam_jual"], rec["antam_bb"] = aj, ab or rec.get("antam_bb")
            status["logammulia"] = "ok" if aj else "tidak ketemu"
        except Exception as e:  # noqa: BLE001
            status["logammulia"] = f"gagal: {e}"
    for k, v in manual.items():
        if v:
            rec[k] = v
            status["manual"] = "harga manual dipakai"
    if world:
        rec["idr_g"] = world[-1]["idr_g"]

    domestic = [x for x in data.get("domestic", []) if x["d"] != today]
    prev_today = next((x for x in data.get("domestic", []) if x["d"] == today), {})
    for k in ("antam_jual", "antam_bb", "ubs_jual", "ubs_bb", "g24_jual", "g24_bb"):
        if not rec.get(k) and prev_today.get(k):
            rec[k] = prev_today[k]          # jangan timpa data pagi dengan kosong
    if any(rec.get(k) for k in ("antam_jual", "ubs_jual")):
        domestic.append(rec)
    domestic.sort(key=lambda x: x["d"])

    if len(series_full) >= 260:
        # deret hari kerja lengkap (titik jarang diinterpolasi); RSI/momentum harian hanya bila ekor datanya harian
        analysis = analyze(series_full, domestic, approx=dense < 30)
    else:
        log("Data dunia belum cukup untuk analisa.")
        analysis = None

    # ---- suku bunga deposito historis (untuk perbandingan emas vs deposito) ----
    dep = data.get("dep_rates") or {}
    if not dep or now.weekday() == 0:
        try:
            dep = deposit_rates_worldbank()
            status["bunga deposito"] = f"World Bank ({min(dep)}–{max(dep)})"
        except Exception as e:  # noqa: BLE001
            status["bunga deposito"] = f"gagal: {str(e)[:50]}" + (" (pakai data lama)" if dep else "")

    # ---- uji historis ----
    try:
        # pakai seluruh riwayat 20 tahun; mode 'approx' (tanpa RSI) bila sebagian besar data lama bukan harian
        bt = backtest(series_full, approx=dense < 2000) if len(series_full) >= 300 else None
    except Exception as e:  # noqa: BLE001
        log("backtest gagal:", e)
        bt = data.get("backtest")

    # ---- notifikasi ----
    state = data.get("state", {})
    sig_log = data.get("signal_log", [])
    if analysis and (not sig_log or sig_log[-1]["z"] != analysis["zone"]):
        sig_log.append({"d": today, "z": analysis["zone"], "s": analysis["score_eff"]})
    sig_log = sig_log[-200:]
    last_dom = domestic[-1] if domestic else {}
    if analysis:
        z = analysis["zone"]
        prev_zone = state.get("zone")
        body = (f"{analysis['label']} (skor {analysis['score_eff']}/100)\n"
                f"UBS 1g: {rp(last_dom.get('ubs_jual'))} | Antam 1g: {rp(last_dom.get('antam_jual'))}\n"
                f"Dunia: ${analysis['ind']['last_usd']:,.0f}/oz\n{analysis['advice']}")
        if z == "BELI" and prev_zone != "BELI":
            ntfy("SINYAL BELI EMAS", body, priority="high", tags="rotating_light,moneybag")
            state["beli_day"] = today
        elif z == "BELI" and state.get("beli_day") != today and now.hour < 13:
            # pengingat tiap pagi selama rekomendasi masih BELI
            ntfy("Emas masih zona BELI", body, priority="high", tags="moneybag")
            state["beli_day"] = today
        elif NOTIF_CICIL and z == "CICIL" and prev_zone in ("TUNGGU", "MAHAL", None):
            ntfy("Emas masuk zona CICIL", body, tags="chart_with_downwards_trend")
        elif DAILY_DIGEST and state.get("digest_day") != today and now.hour < 13:
            ntfy(f"Emas hari ini: {z}", body, priority="low", tags="sunrise")
            state["digest_day"] = today
        state["zone"] = z
    for key, target, name in (("ubs_jual", TARGET_UBS, "UBS"), ("antam_jual", TARGET_ANTAM, "Antam")):
        v = last_dom.get(key)
        flag = f"target_{key}"
        if target and v and v <= target and state.get(flag) != today:
            ntfy(f"Harga {name} sampai target", f"{name} 1 gram {rp(v)} (target {rp(target)}).",
                 priority="high", tags="dart")
            state[flag] = today
    # harga turun tajam dalam sehari
    if NOTIF_DROP and state.get("drop_day") != today:
        msgs = []
        if len(world_daily) >= 2 and world_daily[-1]["d"] >= (now - timedelta(days=4)).strftime("%Y-%m-%d"):
            ch = world_daily[-1]["idr_g"] / world_daily[-2]["idr_g"] - 1
            if ch <= -DROP_PCT / 100:
                msgs.append(f"Emas dunia {ch*100:.1f}% ({rp(world_daily[-1]['idr_g'])}/g)")
        prevd = next((x for x in reversed(domestic) if x["d"] != today and x.get("ubs_jual")), None)
        if prevd and last_dom.get("d") == today:
            for key, nm in (("ubs_jual", "UBS"), ("antam_jual", "Antam")):
                if last_dom.get(key) and prevd.get(key):
                    ch = last_dom[key] / prevd[key] - 1
                    if ch <= -DROP_PCT / 100:
                        msgs.append(f"{nm} {ch*100:.1f}% → {rp(last_dom[key])}")
        if msgs:
            zone_txt = f"\nRekomendasi: {analysis['zone']} (skor {analysis['score_eff']})" if analysis else ""
            ntfy("Harga emas turun tajam", "\n".join(msgs) + zone_txt, priority="high", tags="chart_with_downwards_trend")
            state["drop_day"] = today
    # pengingat H-1 peristiwa penting (dikirim pada run sore sehari sebelumnya)
    if NOTIF_EVENT and now.hour >= 13:
        sent = set(state.get("ev_sent", []))
        for ts, kind in EVENTS:
            ev = datetime.strptime(ts, "%Y-%m-%d %H:%M").replace(tzinfo=WIB)
            if ts not in sent and timedelta(hours=0) < ev - now <= timedelta(hours=30):
                dd = (ev.date() - now.date()).days
                when = {0: "Hari ini", 1: "Besok"}.get(dd, ev.strftime("%d/%m")) + (" dini hari" if ev.hour < 6 else "")
                ntfy(f"{when}: {EV_NAME[kind]}",
                     f"{EV_NAME[kind]} pukul {ev.strftime('%H.%M')} WIB ({ev.strftime('%d/%m')}). "
                     "Harga emas sering bergejolak di sekitar jadwal ini — pertimbangkan menunda pembelian besar sampai setelahnya.",
                     tags="calendar")
                sent.add(ts)
        state["ev_sent"] = sorted(sent)[-30:]
    # ringkasan mingguan (Senin pagi)
    week = now.strftime("%G-W%V")
    if NOTIF_WEEKLY and now.weekday() == 0 and now.hour < 13 and state.get("week_sent") != week and analysis:
        wk_ago = (now - timedelta(days=7)).strftime("%Y-%m-%d")
        prev_w = next((x for x in reversed(domestic) if x["d"] <= wk_ago and x.get("ubs_jual")), None)
        lines = [f"Rekomendasi: {analysis['zone']} (skor {analysis['score_eff']}/100)"]
        for key, nm in (("ubs_jual", "UBS"), ("antam_jual", "Antam")):
            if last_dom.get(key):
                c = f" ({(last_dom[key]/prev_w[key]-1)*100:+.1f}% sepekan)" if prev_w and prev_w.get(key) else ""
                lines.append(f"{nm} 1g: {rp(last_dom[key])}{c}")
        wk_end = now + timedelta(days=7)
        for ts, kind in EVENTS:
            ev = datetime.strptime(ts, "%Y-%m-%d %H:%M").replace(tzinfo=WIB)
            if now < ev <= wk_end:
                hari = ["Sen", "Sel", "Rab", "Kam", "Jum", "Sab", "Min"][ev.weekday()]
                lines.append(f"📅 {hari} {ev.strftime('%d/%m %H.%M')} WIB: {EV_NAME[kind]}")
        ntfy("Ringkasan emas minggu ini", "\n".join(lines), priority="low", tags="newspaper")
        state["week_sent"] = week
    if not last_dom.get("ubs_jual") and state.get("warn_day") != today and now.hour >= 13:
        ntfy("Harga UBS gagal diambil", "Masukkan manual lewat Actions > Run workflow, atau cek log.",
             priority="low", tags="warning")
        state["warn_day"] = today

    out = {
        "version": VERSION,
        "updated": now.isoformat(timespec="minutes"),
        "status": status,
        "analysis": analysis,
        "world": world,
        "domestic": domestic,
        "antam_hist": antam_hist,
        "backtest": bt,
        "dep_rates": dep,
        "signal_log": sig_log,
        "state": state,
        "settings": {"target_ubs": TARGET_UBS, "target_antam": TARGET_ANTAM, "buy_score": BUY_SCORE},
    }
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    log("Selesai.", json.dumps(status, ensure_ascii=False))
    if analysis:
        log("Zona:", analysis["zone"], "skor", analysis["score_eff"])


if __name__ == "__main__":
    main()
