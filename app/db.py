"""Small store: chat history, pauses (human takeover), leads, handoffs, campaigns.

Uses PostgreSQL when DATABASE_URL is set (e.g. free Neon database, for cloud hosting
where the disk is wiped on restart), otherwise a local SQLite file.
"""
import json
import os
import sqlite3
import threading
import time

from .config import DATA_DIR

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
PG = DATABASE_URL.startswith("postgres")
_lock = threading.Lock()
_conn = None

if PG:
    import psycopg
    from psycopg.rows import dict_row

_ID = "BIGSERIAL PRIMARY KEY" if PG else "INTEGER PRIMARY KEY"
_REAL = "DOUBLE PRECISION" if PG else "REAL"
SCHEMA = f"""
CREATE TABLE IF NOT EXISTS messages(
  id {_ID}, brand TEXT, customer TEXT, role TEXT, content TEXT, ts {_REAL});
CREATE INDEX IF NOT EXISTS ix_msg ON messages(brand, customer, ts);
CREATE TABLE IF NOT EXISTS paused(
  brand TEXT, customer TEXT, until {_REAL}, reason TEXT, PRIMARY KEY(brand, customer));
CREATE TABLE IF NOT EXISTS leads(
  id {_ID}, brand TEXT, customer TEXT, data TEXT, ts {_REAL});
CREATE TABLE IF NOT EXISTS handoffs(
  id {_ID}, brand TEXT, customer TEXT, reason TEXT, summary TEXT, ts {_REAL}, closed INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS processed(msg_id TEXT PRIMARY KEY, ts {_REAL});
CREATE TABLE IF NOT EXISTS tg_links(tg_msg_id BIGINT PRIMARY KEY, brand TEXT, customer TEXT);
CREATE TABLE IF NOT EXISTS optout(
  brand TEXT, customer TEXT, ts {_REAL}, PRIMARY KEY(brand, customer));
CREATE TABLE IF NOT EXISTS campaigns(
  id {_ID}, brand TEXT, total INTEGER, ts {_REAL}, template TEXT);
CREATE TABLE IF NOT EXISTS campaign_msgs(
  wamid TEXT PRIMARY KEY, campaign_id INTEGER, brand TEXT, customer TEXT,
  status TEXT DEFAULT 'sent', error TEXT, replied INTEGER DEFAULT 0, ts {_REAL});
CREATE TABLE IF NOT EXISTS contacts(
  brand TEXT, customer TEXT, name TEXT, PRIMARY KEY(brand, customer));
"""


def _connect():
    global _conn
    if PG:
        _conn = psycopg.connect(DATABASE_URL, autocommit=True, row_factory=dict_row)
    else:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(DATA_DIR / "agents.db", check_same_thread=False)
        _conn.row_factory = sqlite3.Row


def _run(sql, args=(), fetch=False):
    """Execute with one automatic reconnect (free Postgres closes idle connections)."""
    if PG:
        sql = sql.replace("?", "%s")
    with _lock:
        for attempt in (1, 2):
            try:
                if _conn is None or (PG and _conn.closed):
                    _connect()
                cur = _conn.execute(sql, args)
                rows = [dict(r) for r in cur.fetchall()] if fetch else None
                if not PG:
                    _conn.commit()
                return rows if fetch else cur
            except Exception as e:
                is_conn_err = PG and isinstance(e, (psycopg.OperationalError, psycopg.InterfaceError))
                if attempt == 1 and is_conn_err:
                    _connect()
                    continue
                raise


def _exec(sql, args=()):
    return _run(sql, args)


def _all(sql, args=()):
    return _run(sql, args, fetch=True)


def _upsert(table, cols, keys, values):
    marks = ",".join("?" * len(cols))
    if PG:
        sets = ",".join(f"{c}=EXCLUDED.{c}" for c in cols if c not in keys) or f"{keys[0]}=EXCLUDED.{keys[0]}"
        sql = f"INSERT INTO {table}({','.join(cols)}) VALUES({marks}) ON CONFLICT ({','.join(keys)}) DO UPDATE SET {sets}"
    else:
        sql = f"INSERT OR REPLACE INTO {table}({','.join(cols)}) VALUES({marks})"
    _exec(sql, values)


def _init():
    _connect()
    for stmt in [x.strip() for x in SCHEMA.split(";") if x.strip()]:
        _exec(stmt)
    try:  # older SQLite databases: add the template column
        _exec("ALTER TABLE campaigns ADD COLUMN template TEXT")
    except Exception:
        pass


_init()


# ---- dedupe -------------------------------------------------------------
def already_processed(msg_id: str) -> bool:
    cur = _exec("INSERT INTO processed(msg_id, ts) VALUES(?,?) ON CONFLICT DO NOTHING", (msg_id, time.time()))
    return cur.rowcount == 0


# ---- messages -----------------------------------------------------------
def add_message(brand, customer, role, content):
    _exec(
        "INSERT INTO messages(brand, customer, role, content, ts) VALUES(?,?,?,?,?)",
        (brand, customer, role, json.dumps(content, ensure_ascii=False), time.time()),
    )


def history(brand, customer, limit=30):
    rows = _all(
        "SELECT role, content FROM messages WHERE brand=? AND customer=? "
        "ORDER BY ts DESC LIMIT ?",
        (brand, customer, limit),
    )
    return [{"role": r["role"], "content": json.loads(r["content"])} for r in reversed(rows)]


def set_contact_name(brand, customer, name):
    if name:
        _upsert("contacts", ["brand", "customer", "name"], ["brand", "customer"], (brand, customer, name))


def contact_name(brand, customer):
    r = _all("SELECT name FROM contacts WHERE brand=? AND customer=?", (brand, customer))
    return r[0]["name"] if r else ""


