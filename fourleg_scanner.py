#!/usr/bin/env python3
"""
四腿外站票掃描器（台灣為基地：華航 CI／長榮 BR／星宇 JX）

票型（connect_pair = "12" 時）：
  腿1 外站 -> TPE   ┐ 轉機（間隔 connect_gap_days 天，預設隔天）
  腿2 TPE  -> 目的地 ┘
  腿3 目的地 -> TPE
  腿4 TPE  -> 外站   （日期由 fixed_leg_date 指定）

connect_pair = "34" 時：腿3、腿4 轉機，腿1 日期由 fixed_leg_date 指定。

比對三種價格：
  1. 四腿票總價（同一航司、每段直飛，Google Flights 多城市組合價）
  2. 基準價：同航司 TPE <-> 目的地 直飛來回
  3. 定位成本：TPE <-> 外站 來回（任何航司，可手動覆寫）
淨成本（四腿＋定位）比同航司直飛便宜超過門檻的組合會被標記為 DEAL。

底層：faster-flights（GitHub: jamexhuang/flights，fast-flights 的分支）。
四腿多城市價格走 Google 的 GetShoppingResults 查價介面，並補上航司與直飛條件。
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import re
import statistics
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta
from pathlib import Path

try:
    from fast_flights import FlightQuery, Passengers, create_query, get_flights, fetch_flights_html
except ImportError:  # 讓 --help 在未安裝時也能看
    FlightQuery = Passengers = create_query = get_flights = fetch_flights_html = None

try:  # faster-flights（GitHub: jamexhuang/flights）提供的多城市 RPC 查價
    import urllib.parse as _up
    import fast_flights.shopping as _shop
    from fast_flights.fetcher import _build_default_client
    _orig_encode = _shop._encode_shopping_request

    def _encode_with_filters(legs, tokens, seat_val=1, **kw):
        """原版不帶航司與轉機條件，這裡補上：index 3 = 轉機（1 = 直飛），index 4 = 航司。"""
        s = _orig_encode(legs, tokens, seat_val, **kw)
        req = json.loads(_up.unquote(s[len("f.req="):-1]))
        inner = json.loads(req[1])
        for seg, leg in zip(inner[1][13], legs):
            ms = getattr(leg, "max_stops", None)
            if ms is not None:
                seg[3] = ms + 1  # Google：1 = 直飛，2 = 最多經停一次
            if getattr(leg, "airlines", None):
                seg[4] = list(leg.airlines)
        req[1] = json.dumps(inner, separators=(",", ":"))
        return f"f.req={_up.quote(json.dumps(req, separators=(',', ':')))}&"

    _shop._encode_shopping_request = _encode_with_filters

    # 原版每次查價都先 GET 一次 Google Flights 首頁「暖機」，這裡改成每條連線只暖機一次
    _orig_warmup = _shop._warmup_shopping_session
    _WARMED: set[int] = set()

    def _warmup_once(client, **kw):
        if id(client) in _WARMED:
            return
        _WARMED.add(id(client))
        _orig_warmup(client, **kw)

    _shop._warmup_shopping_session = _warmup_once
    import inspect as _inspect
    _NO_RETRY = ({"max_retries": 0}
                 if "max_retries" in _inspect.signature(_shop.fetch_shopping_results).parameters else {})
    HAS_RPC = True
except Exception:
    HAS_RPC = False

try:  # fli（PyPI: flights）提供 Google Flights 月曆價格：一次查詢拿到整段日期的來回價
    from fli.models import (Airline as _FAl, Airport as _FAp, DateSearchFilters as _FDSF,
                            FlightSegment as _FSeg, MaxStops as _FMS, PassengerInfo as _FPI,
                            SeatType as _FST, TripType as _FTT)
    from fli.search.dates import SearchDates as _FSD
    HAS_CAL = True
except Exception:
    HAS_CAL = False

HERE = Path(__file__).resolve().parent
LAST_STOP_REASON = ""
CAL_CHECKS: list[str] = []  # 月曆價與逐日實查的對照
STATS = {"calls": 0, "skipped_nonstop": 0, "coarse_saved": 0}  # 本次掃描統計（網頁即時顯示）
ACTIVITY: list[dict] = []  # 即時查詢紀錄（網頁「查詢紀錄」顯示）
_ACT_SEQ = 0


def log_activity(kind: str, text: str, result: str = "", secs: float | None = None):
    """kind：ok 查到價格／none 查無／block 限流／cache 用快取／info 進度說明。"""
    global _ACT_SEQ
    _ACT_SEQ += 1
    ACTIVITY.append({"i": _ACT_SEQ, "t": datetime.now().strftime("%H:%M:%S"), "k": kind,
                     "text": text, "r": result, "s": round(secs, 1) if secs is not None else None})
    del ACTIVITY[:-400]
    print(f"{ACTIVITY[-1]['t']} {text} {result}" + (f"（{secs:.1f} 秒）" if secs is not None else ""))
BLOCKED_MSG = ("Google 目前不回應你的網路（掃描一開始就連續被擋或逾時），看起來是被暫時封鎖。"
               "已自動停止，請 24 小時後再試，期間不要重複掃描，否則封鎖時間會被拉長。")
FIXED_PICKS: list[str] = []  # 固定段日期的彈性挑選結果


class NetworkBlocked(Exception):
    """掃描一開始就連續被擋或逾時。"""


class ComboLimit(Exception):
    """達到每次掃描組數上限。"""


class RateLimited(Exception):
    """Google 連續拒絕查詢（限流），停止掃描避免被擋更久。"""


def looks_blocked(text: str = "", err: str = "") -> bool:
    t = (text or "")[:4000].lower() + " " + (err or "").lower()
    keys = ["excessive load", "unusual traffic", "/sorry/", "429", "too many requests",
            '"wrb.fr",null,null,null,null,[', "timeout", "timed out", "error sending request",
            "connection reset", "connection error"]
    return any(k in t for k in keys)
AIRLINE_NAMES = {"BR": "長榮", "CI": "華航", "JX": "星宇"}


# ---------- 資料結構 ----------

@dataclass
class Leg:
    date: date
    origin: str
    dest: str


@dataclass
class Row:
    airline: str
    outstation: str
    dest: str
    depart: str             # 腿2 出發日（實際出國玩的那天）
    legs: str
    fourleg_price: int | None = None
    baseline_price: int | None = None
    oneway_price: int | None = None      # 參考：台灣 -> 外站 單程（不限航司）
    net_cost: int | None = None
    saving: int | None = None
    saving_pct: float | None = None
    fare_saving: int | None = None       # 直飛來回 − 四腿（不含定位）
    fare_saving_pct: float | None = None
    deal: str = ""
    fourleg_url: str = ""
    baseline_url: str = ""
    oneway_url: str = ""
    note: str = ""
    via: str = ""                        # 非空代表直飛查不到，改用經停航班（"四腿"、"直飛基準"）


# ---------- 價格解析（容錯版，多城市頁面也能用） ----------

def _dig(obj, *idx):
    for i in idx:
        if not isinstance(obj, list) or len(obj) <= i:
            return None
        obj = obj[i]
    return obj


def extract_prices(html_text: str) -> list[int]:
    """從 Google Flights 頁面的 ds:1 資料只取出價格，欄位缺漏時跳過不報錯。"""
    m = re.search(r'<script[^>]*class="ds:1"[^>]*>(.*?)</script>', html_text, re.S)
    if not m:
        return []
    js = m.group(1)
    if "data:" not in js:
        return []
    data = js.split("data:", 1)[1].rsplit(",", 1)[0]
    if data.endswith("errorHasStatus: true"):
        return []
    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        return []
    prices = []
    for section in (2, 3):  # 2 = 最佳航班, 3 = 其他航班
        items = _dig(payload, section, 0)
        if not isinstance(items, list):
            continue
        for k in items:
            p = _dig(k, 1, 0, 1)
            if isinstance(p, (int, float)) and p > 0:
                prices.append(int(p))
    return prices


# ---------- 查詢與快取 ----------

class Fetcher:
    def __init__(self, cfg: dict, cache_path: Path, offline: bool = False):
        self.delay = cfg.get("request_delay_sec", 4)
        self.ttl = cfg.get("cache_hours", 24) * 3600
        self.cache_path = cache_path
        self.offline = offline
        self.cache = {}
        if cache_path.exists():
            try:
                self.cache = json.loads(cache_path.read_text(encoding="utf-8"))
            except Exception:
                self.cache = {}
        self._last = 0.0
        self.blocked_streak = 0
        self.ok_responses = 0  # 本次掃描 Google 有正常回應的次數（含查無航班）
        self.early_stop = int(cfg.get("early_stop_after", 2))
        self.net_calls = 0  # 實際連網查詢次數（快取不算）
        self.block_limit = int(cfg.get("block_stop_after", 5))
        self.backoff = int(cfg.get("block_backoff_sec") or 15)

    def _wait(self):
        import random
        self.net_calls += 1
        STATS["calls"] += 1
        wait = self.delay * random.uniform(1.0, 1.5) - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.time()

    def _mark(self, blocked: bool):
        self.blocked_streak = self.blocked_streak + 1 if blocked else 0
        if not blocked:
            self.ok_responses += 1
        if blocked and self.ok_responses == 0 and self.early_stop and self.blocked_streak >= self.early_stop:
            # 一開始就連續被擋：網路多半已被 Google 暫時封鎖，繼續試只會延長封鎖
            raise NetworkBlocked()
        if self.blocked_streak >= self.block_limit:
            raise RateLimited()
        if blocked:
            # 被擋：丟掉被標記的連線，等一下再重新建立（30 秒、60 秒…逐次拉長，最多 3 分鐘）
            self._client = None
            wait = min(180, self.backoff * self.blocked_streak)
            log_activity("block", f"Google 限流，{wait} 秒後換新連線重試（連續第 {self.blocked_streak} 次，"
                                  f"{self.block_limit} 次就自動停止）")
            time.sleep(wait)

    def calendar_rt(self, al: str, hub: str, dest: str, start: date, end: date,
                    trip: int, stops: int, adults: int, currency: str) -> dict[str, int]:
        """台北來回月曆價：每 61 天一次查詢，回傳 {出發日: 價格}。查不到回傳空字典。"""
        return self.calendar(al, hub, dest, start, end, trip, stops, adults, currency)

    def calendar(self, al: str | None, origin: str, dest: str, start: date, end: date,
                 trip: int | None, stops: int, adults: int, currency: str) -> dict[str, int]:
        """Google Flights 月曆價。trip 為天數＝來回，None＝單程；al 為 None＝不限航司。
        每 61 天一次查詢，回傳 {出發日: 價格}；查不到回傳空字典。"""
        if self.offline or not HAS_CAL or end < start:
            return {}
        key = hashlib.sha1(f"cal|{al}|{origin}|{dest}|{start}|{end}|{trip}|{stops}|{adults}|{currency}".encode()).hexdigest()
        name = (f"月曆 {AIRLINE_NAMES.get(al, al) if al else '不限航司'} "
                f"{origin}{'⇄' if trip else '→'}{dest}")
        hit = self.cache.get(key)
        if hit and hit.get("p") and time.time() - hit["t"] < self.ttl:
            log_activity("cache", f"{name} {start:%m/%d}–{end:%m/%d}", f"{len(hit['p'])} 天有價格")
            return hit["p"]
        try:
            a_o, a_d = _FAp[origin], _FAp[dest]
            a_al = [_FAl[al]] if al else None
        except KeyError:
            return {}
        if getattr(self, "_cal", None) is None:
            self._cal = _FSD()
            c = self._cal.client
            c.post = type(c).post.__wrapped__.__get__(c)  # 關掉套件內建的自動重試，由這裡統一控管
        out: dict[str, int] = {}
        s = start
        while s <= end:
            e = min(s + timedelta(days=60), end)
            segs = [_FSeg(departure_airport=[[a_o, 0]], arrival_airport=[[a_d, 0]], travel_date=s.isoformat())]
            if trip:
                segs.append(_FSeg(departure_airport=[[a_d, 0]], arrival_airport=[[a_o, 0]],
                                  travel_date=(s + timedelta(days=trip)).isoformat()))
            blocked = False
            try:
                flt = _FDSF(
                    trip_type=_FTT.ROUND_TRIP if trip else _FTT.ONE_WAY,
                    passenger_info=_FPI(adults=adults), flight_segments=segs,
                    stops=_FMS.NON_STOP if stops == 0 else _FMS.ONE_STOP_OR_FEWER,
                    seat_type=_FST.ECONOMY, airlines=a_al,
                    from_date=s.isoformat(), to_date=e.isoformat(), duration=trip or None)
            except Exception as ex:  # 日期超出 Google 可查範圍等
                print(f"月曆查價略過（{origin}-{dest}）：{str(ex)[:100]}")
                return out
            self._wait()
            t0, before, err = time.time(), len(out), ""
            try:
                for x in self._cal._search_chunk(flt, currency=currency) or []:
                    out[x.date[0].strftime("%Y-%m-%d")] = int(x.price)
            except Exception as ex:
                blocked = looks_blocked(err=str(ex))
                err = "Google 限流" if blocked else f"失敗：{str(ex)[:60]}"
            got = len(out) - before
            log_activity("ok" if got else ("block" if blocked else "none"), f"{name} {s:%m/%d}–{e:%m/%d}",
                         f"{got} 天有價格" if got else (err or "沒有價格"), time.time() - t0)
            self._mark(blocked)
            s = e + timedelta(days=1)
        if out:
            self.cache[key] = {"t": time.time(), "p": out}
        return out

    def save(self):
        self.cache_path.write_text(json.dumps(self.cache, ensure_ascii=False), encoding="utf-8")

    def _debug(self, key: str, url: str, text: str):
        dbg = self.cache_path.parent / "debug"
        dbg.mkdir(exist_ok=True)
        (dbg / f"{key[:12]}.html").write_text(url + "\n" + (text or ""), encoding="utf-8")

    def cheapest_multicity(self, fq, url: str, adults: int, currency: str, label: str = "四腿票") -> tuple[int | None, str]:
        """四腿票：走 Google 多城市查價 RPC，回傳組合總價。"""
        if self.offline:
            return None, "dry-run"
        if not HAS_RPC:
            return None, "需安裝 faster-flights"
        key = hashlib.sha1(("rpc|" + url).encode()).hexdigest()
        hit = self.cache.get(key)
        if hit and hit.get("p") and time.time() - hit["t"] < self.ttl:
            log_activity("cache", label, f"{hit['p']:,}")
            return hit["p"], "cache"
        self._wait()
        t0 = time.time()
        blocked = False
        try:
            if getattr(self, "_client", None) is None:
                self._client = _build_default_client()
            _, price, raw, found = _shop.fetch_shopping_results(
                client=self._client, legs=fq, tokens=[],
                language="zh-TW", currency=currency, seat="economy",
                passenger_counts=(adults, 0, 0, 0),
                **_NO_RETRY,  # 原版預設失敗或查無時自動重試 3 次（一次查詢變 4 次請求），重試統一由這裡控管
            )
            prices = [f.price for f in (found or []) if getattr(f, "price", 0) and f.price > 0]
            if price and price > 0:
                prices.append(price)
            p = min(prices) if prices else None
            note = ""
            if not p:
                diag = getattr(getattr(found, "diagnostics", None), "status", "") or ""
                blocked = diag == "blocked" or looks_blocked(raw)
                note = "Google 限流" if blocked else "查無航班"
                self._debug(key, url, raw)
        except Exception as e:
            blocked = looks_blocked(err=str(e))
            p, note = None, ("Google 限流" if blocked else f"{type(e).__name__}: {e}"[:80])
            if "timeout" in str(e).lower() or "timed out" in str(e).lower():
                note = "逾時（可能限流）"
        if p:
            self.cache[key] = {"t": time.time(), "p": p}
        log_activity("ok" if p else ("block" if blocked else "none"), label,
                     f"{p:,}" if p else note, time.time() - t0)
        self._mark(blocked)
        return p, note

    def cheapest(self, query, url: str, label: str = "來回") -> tuple[int | None, str]:
        """回傳 (最低價, 備註)。"""
        if self.offline:
            return None, "dry-run"
        key = hashlib.sha1(url.encode()).hexdigest()
        hit = self.cache.get(key)
        if hit and hit.get("p") and time.time() - hit["t"] < self.ttl:
            log_activity("cache", label, f"{hit['p']:,}")
            return hit["p"], "cache"
        self._wait()
        t0 = time.time()
        blocked = False
        try:
            page = fetch_flights_html(query)
            prices = extract_prices(page)
            p = min(prices) if prices else None
            note = ""
            if not p:  # 留下原始頁面以便除錯
                blocked = looks_blocked(page) or 'class="ds:1"' not in page
                note = "Google 限流" if blocked else "查無航班"
                self._debug(key, url, page)
        except Exception as e:  # 網路錯誤、版面改版
            blocked = looks_blocked(err=str(e))
            p, note = None, ("Google 限流" if blocked else f"{type(e).__name__}: {e}"[:80])
            if "timeout" in str(e).lower() or "timed out" in str(e).lower():
                note = "逾時（可能限流）"
        if p:
            self.cache[key] = {"t": time.time(), "p": p}
        log_activity("ok" if p else ("block" if blocked else "none"), label,
                     f"{p:,}" if p else note, time.time() - t0)
        self._mark(blocked)
        return p, note


def make_fq(legs: list[Leg], airline: str | None, max_stops=0):
    """max_stops 可為整數（每段相同）或清單（每段各自設定）。"""
    stops = max_stops if isinstance(max_stops, (list, tuple)) else [max_stops] * len(legs)
    return [
        FlightQuery(
            date=l.date.isoformat(),
            from_airport=l.origin,
            to_airport=l.dest,
            max_stops=ms,
            airlines=[airline] if airline else None,
        )
        for l, ms in zip(legs, stops)
    ]


def make_query(legs: list[Leg], trip: str, cfg: dict, airline: str | None, max_stops=0):
    fq = make_fq(legs, airline, max_stops)
    return create_query(
        flights=fq,
        trip=trip,
        seat="economy",
        passengers=Passengers(adults=cfg.get("adults", 1)),
        currency=cfg.get("currency", "TWD"),
        language="zh-TW",
    )


# ---------- 航點資料 ----------

def load_routes() -> dict:
    """routes.json：每家航空飛哪些點。讀不到就不過濾。"""
    try:
        return json.loads((HERE / "routes.json").read_text(encoding="utf-8")).get("airlines", {})
    except Exception:
        return {}


def served(routes: dict, al: str, code: str, as_outstation: bool) -> bool:
    a = routes.get(al)
    if not a:
        return True
    pool = a.get("asia", []) if as_outstation else a.get("asia", []) + a.get("longhaul", [])
    return code in pool


# ---------- 行程組合 ----------

def date_range(cfg: dict):
    w = cfg["date_window"]
    d = date.fromisoformat(w["start"])
    end = date.fromisoformat(w["end"])
    step = timedelta(days=max(1, w.get("step_days", 7)))
    while d <= end:
        yield d
        d += step


_REGION_OF: dict | None = None


def region_of(code: str) -> str:
    global _REGION_OF
    if _REGION_OF is None:
        try:
            data = json.loads((HERE / "routes.json").read_text(encoding="utf-8"))
            _REGION_OF = {c: g["id"] for g in data.get("regions", []) for c in g["codes"]}
        except Exception:
            _REGION_OF = {}
    return _REGION_OF.get(code, "")


def is_longhaul(code: str) -> bool:
    return region_of(code) in ("eu", "na", "oc")


def arrival_days(cfg: dict, dest: str) -> int:
    """從目的地飛回台北，起飛後第幾天抵達（歐洲、北美預設隔天）。"""
    table = {"eu": 1, "na": 1, "oc": 0, **cfg.get("longhaul_arrival_days", {})}
    return int(table.get(region_of(dest), 0))


def build_legs(cfg: dict, out: str, dest: str, d: date, extra_days: int = 0) -> list[Leg] | None:
    hub = cfg.get("hub", "TPE")
    trip = timedelta(days=cfg.get("trip_days", 5))
    gap = timedelta(days=cfg.get("connect_gap_days", 1))  # 轉機段間隔（預設隔天，從抵達日起算）
    arrive = timedelta(days=arrival_days(cfg, dest) + extra_days)
    fixed = cfg.get("fixed_leg_date")
    fixed = date.fromisoformat(fixed) if fixed else None
    if cfg.get("connect_pair", "12") == "12":
        # 前面轉：腿1、腿2 轉機；腿4（台北 -> 外站）用自訂日期
        leg4 = fixed or (d + trip + timedelta(days=cfg.get("stopover_days", 60)))
        legs = [Leg(d - gap, out, hub), Leg(d, hub, dest),
                Leg(d + trip, dest, hub), Leg(leg4, hub, out)]
        if legs[3].date < legs[2].date + arrive:
            return None
    else:  # "34"
        # 後面轉：腿3、腿4 轉機；腿1（外站 -> 台北）用自訂日期
        leg1 = fixed or (d - timedelta(days=cfg.get("stopover_days", 60)))
        legs = [Leg(leg1, out, hub), Leg(d, hub, dest),
                Leg(d + trip, dest, hub), Leg(d + trip + arrive + gap, hub, out)]
        if legs[0].date > legs[1].date:
            return None
    if legs[0].date <= date.today():
        return None
    return legs


def legs_text(legs: list[Leg]) -> str:
    return " / ".join(f"{l.origin}-{l.dest} {l.date:%m/%d}" for l in legs)


# ---------- 歷史常態價 ----------

def load_history(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def append_history(path: Path, rows: list[Row]):
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["scanned_at", "airline", "outstation", "dest", "depart", "fourleg_price"])
        stamp = datetime.now().isoformat(timespec="minutes")
        for r in rows:
            if r.fourleg_price:
                w.writerow([stamp, r.airline, r.outstation, r.dest, r.depart, r.fourleg_price])


# ---------- 計算 ----------

def evaluate(r: Row, rules: dict, history: list[dict]):
    if r.fourleg_price is None:
        return
    if r.baseline_price:
        r.fare_saving = r.baseline_price - r.fourleg_price
        r.fare_saving_pct = round(r.fare_saving / r.baseline_price * 100, 1)
    # 只跟同航司台北來回比
    if r.fare_saving_pct is not None and r.fare_saving_pct >= rules.get("min_saving_pct", 15):
        r.deal = "DEAL"


# ---------- 主流程 ----------

def scan(cfg: dict, offline: bool, only_airline: str | None, only_out: str | None,
         on_row=None, stop_event=None, on_update=None) -> list[Row]:
    global LAST_STOP_REASON
    LAST_STOP_REASON = ""
    FIXED_PICKS.clear()
    CAL_CHECKS.clear()
    STATS.update(calls=0, skipped_nonstop=0, coarse_saved=0)
    ACTIVITY.clear()
    out_dir = HERE / "output"
    out_dir.mkdir(exist_ok=True)
    fetcher = Fetcher(cfg, out_dir / "cache.json", offline=offline)
    history = load_history(out_dir / "history.csv")
    rules = cfg.get("deal_rules", {})
    hub = cfg.get("hub", "TPE")
    adults, cur = cfg.get("adults", 1), cfg.get("currency", "TWD")
    fallback = cfg.get("allow_stops_fallback", True)

    airlines = [only_airline] if only_airline else cfg["airlines"]
    outs = [only_out] if only_out else cfg["outstations"]
    dates = list(date_range(cfg))
    routes = load_routes() if cfg.get("use_routes", True) else {}
    pairs = [(dest, al, out)
             for dest in cfg["destinations"] for al in airlines for out in outs
             if out != dest and served(routes, al, out, True) and served(routes, al, dest, False)]
    skipped = len(airlines) * len(outs) * len(cfg["destinations"]) - len(pairs)

    # 快速模式：先隔一個日期掃（粗掃），再補查最便宜價位旁邊的日期（細查）
    coarse = bool(cfg.get("coarse_fine", True)) and len(dates) >= 6
    stage1 = dates[::2] if coarse else dates
    total = len(pairs) * len(stage1)
    log_activity("info", f"開始掃描：共 {len(pairs) * len(dates)} 組行程"
                 + (f"，快速模式先查 {total} 組" if coarse else "")
                 + (f"（已略過 {skipped * len(dates)} 組航空公司沒飛的組合）" if skipped else ""))

    rows: list[Row] = []
    baseline_seen: dict[str, tuple] = {}
    n = 0
    max_c = int(cfg.get("max_combos_per_scan", 20) or 0)  # 每次最多實際查幾組（快取命中不算）
    state = {"fresh": 0, "c0": 0}

    # ---- 月曆價：台北來回、台灣→外站單程，都是一次查詢涵蓋整段日期 ----
    # 台北來回比價方式：top＝四腿全部掃完，只拿最便宜的幾個日期查來回（預設）；all＝每個日期都查
    top_mode = cfg.get("baseline_mode", "top") == "top"
    top_n = max(1, int(cfg.get("baseline_top_n", 5)))
    use_cal = HAS_CAL and cfg.get("baseline_calendar", False)
    # Google Flights 大約只能查 11 個月內的航班，超過的組合直接略過，不浪費查詢、也不誤判成限流
    horizon = date.today() + timedelta(days=int(cfg.get("max_days_ahead", 330)))
    cal_cache: dict[tuple, dict | None] = {}
    cal_checked: set = set()

    cal_full: dict[tuple, bool] = {}

    def cal_prices(al: str, dest: str, stops: int) -> dict | None:
        """回傳 {出發日: 價格}；None 代表月曆不可用，要改回逐日查。"""
        if not use_cal or not dates:
            return None
        k = ("rt", al, dest, stops)
        if k not in cal_cache:
            in_range = [d for d in dates if d + timedelta(days=int(cfg.get("trip_days", 5))) <= horizon]
            got = fetcher.calendar(al, hub, dest, dates[0], min(dates[-1], horizon), int(cfg.get("trip_days", 5)),
                                   stops, adults, cur) if in_range else {}
            cal_cache[k] = got or None
            # 月曆只有部分日期有價格（遠期常見）：缺的日期改回逐日查，也不拿來判斷有沒有直飛
            cover = sum(d.isoformat() in got for d in in_range) / len(in_range) if got and in_range else 0
            cal_full[k] = cover >= 0.6
            if got and not cal_full[k]:
                log_activity("info", f"月曆 {AIRLINE_NAMES.get(al, al)} {hub}⇄{dest} 只有 {cover:.0%} 的日期有價格，"
                                     "缺的日期改回逐日查")
        return cal_cache[k]

    def cal_ok(al: str, dest: str, stops: int) -> bool:
        return cal_full.get(("rt", al, dest, stops), False)

    def oneway_prices(out: str, lo: date, hi: date) -> dict | None:
        if not use_cal:
            return None
        k = ("ow", out)
        if k not in cal_cache:
            lo = max(lo, date.today() + timedelta(days=1))
            got = fetcher.calendar(None, hub, out, lo, hi, None, 0, adults, cur)
            cal_cache[k] = got or None
        return cal_cache[k]

    # ---- 固定段（不轉機的第 1 或第 4 段）前後彈性 ----
    # 先用第一個出國日，查固定段日期前後 N 天的四腿票價，挑最便宜的那天，
    # 之後同一組航司／外站／目的地都用這天
    flex = int(cfg.get("fixed_flex_days", 2) or 0)
    fixed_pick: dict[tuple, dict] = {}

    def fixed_for(dest: str, al: str, out: str, d: date) -> dict:
        key = (dest, al, out)
        if key in fixed_pick:
            return fixed_pick[key]
        base = cfg.get("fixed_leg_date")
        if not flex or not base:
            fixed_pick[key] = cfg
            return cfg
        b = date.fromisoformat(base)
        best = None
        for off in range(-flex, flex + 1):
            c2 = {**cfg, "fixed_leg_date": (b + timedelta(days=off)).isoformat()}
            lg = build_legs(c2, out, dest, d)
            if not lg or max(l.date for l in lg) > horizon:
                continue
            q = make_query(lg, "multi-city", cfg, al)
            p, _ = fetcher.cheapest_multicity(make_fq(lg, al), q.url(), adults, cur,
                                              f"固定段試算 {AIRLINE_NAMES.get(al, al)} {out}→{dest} 固定段 {b + timedelta(days=off):%m/%d}")
            if p and (best is None or p < best[0]):
                best = (p, c2)
        if best is None:
            return cfg  # 這天查不到，下一個出國日再試
        fixed_pick[key] = best[1]
        nd = date.fromisoformat(best[1]["fixed_leg_date"])
        FIXED_PICKS.append(f"{AIRLINE_NAMES.get(al, al)} {out}→{dest} {nd:%m/%d}")
        log_activity("info", f"固定段日期：{AIRLINE_NAMES.get(al, al)} {out}→{dest} 前後 {flex} 天最便宜是 {nd:%m/%d}（{best[0]:,}）")
        return best[1]

    # ---- 單一組行程 ----
    def one(d: date, dest: str, al: str, out: str) -> Row | None:
        pcfg = fixed_for(dest, al, out, d)
        legs = build_legs(pcfg, out, dest, d)
        if not legs:
            return None
        r = Row(al, out, dest, d.isoformat(), legs_text(legs))
        tag = f"{AIRLINE_NAMES.get(al, al)} {out}→{dest} {d:%m/%d}"
        if max(l.date for l in legs) > horizon:
            r.note = "超過可查範圍"
            log_activity("none", f"四腿 {tag}", f"最後一段晚於 {horizon:%Y/%m/%d}，Google 查不到，略過")
            evaluate(r, rules, history)
            return r
        via = []
        q4 = make_query(legs, "multi-city", cfg, al)
        r.fourleg_url, note = q4.url(), ""

        # 月曆上這天沒有直飛來回 → 多半沒有直飛班次，直接查經停，省一次查詢
        cal0 = cal_prices(al, dest, 0)
        no_nonstop = fallback and cal0 is not None and cal_ok(al, dest, 0) and d.isoformat() not in cal0
        if no_nonstop:
            STATS["skipped_nonstop"] += 1
            log_activity("info", f"四腿 {tag}：月曆上這天沒有直飛，直接查經停")
        else:
            r.fourleg_price, note = fetcher.cheapest_multicity(make_fq(legs, al), r.fourleg_url, adults, cur,
                                                               f"四腿 {tag} 直飛")
        if r.fourleg_price is None and fallback:
            # 長程線常沒有每天直飛：外站段維持直飛，台北↔目的地兩段放寬為最多經停一次
            tries = [(0, [0, 1, 1, 0])]
            if is_longhaul(dest) and cfg.get("connect_pair", "12") == "34":
                tries.append((1, [0, 1, 1, 0]))  # 回程隔兩天才抵達台北的班次
            for extra, stops in tries:
                lg = build_legs(pcfg, out, dest, d, extra) if extra else legs
                if not lg or max(l.date for l in lg) > horizon:
                    continue
                q4s = make_query(lg, "multi-city", cfg, al, stops)
                p4s, n4s = fetcher.cheapest_multicity(make_fq(lg, al, stops), q4s.url(), adults, cur,
                                                      f"四腿 {tag} 經停")
                if p4s:
                    r.fourleg_price, r.fourleg_url, note = p4s, q4s.url(), ""
                    r.legs = legs_text(lg)
                    via.append("四腿")
                    break
                note = note or n4s

        r.note = note
        r._legs, r._via = legs, via
        r.via = "、".join(via)
        if top_mode:
            evaluate(r, rules, history)  # 先只看四腿價，台北來回留到最後挑最便宜的日期再查
        else:
            add_baseline(r)
        return r

    def add_baseline(r: Row):
        d, dest, al, out = date.fromisoformat(r.depart), r.dest, r.airline, r.outstation
        legs, via = r._legs, r._via
        cal0 = cal_prices(al, dest, 0)
        # 台北來回（同航司，同一段日期）：先看直飛；
        # 直飛查不到，或四腿用了經停航班，就一併看經停來回，取較低者，讓比較基準一致
        bkey = f"{al}|{dest}|{d}"
        if bkey not in baseline_seen:
            qb = make_query([legs[1], legs[2]], "round-trip", cfg, al)
            if cal0 is None or (d.isoformat() not in cal0 and not cal_ok(al, dest, 0)):
                # 月曆查不到、關閉，或這天剛好沒有月曆價：改回逐日查
                bp = fetcher.cheapest(qb, qb.url(), f"來回 {AIRLINE_NAMES.get(al, al)} {hub}⇄{dest} {d:%m/%d}")[0]
            else:
                bp = cal0.get(d.isoformat())
                ck = (al, dest)
                if bp and cfg.get("calendar_check", True) and ck not in cal_checked:
                    # 每組航司＋目的地，用第一個有價格的日期實查一次，對照月曆價準不準
                    cal_checked.add(ck)
                    real = fetcher.cheapest(qb, qb.url(), f"來回實查核對 {AIRLINE_NAMES.get(al, al)} {hub}⇄{dest} {d:%m/%d}")[0]
                    if real:
                        diff = (bp - real) / real * 100
                        CAL_CHECKS.append(f"{AIRLINE_NAMES.get(al, al)} {dest} {d:%m/%d} 月曆 {bp:,}／實查 {real:,}（{diff:+.0f}%）")
                        log_activity("info", "月曆價核對：" + CAL_CHECKS[-1])
                        bp = real
            baseline_seen[bkey] = (qb.url(), bp)
        b_url, b_price = baseline_seen[bkey]
        b_via = False
        if fallback and (b_price is None or "四腿" in via):
            skey = bkey + "|stops"
            if skey not in baseline_seen:
                qbs = make_query([legs[1], legs[2]], "round-trip", cfg, al, [1, 1])
                cal_s = cal_prices(al, dest, 1)
                sp = cal_s.get(d.isoformat()) if cal_s is not None else None
                if sp is None and (cal_s is None or not cal_ok(al, dest, 1)):
                    sp = fetcher.cheapest(qbs, qbs.url(), f"來回經停 {AIRLINE_NAMES.get(al, al)} {hub}⇄{dest} {d:%m/%d}")[0]
                baseline_seen[skey] = (qbs.url(), sp)
            s_url, s_price = baseline_seen[skey]
            if s_price and (b_price is None or s_price < b_price):
                b_url, b_price, b_via = s_url, s_price, True
        r.baseline_url, r.baseline_price = b_url, b_price
        if b_via:
            via.append("台北來回")
        r.via = "、".join(via)

        # 參考：台灣 -> 外站 單程（不限航司、直飛），日期為第 1 段前一天；月曆一次查完整段
        if cfg.get("oneway_ref", True):
            od = legs[0].date - timedelta(days=1)
            qo = make_query([Leg(od, hub, out)], "one-way", cfg, None, 0)
            r.oneway_url = qo.url()
            gap = int(cfg.get("connect_gap_days", 1))
            if cfg.get("connect_pair", "12") == "12":
                lo, hi = dates[0] - timedelta(days=gap + 1), dates[-1] - timedelta(days=gap + 1)
            else:
                fb = date.fromisoformat(cfg.get("fixed_leg_date") or od.isoformat())
                lo, hi = fb - timedelta(days=flex + 1), fb + timedelta(days=flex)
            ow = oneway_prices(out, min(lo, od), max(hi, od))
            okey = f"ow|{out}|{od}"
            if ow is not None:
                r.oneway_price = ow.get(od.isoformat())
            else:
                if okey not in baseline_seen:
                    baseline_seen[okey] = (r.oneway_url, fetcher.cheapest(qo, qo.url(), f"單程 {hub}→{out} {od:%m/%d}")[0])
                r.oneway_price = baseline_seen[okey][1]

        evaluate(r, rules, history)

    def run(d: date, dest: str, al: str, out: str):
        nonlocal n
        if stop_event is not None and stop_event.is_set():
            raise KeyboardInterrupt
        if fetcher.net_calls > state["c0"]:
            state["fresh"] += 1
        if max_c and state["fresh"] >= max_c:
            raise ComboLimit
        state["c0"] = fetcher.net_calls
        n += 1
        r = one(d, dest, al, out)
        if r is not None:
            rows.append(r)
            price = f"{r.fourleg_price:,}" if r.fourleg_price else "-"
            print(f"[{n}/{total}] {AIRLINE_NAMES.get(al, al)} {out}->{dest} {d} 四腿 {price} {r.deal}")
        if on_row:
            on_row(r, n, total)

    def baseline_stage():
        nonlocal n, total
        groups: dict[tuple, list[Row]] = {}
        for r in rows:
            if r.fourleg_price and getattr(r, "_legs", None):
                groups.setdefault((r.airline, r.dest), []).append(r)
        pick: list[Row] = []
        for (al, dest), rs in groups.items():
            top_dates: list[str] = []
            for r in sorted(rs, key=lambda x: (x.fourleg_price, x.depart)):
                if r.depart not in top_dates:
                    if len(top_dates) >= top_n:
                        continue
                    top_dates.append(r.depart)
                pick.append(r)
        if not pick:
            return
        total += len(pick)
        log_activity("info", f"四腿查完，挑出各航司＋目的地最便宜的 {top_n} 個出國日（共 {len(pick)} 組），"
                             "跟同航司台北來回比價")
        for r in sorted(pick, key=lambda x: (x.dest, x.airline, x.depart, x.outstation)):
            if stop_event is not None and stop_event.is_set():
                raise KeyboardInterrupt
            n += 1
            add_baseline(r)
            if on_update:
                on_update(r, n, total)

    def refine_dates() -> list[tuple]:
        """細查：每組航司／外站／目的地，補查四腿最便宜價位（5% 內）旁邊、還沒查過的日期。"""
        done = {(r.dest, r.airline, r.outstation, r.depart): r.fourleg_price for r in rows}
        extra = []
        for dest, al, out in pairs:
            got = {d: done.get((dest, al, out, d.isoformat())) for d in stage1}
            prices = [p for p in got.values() if p]
            if not prices:
                continue
            best = min(prices)
            for i, d in enumerate(dates):
                if d in got:
                    continue
                nb = [got.get(x) for x in (dates[i - 1] if i else None, dates[i + 1] if i + 1 < len(dates) else None)]
                if any(p and p <= best * 1.05 for p in nb):
                    extra.append((d, dest, al, out))
                else:
                    STATS["coarse_saved"] += 1
        return extra

    try:
        for d in stage1:
            for dest, al, out in pairs:
                run(d, dest, al, out)
        if coarse:
            todo = refine_dates()
            total += len(todo)
            log_activity("info", f"粗掃完成，細查：補查最便宜價位附近的 {len(todo)} 組日期"
                         + (f"，略過 {STATS['coarse_saved']} 組偏貴的日期" if STATS["coarse_saved"] else ""))
            for d, dest, al, out in sorted(todo):
                run(d, dest, al, out)
    except KeyboardInterrupt:
        log_activity("info", "已停止，保留目前結果")
    except ComboLimit:
        LAST_STOP_REASON = (f"本次已查 {max_c} 組（每次上限）。"
                            "再按一次開始掃描，查過的會用快取，從下一組接著查。")
        log_activity("info", LAST_STOP_REASON)
    except RateLimited:
        LAST_STOP_REASON = "Google 暫時限流，已自動停止。建議 30–60 分鐘後再掃，並縮小範圍。"
        log_activity("block", LAST_STOP_REASON)
    except NetworkBlocked:
        LAST_STOP_REASON = BLOCKED_MSG
        log_activity("block", LAST_STOP_REASON)
    if top_mode and not (LAST_STOP_REASON.startswith("Google") or LAST_STOP_REASON == BLOCKED_MSG or (stop_event is not None and stop_event.is_set())):
        try:
            baseline_stage()
        except KeyboardInterrupt:
            log_activity("info", "已停止，保留目前結果")
        except RateLimited:
            LAST_STOP_REASON = "Google 暫時限流，已自動停止。建議 30–60 分鐘後再掃，並縮小範圍。"
            log_activity("block", LAST_STOP_REASON)
        except NetworkBlocked:
            LAST_STOP_REASON = BLOCKED_MSG
            log_activity("block", LAST_STOP_REASON)
    fetcher.save()

    if not offline:
        append_history(out_dir / "history.csv", rows)
    return rows


# ---------- 輸出 ----------

def fmt(v, pct=False):
    if v is None:
        return "-"
    return f"{v:+.1f}%" if pct else f"{v:,}"


def write_outputs(rows: list[Row], cfg: dict, offline: bool):
    out_dir = HERE / "output"
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    csv_path = out_dir / f"results-{stamp}.csv"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(asdict(rows[0]).keys()) if rows else ["empty"])
        w.writeheader()
        for r in rows:
            w.writerow(asdict(r))

    rows_sorted = sorted(rows, key=lambda r: (r.fare_saving is None, -(r.fare_saving or 0)))
    trs = []
    for r in rows_sorted:
        cls = "deal" if r.deal else ""
        links = f'<a href="{html.escape(r.fourleg_url)}" target="_blank">四腿</a>'
        if r.baseline_url:
            links += f' · <a href="{html.escape(r.baseline_url)}" target="_blank">來回</a>'
        if r.oneway_url:
            links += f' · <a href="{html.escape(r.oneway_url)}" target="_blank">單程</a>'
        trs.append(
            f'<tr class="{cls}"><td>{AIRLINE_NAMES.get(r.airline, r.airline)} {r.airline}</td>'
            f"<td>{r.outstation}</td><td>{r.dest}</td><td>{r.depart}</td>"
            f'<td class="legs">{html.escape(r.legs)}</td>'
            f'<td class="n">{fmt(r.fourleg_price)}</td>'
            f'<td class="n">{fmt(r.baseline_price)}</td>'
            f'<td class="n">{fmt(r.fare_saving)}</td><td class="n">{fmt(r.fare_saving_pct, True)}</td>'
            f'<td class="n">{fmt(r.oneway_price)}</td>'
            f"<td>{r.deal}{(' 經停：' + r.via) if r.via else ''}</td><td>{links}</td></tr>"
        )
    deals = sum(1 for r in rows if r.deal)
    mode = "僅產生連結（dry-run）" if offline else f"共 {len(rows)} 組，DEAL {deals} 組"
    page = f"""<!doctype html><html lang="zh-Hant-TW"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>四腿外站票掃描</title>
