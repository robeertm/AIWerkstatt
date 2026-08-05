"""Users, secrets and app key — thin layer over the shared SQLite db.

Users log in with a name + PIN (hashed). Provider credentials (API keys / OAuth
tokens) live in the `secrets` table inside the private data volume — never in the
repo, never in a container image; they are handed to a run only as that ephemeral
container's environment.
"""
import os
import secrets as pysecrets
import time

from werkzeug.security import check_password_hash, generate_password_hash

import config
import db


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------- app secret key (session cookies) ----------

def get_secret_key():
    row = db.query_one("SELECT value FROM meta WHERE key='secret_key'")
    if row:
        return row["value"]
    key = pysecrets.token_hex(32)
    db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('secret_key', ?)", (key,))
    return key


# ---------- users ----------

def _pin_ok(pin):
    return isinstance(pin, str) and pin.isdigit() and 4 <= len(pin) <= 12


def ensure_seed():
    db.conn()  # create schema
    if not db.query_one("SELECT 1 FROM users LIMIT 1"):
        name = (config.ADMIN or "admin").strip()
        pin = config.ADMIN_PIN or "1234"
        db.execute("INSERT INTO users(name, pin_hash, role, emoji, must_change) VALUES(?,?,?,?,1)",
                   (name, generate_password_hash(pin), "admin", "🛠️"))


def list_users():
    return [dict(r) for r in db.query_all("SELECT name, role, emoji, must_change FROM users ORDER BY name")]


def get_user(name):
    if not name:
        return None
    r = db.query_one("SELECT name, role, emoji, must_change FROM users WHERE name=? COLLATE NOCASE", (name,))
    return dict(r) if r else None


def public_user(u):
    return {"name": u["name"], "role": u["role"], "emoji": u.get("emoji", "🙂"),
            "must_change": bool(u.get("must_change"))}


def verify(name, pin):
    if not name or not pin:
        return None
    r = db.query_one("SELECT * FROM users WHERE name=? COLLATE NOCASE", (name,))
    if r and check_password_hash(r["pin_hash"], str(pin)):
        return dict(r)
    return None


def set_pin(name, pin):
    if not _pin_ok(str(pin)):
        raise ValueError("PIN must be 4–12 digits.")
    if not get_user(name):
        raise ValueError("No such user.")
    db.execute("UPDATE users SET pin_hash=?, must_change=0 WHERE name=? COLLATE NOCASE",
               (generate_password_hash(str(pin)), name))


def add_user(name, pin, role="member", emoji="🙂"):
    name = (name or "").strip()
    if not name:
        raise ValueError("Name required.")
    if get_user(name):
        raise ValueError("User already exists.")
    if not _pin_ok(str(pin)):
        raise ValueError("PIN must be 4–12 digits.")
    role = "admin" if role == "admin" else "member"
    db.execute("INSERT INTO users(name, pin_hash, role, emoji, must_change) VALUES(?,?,?,?,0)",
               (name, generate_password_hash(str(pin)), role, emoji or "🙂"))
    return {"name": name, "role": role, "emoji": emoji or "🙂"}


def remove_user(name):
    if not get_user(name):
        raise ValueError("No such user.")
    admins = db.query_one("SELECT COUNT(*) c FROM users WHERE role='admin'")["c"]
    u = get_user(name)
    if u["role"] == "admin" and admins <= 1:
        raise ValueError("Cannot remove the last admin.")
    db.execute("DELETE FROM users WHERE name=? COLLATE NOCASE", (name,))


# ---------- secrets (provider credentials) ----------

def set_secret(key, value):
    db.execute("INSERT OR REPLACE INTO secrets(key, value) VALUES(?,?)", (key, value))


def get_secret(key, default=None):
    r = db.query_one("SELECT value FROM secrets WHERE key=?", (key,))
    return r["value"] if r else default


def delete_secret(key):
    db.execute("DELETE FROM secrets WHERE key=?", (key,))


def provider_connected(provider_id):
    return bool(get_secret("provider:%s:key" % provider_id)) or \
        bool(get_secret("provider:%s:oauth" % provider_id))