# ---- pause / human takeover -------------------------------------------
def pause(brand, customer, hours, reason=""):
    _upsert("paused", ["brand", "customer", "until", "reason"], ["brand", "customer"],
            (brand, customer, time.time() + hours * 3600, reason))


def resume(brand, customer):
    _exec("DELETE FROM paused WHERE brand=? AND customer=?", (brand, customer))
    _exec("UPDATE handoffs SET closed=1 WHERE brand=? AND customer=?", (brand, customer))


def is_paused(brand, customer) -> bool:
    r = _all("SELECT until FROM paused WHERE brand=? AND customer=?", (brand, customer))
    if not r:
        return False
    if r[0]["until"] < time.time():
        resume(brand, customer)
        return False
    return True


def paused_list():
    now = time.time()
    return _all("SELECT * FROM paused WHERE until > ? ORDER BY until", (now,))


# ---- leads / handoffs -----------------------------------------------------
def save_lead(brand, customer, data: dict):
    _exec(
        "INSERT INTO leads(brand, customer, data, ts) VALUES(?,?,?,?)",
        (brand, customer, json.dumps(data, ensure_ascii=False), time.time()),
    )


def recent_leads(limit=10):
    rows = _all("SELECT * FROM leads ORDER BY ts DESC LIMIT ?", (limit,))
    for r in rows:
        r["data"] = json.loads(r["data"])
    return rows


def add_handoff(brand, customer, reason, summary):
    _exec(
        "INSERT INTO handoffs(brand, customer, reason, summary, ts) VALUES(?,?,?,?,?)",
        (brand, customer, reason, summary, time.time()),
    )


def open_handoffs():
    return _all("SELECT * FROM handoffs WHERE closed=0 ORDER BY ts DESC")


def link_tg(tg_msg_id, brand, customer):
    _upsert("tg_links", ["tg_msg_id", "brand", "customer"], ["tg_msg_id"], (tg_msg_id, brand, customer))


def tg_target(tg_msg_id):
    r = _all("SELECT brand, customer FROM tg_links WHERE tg_msg_id=?", (tg_msg_id,))
    return (r[0]["brand"], r[0]["customer"]) if r else None


# ---- stats ------------------------------------------------------------------
def stats(since_ts: float):
    out = {}
    for b in ("seen", "nvolve"):
        out[b] = {
            "chats": _all(
                "SELECT COUNT(DISTINCT customer) c FROM messages WHERE brand=? AND ts>? AND role='user'",
                (b, since_ts))[0]["c"],
            "messages": _all(
                "SELECT COUNT(*) c FROM messages WHERE brand=? AND ts>? AND role='user'",
                (b, since_ts))[0]["c"],
            "leads": _all("SELECT COUNT(*) c FROM leads WHERE brand=? AND ts>?", (b, since_ts))[0]["c"],
            "handoffs": _all("SELECT COUNT(*) c FROM handoffs WHERE brand=? AND ts>?", (b, since_ts))[0]["c"],
        }
    return out


# ---- campaigns / opt-out ------------------------------------------------------
def opt_out(brand, customer):
    _upsert("optout", ["brand", "customer", "ts"], ["brand", "customer"], (brand, customer, time.time()))


def is_opted_out(brand, customer) -> bool:
    return bool(_all("SELECT 1 FROM optout WHERE brand=? AND customer=?", (brand, customer)))


def new_campaign(brand, total, template="") -> int:
    return _all("INSERT INTO campaigns(brand, total, ts, template) VALUES(?,?,?,?) RETURNING id",
                (brand, total, time.time(), template))[0]["id"]


def add_campaign_msg(wamid, campaign_id, brand, customer):
    if wamid:
        _upsert("campaign_msgs", ["wamid", "campaign_id", "brand", "customer", "ts"], ["wamid"],
                (wamid, campaign_id, brand, customer, time.time()))


_STATUS_RANK = {"sent": 1, "delivered": 2, "read": 3, "failed": 4}


def update_campaign_status(wamid, status, error=""):
    r = _all("SELECT status FROM campaign_msgs WHERE wamid=?", (wamid,))
    if not r:
        return
    if _STATUS_RANK.get(status, 0) >= _STATUS_RANK.get(r[0]["status"], 0):
        _exec("UPDATE campaign_msgs SET status=?, error=? WHERE wamid=?", (status, error, wamid))


def mark_campaign_reply(brand, customer) -> bool:
    """Mark the latest campaign message to this customer as replied. True if it was a campaign contact."""
    r = _all("SELECT wamid, replied FROM campaign_msgs WHERE brand=? AND customer=? ORDER BY ts DESC LIMIT 1",
             (brand, customer))
    if not r:
        return False
    if not r[0]["replied"]:
        _exec("UPDATE campaign_msgs SET replied=1 WHERE wamid=?", (r[0]["wamid"],))
    return True


def campaign_report(limit=3):
    out = []
    for c in _all("SELECT * FROM campaigns ORDER BY id DESC LIMIT ?", (limit,)):
        rows = _all("SELECT status, replied FROM campaign_msgs WHERE campaign_id=?", (c["id"],))
        st = {k: 0 for k in ("sent", "delivered", "read", "failed")}
        for r in rows:
            st[r["status"]] = st.get(r["status"], 0) + 1
        replied = sum(r["replied"] for r in rows)
        optouts = _all("SELECT COUNT(*) n FROM optout WHERE brand=? AND ts>?", (c["brand"], c["ts"]))[0]["n"]
        out.append({**c, **st, "accepted": len(rows), "replied": replied, "optouts": optouts})
    return out
