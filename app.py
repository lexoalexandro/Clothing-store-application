import os
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta

from flask import Flask, g, jsonify, request, send_from_directory

APP_VERSION = "1.8"

# Working day: 10:00 - 22:00. A sale made from 22:00 onward is registered in the NEXT day
# (so the day ends on its own at 22:00).
DAY_START_HOUR = 10
DAY_END_HOUR = 22
PAYMENTS = ("cash", "visa")

# When packaged with PyInstaller, bundled files live in sys._MEIPASS
RES = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))

# Database is kept in the user's home folder so it survives updates of the exe:
#   Windows: C:\Users\<name>\StoreApp\store.db
DATA_DIR = os.path.join(os.path.expanduser("~"), "StoreApp")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "store.db")

app = Flask(__name__, static_folder=os.path.join(RES, "static"))


def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_):
    conn = g.pop("db", None)
    if conn:
        conn.close()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT '',
            price REAL NOT NULL,
            qr TEXT NOT NULL UNIQUE
        );
        CREATE TABLE IF NOT EXISTS stock (
            product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
            size TEXT NOT NULL,
            qty INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (product_id, size)
        );
        CREATE TABLE IF NOT EXISTS sales (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sold_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            product_id INTEGER,
            product_name TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT '',
            size TEXT NOT NULL,
            price REAL NOT NULL,
            payment TEXT NOT NULL DEFAULT 'cash',
            day TEXT,
            kind TEXT NOT NULL DEFAULT 'sale',
            note TEXT NOT NULL DEFAULT '',
            returned INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS days (
            day TEXT PRIMARY KEY,
            closed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        """
    )
    # Upgrade databases created by older versions (1.1 - 1.3)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(sales)")}
    if "category" not in cols:
        conn.execute("ALTER TABLE sales ADD COLUMN category TEXT NOT NULL DEFAULT ''")
    if "payment" not in cols:
        conn.execute("ALTER TABLE sales ADD COLUMN payment TEXT NOT NULL DEFAULT 'cash'")
    if "day" not in cols:
        conn.execute("ALTER TABLE sales ADD COLUMN day TEXT")
    if "kind" not in cols:
        conn.execute("ALTER TABLE sales ADD COLUMN kind TEXT NOT NULL DEFAULT 'sale'")
    if "note" not in cols:
        conn.execute("ALTER TABLE sales ADD COLUMN note TEXT NOT NULL DEFAULT ''")
    if "returned" not in cols:
        conn.execute("ALTER TABLE sales ADD COLUMN returned INTEGER NOT NULL DEFAULT 0")
    conn.execute("UPDATE sales SET day = date(sold_at) WHERE day IS NULL")
    conn.execute(
        "UPDATE sales SET category = COALESCE((SELECT category FROM products WHERE id = sales.product_id), '') "
        "WHERE category = ''"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sales_day ON sales(day)")
    conn.commit()
    conn.close()


# ---------------------------------------------------------------- working-day logic
def valid_day(s):
    try:
        datetime.strptime(s or "", "%Y-%m-%d")
        return True
    except ValueError:
        return False


def is_closed(conn, day):
    return conn.execute(
        "SELECT 1 FROM days WHERE day = ? AND closed_at IS NOT NULL", (day,)
    ).fetchone() is not None


def current_day(conn, now=None):
    """The day that new sales are registered in right now."""
    now = now or datetime.now()
    d = now.date()
    if now.hour >= DAY_END_HOUR:  # 22:00 or later -> next day
        d += timedelta(days=1)
    while is_closed(conn, d.isoformat()):  # day ended early -> next day
        d += timedelta(days=1)
    return d.isoformat()


def r2(x):
    return round(float(x or 0), 2)


def product_dict(row):
    sizes = db().execute(
        "SELECT size, qty FROM stock WHERE product_id = ? ORDER BY size", (row["id"],)
    ).fetchall()
    sizes = [{"size": s["size"], "qty": s["qty"]} for s in sizes]
    return {
        "id": row["id"],
        "name": row["name"],
        "category": row["category"],
        "price": row["price"],
        "qr": row["qr"],
        "sizes": sizes,
        "total": sum(s["qty"] for s in sizes),
    }


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


# ---------------------------------------------------------------- products / stock
@app.get("/api/products")
def list_products():
    q = request.args.get("q", "").strip().lower()
    category = request.args.get("category", "").strip()
    rows = db().execute("SELECT * FROM products ORDER BY name").fetchall()
    result = []
    for r in rows:
        if q and q not in r["name"].lower():
            continue
        if category and r["category"] != category:
            continue
        result.append(product_dict(r))
    return jsonify(result)


@app.get("/api/categories")
def categories():
    rows = db().execute(
        "SELECT DISTINCT category FROM products WHERE category != '' ORDER BY category"
    ).fetchall()
    return jsonify([r["category"] for r in rows])


@app.get("/api/product-by-qr/<path:qr>")
def by_qr(qr):
    row = db().execute("SELECT * FROM products WHERE qr = ?", (qr.strip(),)).fetchone()
    if not row:
        return jsonify({"error": "პროდუქტი ვერ მოიძებნა"}), 404
    return jsonify(product_dict(row))


@app.post("/api/products")
def add_product():
    """Create a product, or add stock to an existing one with the same QR code."""
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    category = (data.get("category") or "").strip()
    qr = (data.get("qr") or "").strip()
    try:
        price = float(data.get("price"))
    except (TypeError, ValueError):
        return jsonify({"error": "ფასი არასწორია"}), 400
    sizes = data.get("sizes") or []
    if not name or not qr:
        return jsonify({"error": "სახელი და QR კოდი სავალდებულოა"}), 400

    conn = db()
    row = conn.execute("SELECT id FROM products WHERE qr = ?", (qr,)).fetchone()
    if row:
        pid = row["id"]
        conn.execute(
            "UPDATE products SET name = ?, category = ?, price = ? WHERE id = ?",
            (name, category, price, pid),
        )
    else:
        cur = conn.execute(
            "INSERT INTO products (name, category, price, qr) VALUES (?, ?, ?, ?)",
            (name, category, price, qr),
        )
        pid = cur.lastrowid

    for s in sizes:
        size = str(s.get("size", "")).strip().upper()
        try:
            qty = int(s.get("qty", 0))
        except (TypeError, ValueError):
            qty = 0
        if not size or qty <= 0:
            continue
        conn.execute(
            """INSERT INTO stock (product_id, size, qty) VALUES (?, ?, ?)
               ON CONFLICT(product_id, size) DO UPDATE SET qty = qty + excluded.qty""",
            (pid, size, qty),
        )
    conn.commit()
    row = conn.execute("SELECT * FROM products WHERE id = ?", (pid,)).fetchone()
    return jsonify(product_dict(row))


@app.put("/api/stock")
def set_stock():
    """Set exact quantity for one size (used to correct mistakes)."""
    data = request.get_json(force=True)
    pid, size = data.get("product_id"), str(data.get("size", "")).strip().upper()
    qty = int(data.get("qty", 0))
    conn = db()
    if qty <= 0:
        conn.execute("DELETE FROM stock WHERE product_id = ? AND size = ?", (pid, size))
    else:
        conn.execute(
            """INSERT INTO stock (product_id, size, qty) VALUES (?, ?, ?)
               ON CONFLICT(product_id, size) DO UPDATE SET qty = excluded.qty""",
            (pid, size, qty),
        )
    conn.commit()
    return jsonify({"ok": True})


@app.delete("/api/products/<int:pid>")
def delete_product(pid):
    conn = db()
    conn.execute("DELETE FROM products WHERE id = ?", (pid,))
    conn.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------- settings (remembered payment choice)
@app.get("/api/settings")
def get_settings():
    rows = db().execute("SELECT key, value FROM settings").fetchall()
    return jsonify({r["key"]: r["value"] for r in rows})


@app.put("/api/settings")
def put_settings():
    payment = (request.get_json(force=True) or {}).get("payment")
    if payment not in PAYMENTS:
        return jsonify({"error": "ანგარიშწორების ტიპი არასწორია"}), 400
    conn = db()
    conn.execute(
        "INSERT INTO settings (key, value) VALUES ('payment', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (payment,),
    )
    conn.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------- selling
@app.post("/api/sell")
def sell():
    """items: [{product_id, size}, ...] - each item is 1 piece. All-or-nothing."""
    payload = request.get_json(force=True) or {}
    items = payload.get("items") or []
    payment = payload.get("payment")
    if not items:
        return jsonify({"error": "სია ცარიელია"}), 400
    if payment not in PAYMENTS:
        return jsonify({"error": "აირჩიე ანგარიშწორების საშუალება"}), 400

    conn = db()
    needed = Counter((int(i["product_id"]), str(i["size"]).strip().upper()) for i in items)

    for (pid, size), n in needed.items():
        row = conn.execute(
            "SELECT p.name, s.qty FROM products p LEFT JOIN stock s "
            "ON s.product_id = p.id AND s.size = ? WHERE p.id = ?",
            (size, pid),
        ).fetchone()
        if not row or (row["qty"] or 0) < n:
            name = row["name"] if row else f"#{pid}"
            have = (row["qty"] or 0) if row else 0
            return jsonify(
                {"error": f"არასაკმარისი მარაგი: {name} ({size}) - საწყობშია {have}, საჭიროა {n}"}
            ), 400

    day = current_day(conn)
    try:
        conn.execute("BEGIN")
        for (pid, size), n in needed.items():
            prod = conn.execute(
                "SELECT name, price, category FROM products WHERE id = ?", (pid,)
            ).fetchone()
            conn.execute(
                "UPDATE stock SET qty = qty - ? WHERE product_id = ? AND size = ?", (n, pid, size)
            )
            conn.execute("DELETE FROM stock WHERE product_id = ? AND size = ? AND qty <= 0", (pid, size))
            for _ in range(n):
                conn.execute(
                    "INSERT INTO sales (product_id, product_name, category, size, price, payment, day) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (pid, prod["name"], prod["category"], size, prod["price"], payment, day),
                )
        conn.commit()
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True, "day": day})


# ---------------------------------------------------------------- daily sales (დღიური ნავაჭრი)
@app.get("/api/day-info")
def day_info():
    conn = db()
    return jsonify({
        "current_day": current_day(conn),
        "today": datetime.now().date().isoformat(),
        "start": DAY_START_HOUR,
        "end": DAY_END_HOUR,
    })


@app.get("/api/sales")
def list_sales():
    day = request.args.get("day", "")
    if not valid_day(day):
        return jsonify({"error": "თარიღი არასწორია"}), 400
    conn = db()
    rows = conn.execute(
        "SELECT id, sold_at, product_name, category, size, price, payment, kind, note "
        "FROM sales WHERE day = ? ORDER BY id",
        (day,),
    ).fetchall()
    cur = current_day(conn)
    closed_flag = is_closed(conn, day)
    state = "open" if day == cur else ("closed" if day < cur else "future")
    return jsonify({
        "day": day,
        "state": state,
        "can_reopen": closed_flag and day >= datetime.now().date().isoformat(),
        "sales": [
            {
                "id": r["id"],
                "time": (r["sold_at"] or "")[11:16],
                "name": r["product_name"],
                "category": r["category"],
                "size": r["size"],
                "price": r["price"],
                "payment": r["payment"],
                "kind": r["kind"],
                "note": r["note"],
            }
            for r in rows
        ],
    })


@app.get("/api/summary")
def summary():
    """დღიური ჯამი: only three numbers - cash, visa, both together."""
    day = request.args.get("day", "")
    if not valid_day(day):
        return jsonify({"error": "თარიღი არასწორია"}), 400
    row = db().execute(
        "SELECT COALESCE(SUM(CASE WHEN payment = 'cash' THEN price END), 0) AS cash, "
        "       COALESCE(SUM(CASE WHEN payment = 'visa' THEN price END), 0) AS visa "
        "FROM sales WHERE day = ?",
        (day,),
    ).fetchone()
    cash, visa = r2(row["cash"]), r2(row["visa"])
    return jsonify({"day": day, "cash": cash, "visa": visa, "total": r2(cash + visa)})


@app.get("/api/calendar")
def calendar():
    month = request.args.get("month", "")  # YYYY-MM
    if not valid_day(month + "-01"):
        return jsonify({"error": "თვე არასწორია"}), 400
    rows = db().execute(
        "SELECT DISTINCT day FROM sales WHERE day LIKE ?", (month + "-%",)
    ).fetchall()
    return jsonify({"days": [r["day"] for r in rows]})


@app.put("/api/sales/<int:sid>")
def update_sale(sid):
    data = request.get_json(force=True) or {}
    conn = db()
    old = conn.execute("SELECT product_id, size, kind FROM sales WHERE id = ?", (sid,)).fetchone()
    if not old:
        return jsonify({"error": "ჩანაწერი ვერ მოიძებნა"}), 404
    payment = data.get("payment")
    try:
        price = float(data.get("price"))
    except (TypeError, ValueError):
        return jsonify({"error": "ფასი არასწორია"}), 400
    if payment not in PAYMENTS:
        return jsonify({"error": "გადახდის საშუალება არასწორია"}), 400

    if old["kind"] == "exchange":
        # an exchange row is only a money difference: no stock is involved
        note = (data.get("note") or "").strip()
        conn.execute(
            "UPDATE sales SET price = ?, payment = ?, note = ? WHERE id = ?", (price, payment, note, sid)
        )
        conn.commit()
        return jsonify({"ok": True, "stock_updated": False})

    name = (data.get("name") or "").strip()
    category = (data.get("category") or "").strip()
    size = (data.get("size") or "").strip().upper()
    if not name or not size:
        return jsonify({"error": "სახელწოდება და ზომა სავალდებულოა"}), 400
    if price < 0:
        return jsonify({"error": "ფასი არასწორია"}), 400
    pid, old_size = old["product_id"], old["size"]

    stock_updated = False
    try:
        conn.execute("BEGIN")
        product_exists = pid is not None and conn.execute(
            "SELECT 1 FROM products WHERE id = ?", (pid,)
        ).fetchone()
        if size != old_size and product_exists:
            # Size was corrected (e.g. M -> L): give the old size back, take one from the new size.
            row = conn.execute(
                "SELECT qty FROM stock WHERE product_id = ? AND size = ?", (pid, size)
            ).fetchone()
            if not row or row["qty"] < 1:
                conn.rollback()
                return jsonify({"error": f"ზომა {size} საწყობში არ არის (0 ც.). ჯერ დაამატე საწყობში, შემდეგ შეცვალე."}), 400
            conn.execute(
                """INSERT INTO stock (product_id, size, qty) VALUES (?, ?, 1)
                   ON CONFLICT(product_id, size) DO UPDATE SET qty = qty + 1""",
                (pid, old_size),
            )
            conn.execute(
                "UPDATE stock SET qty = qty - 1 WHERE product_id = ? AND size = ?", (pid, size)
            )
            conn.execute("DELETE FROM stock WHERE product_id = ? AND size = ? AND qty <= 0", (pid, size))
            stock_updated = True
        conn.execute(
            "UPDATE sales SET product_name = ?, category = ?, size = ?, price = ?, payment = ? WHERE id = ?",
            (name, category, size, price, payment, sid),
        )
        conn.commit()
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True, "stock_updated": stock_updated})


@app.delete("/api/sales/<int:sid>")
def delete_sale(sid):
    conn = db()
    conn.execute("DELETE FROM sales WHERE id = ?", (sid,))
    conn.commit()
    return jsonify({"ok": True})


@app.get("/api/returnable/<int:pid>")
def returnable(pid):
    """Sold pieces of this product that have not been exchanged yet (newest first)."""
    rows = db().execute(
        "SELECT id, day, sold_at, size, price FROM sales "
        "WHERE product_id = ? AND kind = 'sale' AND returned = 0 ORDER BY id DESC",
        (pid,),
    ).fetchall()
    return jsonify([
        {"id": r["id"], "day": r["day"], "time": (r["sold_at"] or "")[11:16], "size": r["size"], "price": r["price"]}
        for r in rows
    ])


@app.post("/api/exchange")
def exchange():
    """გადაცვლა: the customer brings back one item and takes another.
    Stock: returned item +1, new item -1.
    Money: ONLY the price difference (new - returned) is added to today's daily sales,
    as one 'გადაცვლილი თანხა' row (negative when the new item is cheaper = money given back)."""
    data = request.get_json(force=True) or {}
    ret, new = data.get("return") or {}, data.get("new") or {}
    conn = db()
    try:
        sale_id = int(ret["sale_id"])
        n_pid, n_size, n_price = int(new["product_id"]), str(new["size"]).strip().upper(), float(new["price"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "მონაცემები არასწორია"}), 400
    if not n_size or n_price < 0:
        return jsonify({"error": "აირჩიე ახალი ნივთის ზომა"}), 400

    # the returned item must be a piece that was really sold and not exchanged before
    sold = conn.execute(
        "SELECT id, product_id, size, price, kind, returned FROM sales WHERE id = ?", (sale_id,)
    ).fetchone()
    if not sold or sold["kind"] != "sale" or sold["product_id"] is None:
        return jsonify({"error": "ეს ნივთი გაყიდვების ისტორიაში ვერ მოიძებნა"}), 400
    if sold["returned"]:
        return jsonify({"error": "ეს ნივთი უკვე გადაცვლილია"}), 400
    r_pid, r_size, r_price = sold["product_id"], sold["size"], float(sold["price"])
    if (r_pid, r_size) == (n_pid, n_size):
        return jsonify({"error": "ეს იგივე ნივთია"}), 400

    r_prod = conn.execute("SELECT name FROM products WHERE id = ?", (r_pid,)).fetchone()
    n_prod = conn.execute("SELECT name FROM products WHERE id = ?", (n_pid,)).fetchone()
    if not r_prod or not n_prod:
        return jsonify({"error": "პროდუქტი ვერ მოიძებნა"}), 404
    have = conn.execute(
        "SELECT qty FROM stock WHERE product_id = ? AND size = ?", (n_pid, n_size)
    ).fetchone()
    if not have or have["qty"] < 1:
        return jsonify({"error": f"{n_prod['name']} ({n_size}) საწყობში არ არის"}), 400

    diff = round(n_price - r_price, 2)
    if payment_missing := (data.get("payment") not in PAYMENTS):
        if diff != 0:
            return jsonify({"error": "აირჩიე გადახდის საშუალება"}), 400
    payment = "cash" if payment_missing else data["payment"]

    day = current_day(conn)
    note = f"{r_prod['name']} ({r_size}) → {n_prod['name']} ({n_size})"
    try:
        conn.execute("BEGIN")
        conn.execute(
            """INSERT INTO stock (product_id, size, qty) VALUES (?, ?, 1)
               ON CONFLICT(product_id, size) DO UPDATE SET qty = qty + 1""",
            (r_pid, r_size),
        )
        conn.execute("UPDATE sales SET returned = 1 WHERE id = ?", (sale_id,))
        conn.execute("UPDATE stock SET qty = qty - 1 WHERE product_id = ? AND size = ?", (n_pid, n_size))
        conn.execute("DELETE FROM stock WHERE product_id = ? AND size = ? AND qty <= 0", (n_pid, n_size))
        conn.execute(
            "INSERT INTO sales (product_id, product_name, category, size, price, payment, day, kind, note) "
            "VALUES (NULL, 'გადაცვლილი თანხა', 'გადაცვლა', ?, ?, ?, ?, 'exchange', ?)",
            (f"{r_size} → {n_size}", diff, payment, day, note),
        )
        conn.commit()
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True, "diff": diff, "day": day, "note": note})


@app.post("/api/day/close")
def close_day():
    day = (request.get_json(force=True) or {}).get("day", "")
    if not valid_day(day):
        return jsonify({"error": "თარიღი არასწორია"}), 400
    conn = db()
    conn.execute(
        "INSERT INTO days (day, closed_at) VALUES (?, datetime('now', 'localtime')) "
        "ON CONFLICT(day) DO UPDATE SET closed_at = excluded.closed_at",
        (day,),
    )
    conn.commit()
    return jsonify({"ok": True})


@app.post("/api/day/open")
def open_day():
    day = (request.get_json(force=True) or {}).get("day", "")
    if not valid_day(day):
        return jsonify({"error": "თარიღი არასწორია"}), 400
    conn = db()
    conn.execute("UPDATE days SET closed_at = NULL WHERE day = ?", (day,))
    conn.commit()
    return jsonify({"ok": True})


def free_port():
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for_server(port, timeout=10):
    import socket
    import time
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return
        except OSError:
            time.sleep(0.1)


if __name__ == "__main__":
    import threading

    init_db()
    port = free_port()
    threading.Thread(
        target=lambda: app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False),
        daemon=True,
    ).start()
    wait_for_server(port)

    import webview  # pywebview: native window

    webview.create_window(f"მაღაზია v{APP_VERSION}", f"http://127.0.0.1:{port}", width=1200, height=800)
    webview.start()