<style>
:root{{--bg:#fafaf8;--fg:#1d1d1b;--mut:#6b6b66;--line:#e4e2dc;--deal:#e8f4ea;--acc:#1f6f43}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151514;--fg:#ecebe6;--mut:#9a9890;--line:#2c2b28;--deal:#1d3324;--acc:#7fd19b}}}}
body{{margin:0;padding:24px 16px;background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,"PingFang TC","Noto Sans TC",sans-serif}}
h1{{font-size:20px;margin:0 0 4px}} p{{color:var(--mut);margin:0 0 16px}}
.wrap{{overflow-x:auto}} table{{border-collapse:collapse;min-width:1100px;width:100%}}
th,td{{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}}
th{{font-weight:600;color:var(--mut);position:sticky;top:0;background:var(--bg)}}
td.n{{text-align:right;font-variant-numeric:tabular-nums}} td.legs{{color:var(--mut);font-size:12px}}
tr.deal{{background:var(--deal)}} a{{color:var(--acc)}}
</style></head><body>
<h1>四腿外站票掃描結果</h1>
<p>{datetime.now():%Y-%m-%d %H:%M}　{mode}　幣別 {cfg.get("currency","TWD")}
票價差＝同航司台北來回 − 四腿；台灣→外站單程僅供參考，不計入。價格為 Google Flights 參考價，下單前請到航司官網用「多個城市」重查。</p>
<div class="wrap"><table><thead><tr><th>航司</th><th>外站</th><th>目的地</th><th>出發</th><th>四段</th>
<th>四腿</th><th>台北來回</th><th>票價差</th><th>票價差 %</th><th>台灣→外站單程（參考）</th><th>標記</th><th>連結</th></tr></thead>
<tbody>{''.join(trs)}</tbody></table></div></body></html>"""
    html_path = out_dir / f"report-{stamp}.html"
    html_path.write_text(page, encoding="utf-8")
    return csv_path, html_path


def main():
    ap = argparse.ArgumentParser(description="四腿外站票掃描器")
    ap.add_argument("-c", "--config", default=str(HERE / "config.json"))
    ap.add_argument("--dry-run", action="store_true", help="不連網，只產生 Google Flights 連結報表")
    ap.add_argument("--airline", help="只掃單一航司，例如 BR")
    ap.add_argument("--outstation", help="只掃單一外站，例如 HKG")
    args = ap.parse_args()

    if create_query is None:
        sys.exit("請先安裝：pip install fast-flights")
    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    rows = scan(cfg, args.dry_run, args.airline, args.outstation)
    if not rows:
        sys.exit("沒有可查的日期（起始日需晚於今天）")
    csv_path, html_path = write_outputs(rows, cfg, args.dry_run)
    deals = [r for r in rows if r.deal]
    print(f"\n完成：{html_path}\nCSV：{csv_path}")
    for r in sorted(deals, key=lambda r: -(r.fare_saving or 0))[:10]:
        print(f"  {r.deal:14} {AIRLINE_NAMES.get(r.airline)} {r.outstation}->{r.dest} {r.depart} "
              f"四腿 {fmt(r.fourleg_price)} vs 台北來回 {fmt(r.baseline_price)}（省 {fmt(r.fare_saving_pct, True)}）")


if __name__ == "__main__":
    main()
