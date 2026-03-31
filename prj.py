#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import html
import secrets
import shutil
import sqlite3
import subprocess
import urllib.parse
import webbrowser
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timedelta
from http import cookies
from pathlib import Path
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIServer, make_server


BASE_DIR = Path(__file__).resolve().parent
RESOURCES_DIR = BASE_DIR / "resources"
IMPORT_DIR = RESOURCES_DIR / "import"
IMAGES_DIR = RESOURCES_DIR / "product_images"
DB_PATH = BASE_DIR / "shoe_store.db"
SCHEMA_PATH = BASE_DIR / "schema.sql"
SESSIONS: dict[str, dict[str, object]] = {}


def e(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def parse_date(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    try:
        serial = int(float(value))
        return (datetime(1899, 12, 30) + timedelta(days=serial)).strftime("%Y-%m-%d")
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(value, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return value


def xlsx_rows(path: Path) -> list[list[str]]:
    ns = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    rows: list[list[str]] = []

    def col_index(ref: str) -> int:
        col = "".join(ch for ch in ref if ch.isalpha())
        result = 0
        for ch in col:
            result = result * 26 + ord(ch.upper()) - 64
        return result - 1

    with zipfile.ZipFile(path) as zf:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in zf.namelist():
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for item in root.findall("main:si", ns):
                shared.append("".join(node.text or "" for node in item.findall(".//main:t", ns)))

        workbook = ET.fromstring(zf.read("xl/workbook.xml"))
        rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
        rel_map = {rel.attrib["Id"]: rel.attrib["Target"] for rel in rels}
        sheet = workbook.find("main:sheets", ns)[0]
        rid = sheet.attrib["{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"]
        sheet_xml = ET.fromstring(zf.read("xl/" + rel_map[rid]))

        for row in sheet_xml.findall(".//main:sheetData/main:row", ns):
            cells: dict[int, str] = {}
            max_idx = -1
            for cell in row.findall("main:c", ns):
                idx = col_index(cell.attrib.get("r", "A1"))
                max_idx = max(max_idx, idx)
                cell_type = cell.attrib.get("t")
                value = ""
                if cell_type == "inlineStr":
                    value = "".join(node.text or "" for node in cell.findall(".//main:t", ns))
                else:
                    node = cell.find("main:v", ns)
                    if node is not None:
                        raw = node.text or ""
                        value = shared[int(raw)] if cell_type == "s" and raw.isdigit() else raw
                cells[idx] = value.strip()
            result = [cells.get(i, "") for i in range(max_idx + 1)]
            if any(part.strip() for part in result):
                rows.append(result)
    return rows


def ensure_resources() -> None:
    IMPORT_DIR.mkdir(parents=True, exist_ok=True)
    if not (IMPORT_DIR / "Tovar.xlsx").exists():
        archive = next(Path.home().joinpath("Downloads").glob("Прил_2_ОЗ_КОД*.rar"), None)
        if archive is None:
            raise FileNotFoundError("Не найден архив с ресурсами в Downloads.")
        subprocess.run(["bsdtar", "-xf", str(archive), "-C", str(RESOURCES_DIR)], check=True)

    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    placeholder = IMPORT_DIR / "picture.png"
    if placeholder.exists() and not (IMAGES_DIR / "picture.png").exists():
        shutil.copy2(placeholder, IMAGES_DIR / "picture.png")

    for file in IMPORT_DIR.glob("*.jpg"):
        target = IMAGES_DIR / f"{file.stem}.png"
        if not target.exists():
            subprocess.run(
                ["sips", "-Z", "300", "-s", "format", "png", str(file), "--out", str(target)],
                check=True,
                capture_output=True,
                text=True,
            )


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_or_create(conn: sqlite3.Connection, table: str, name: str) -> int:
    row = conn.execute(f"SELECT id FROM {table} WHERE name = ?", (name,)).fetchone()
    if row:
        return int(row["id"])
    return int(conn.execute(f"INSERT INTO {table} (name) VALUES (?)", (name,)).lastrowid)


def init_db(force: bool = False) -> None:
    ensure_resources()
    if force and DB_PATH.exists():
        DB_PATH.unlink()
    if DB_PATH.exists():
        return

    with connect() as conn:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))

        for row in xlsx_rows(IMPORT_DIR / "user_import.xlsx")[1:]:
            if len(row) < 4 or not row[2]:
                continue
            role_id = get_or_create(conn, "roles", row[0].strip())
            conn.execute(
                "INSERT INTO users (full_name, login, password, role_id) VALUES (?, ?, ?, ?)",
                (row[1].strip(), row[2].strip(), row[3].strip(), role_id),
            )

        for row in xlsx_rows(IMPORT_DIR / "Tovar.xlsx")[1:]:
            if len(row) < 11 or not row[0]:
                continue
            supplier_id = get_or_create(conn, "suppliers", row[4].strip())
            manufacturer_id = get_or_create(conn, "manufacturers", row[5].strip())
            category_id = get_or_create(conn, "categories", row[6].strip())
            image_path = IMAGES_DIR / "picture.png"
            if row[10].strip():
                candidate = IMAGES_DIR / f"{Path(row[10]).stem}.png"
                if candidate.exists():
                    image_path = candidate
            conn.execute(
                """
                INSERT INTO products (
                    article, name, unit, price, supplier_id, manufacturer_id, category_id,
                    discount_percent, stock_quantity, description, image_path
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row[0].strip(),
                    row[1].strip(),
                    row[2].strip(),
                    float(row[3] or 0),
                    supplier_id,
                    manufacturer_id,
                    category_id,
                    int(float(row[7] or 0)),
                    int(float(row[8] or 0)),
                    row[9].strip(),
                    str(image_path),
                ),
            )

        for row in xlsx_rows(IMPORT_DIR / "Пункты выдачи_import.xlsx"):
            if row and row[0].strip():
                conn.execute("INSERT INTO pickup_points (address) VALUES (?)", (row[0].strip(),))

        for row in xlsx_rows(IMPORT_DIR / "Заказ_import.xlsx")[1:]:
            if len(row) < 8 or not row[0]:
                continue
            status_id = get_or_create(conn, "order_statuses", row[7].strip())
            client = conn.execute("SELECT id FROM users WHERE full_name = ?", (row[5].strip(),)).fetchone()
            order_id = int(float(row[0]))
            conn.execute(
                """
                INSERT INTO orders (id, order_date, delivery_date, pickup_point_id, client_id, pickup_code, status_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order_id,
                    parse_date(row[2]),
                    parse_date(row[3]),
                    int(float(row[4])),
                    int(client["id"]) if client else None,
                    row[6].strip(),
                    status_id,
                ),
            )
            for article, qty in parse_items(row[1]):
                product = conn.execute("SELECT id FROM products WHERE article = ?", (article,)).fetchone()
                if product:
                    conn.execute(
                        "INSERT INTO order_items (order_id, product_id, quantity) VALUES (?, ?, ?)",
                        (order_id, int(product["id"]), qty),
                    )
        conn.commit()


def parse_items(text: str) -> list[tuple[str, int]]:
    parts = [part.strip() for part in text.split(",") if part.strip()]
    if not parts or len(parts) % 2 != 0:
        raise ValueError("Артикулы заказа укажите так: А112Т4, 2, F635R4, 1")
    result: list[tuple[str, int]] = []
    for i in range(0, len(parts), 2):
        qty = int(parts[i + 1])
        if qty <= 0:
            raise ValueError("Количество товара должно быть больше 0.")
        result.append((parts[i], qty))
    return result


def current_user(environ: dict[str, object]) -> dict[str, object] | None:
    raw = environ.get("HTTP_COOKIE", "")
    cookie = cookies.SimpleCookie()
    cookie.load(raw)
    if "sid" not in cookie:
        return None
    return SESSIONS.get(cookie["sid"].value)


def login_user(user: sqlite3.Row | dict[str, object]) -> str:
    sid = secrets.token_hex(24)
    data = {
        "id": user["id"] if "id" in user.keys() else None,  # type: ignore[union-attr]
        "full_name": user["full_name"],
        "role_name": user["role_name"],
    }
    SESSIONS[sid] = data
    cookie = cookies.SimpleCookie()
    cookie["sid"] = sid
    cookie["sid"]["path"] = "/"
    return cookie.output(header="").strip()


def logout_cookie() -> str:
    cookie = cookies.SimpleCookie()
    cookie["sid"] = ""
    cookie["sid"]["path"] = "/"
    cookie["sid"]["expires"] = "Thu, 01 Jan 1970 00:00:00 GMT"
    return cookie.output(header="").strip()


def body_data(environ: dict[str, object]) -> dict[str, str]:
    size = int(environ.get("CONTENT_LENGTH", "0") or 0)
    raw = environ["wsgi.input"].read(size).decode("utf-8")
    parsed = urllib.parse.parse_qs(raw, keep_blank_values=True)
    return {k: v[-1] for k, v in parsed.items()}


def query_data(environ: dict[str, object]) -> dict[str, str]:
    parsed = urllib.parse.parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True)
    return {k: v[-1] for k, v in parsed.items()}


def page(title: str, content: str, message: str = "", error: bool = False) -> bytes:
    msg = ""
    if message:
        cls = "msg error" if error else "msg"
        msg = f'<div class="{cls}">{e(message)}</div>'
    css = """
    body{font-family:"Times New Roman",serif;background:#fff;margin:0}
    .wrap{max-width:1200px;margin:0 auto;padding:20px}
    .top{background:#7FFF00;padding:14px 18px;margin-bottom:18px}
    .card,.panel{border:1px solid #ccc;padding:14px;background:#fff;margin-bottom:14px}
    .btn,button{background:#00FA9A;border:1px solid #999;padding:8px 12px;color:#000;text-decoration:none;cursor:pointer;font-family:inherit}
    .btn.gray{background:#eee}
    .row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
    .grid{display:grid;grid-template-columns:300px 1fr;gap:14px}
    .products{display:grid;grid-template-columns:1fr 1fr;gap:14px}
    .top-right{text-align:right}
    img.product{width:300px;height:200px;object-fit:cover;background:#eee}
    input,select,textarea{width:100%;padding:8px;box-sizing:border-box;font-family:inherit}
    textarea{min-height:110px}
    table{width:100%;border-collapse:collapse;background:#fff}
    th,td{border:1px solid #ddd;padding:8px;text-align:left;vertical-align:top}
    .discount{display:inline-block;padding:5px 8px;background:#7FFF00}
    .discount.high{background:#2E8B57;color:#fff}
    .old-price{color:red;text-decoration:line-through;margin-right:8px}
    .new-price{color:black;font-weight:bold}
    .out-of-stock{background:#dff6ff}
    .orders{display:flex;flex-direction:column;gap:14px}
    .order-card{display:grid;grid-template-columns:3fr 1fr;gap:14px;border:1px solid #ccc;padding:14px;background:#fff}
    .order-main{display:flex;flex-direction:column;gap:6px}
    .order-side{border-left:1px solid #ddd;padding-left:14px}
    .msg{padding:10px;margin-bottom:12px;border:1px solid #9bc58b;background:#eef9e9}
    .msg.error{border-color:#d88;background:#fff0f0}
    @media (max-width:900px){.products{grid-template-columns:1fr}.grid{grid-template-columns:1fr}img.product{width:100%;height:auto}.order-card{grid-template-columns:1fr}.order-side{border-left:none;padding-left:0}}
    """
    html_doc = f"""<!doctype html>
    <html lang="ru">
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>{e(title)}</title>
      <style>{css}</style>
    </head>
    <body>
      <div class="wrap">
        {msg}
        {content}
      </div>
    </body>
    </html>"""
    return html_doc.encode("utf-8")


def nav(user: dict[str, object] | None, title: str) -> str:
    if not user:
        return f'<div class="top"><h1 style="margin:0">{e(title)}</h1></div>'
    orders_btn = ""
    if str(user["role_name"]) in {"Менеджер", "Администратор"}:
        orders_btn = '<a class="btn gray" href="/orders">Заказы</a>'
    return f"""
    <div class="top">
      <div class="row" style="justify-content:space-between">
        <div>
          <h1 style="margin:0">{e(title)}</h1>
        </div>
        <div class="top-right">
          <div style="margin-bottom:8px">{e(user["full_name"])} ({e(user["role_name"])})</div>
          <div class="row" style="justify-content:flex-end">
            <a class="btn gray" href="/products">Товары</a>
            {orders_btn}
            <a class="btn gray" href="/logout">Выход</a>
          </div>
        </div>
      </div>
    </div>
    """


def respond(start_response, body: bytes, status: str = "200 OK", headers: list[tuple[str, str]] | None = None):  # type: ignore[no-untyped-def]
    out = [("Content-Type", "text/html; charset=utf-8"), ("Content-Length", str(len(body)))]
    if headers:
        out.extend(headers)
    start_response(status, out)
    return [body]


def redirect(start_response, location: str, headers: list[tuple[str, str]] | None = None):  # type: ignore[no-untyped-def]
    out = [("Location", location)]
    if headers:
        out.extend(headers)
    start_response("302 Found", out)
    return [b""]


def require(user: dict[str, object] | None, roles: set[str]) -> bool:
    return bool(user and str(user["role_name"]) in roles)


def render_login(message: str = "", error: bool = False) -> bytes:
    content = """
    <div class="panel" style="max-width:420px;margin:60px auto">
      <h1>Авторизация</h1>
      <form method="post" action="/login">
        <p><label>Логин<br><input name="login" required></label></p>
        <p><label>Пароль<br><input type="password" name="password" required></label></p>
        <div class="row">
          <button type="submit">Войти</button>
          <a class="btn gray" href="/guest">Войти как гость</a>
        </div>
      </form>
    </div>
    """
    return page("Авторизация", content, message, error)


def product_image_url(path: str) -> str:
    return "/media/" + urllib.parse.quote(Path(path).name)


def product_list_html(user: dict[str, object] | None, query: dict[str, str]) -> str:
    role = str(user["role_name"]) if user else "Гость"
    search = query.get("search", "")
    supplier = query.get("supplier", "")
    sort = query.get("sort", "")
    can_filter = role in {"Менеджер", "Администратор"}
    can_edit = role == "Администратор"

    sql = """
    SELECT products.id, products.article, products.name, products.unit, products.price,
           suppliers.name supplier_name, manufacturers.name manufacturer_name, categories.name category_name,
           products.discount_percent, products.stock_quantity, products.description, products.image_path
    FROM products
    JOIN suppliers ON suppliers.id = products.supplier_id
    JOIN manufacturers ON manufacturers.id = products.manufacturer_id
    JOIN categories ON categories.id = products.category_id
    WHERE 1=1
    """
    params: list[object] = []
    if can_filter and supplier and supplier != "Все поставщики":
        sql += " AND suppliers.name = ?"
        params.append(supplier)
    if can_filter and sort == "asc":
        sql += " ORDER BY products.stock_quantity ASC, products.id ASC"
    elif can_filter and sort == "desc":
        sql += " ORDER BY products.stock_quantity DESC, products.id ASC"
    else:
        sql += " ORDER BY products.id ASC"

    with connect() as conn:
        products = conn.execute(sql, params).fetchall()
        suppliers = [row["name"] for row in conn.execute("SELECT name FROM suppliers ORDER BY name").fetchall()]

    if can_filter and search.strip():
        words = [word.casefold() for word in search.strip().split() if word]
        filtered_products = []
        for product in products:
            haystack = " ".join(
                [
                    str(product["article"]),
                    str(product["name"]),
                    str(product["unit"]),
                    str(product["description"]),
                    str(product["supplier_name"]),
                    str(product["manufacturer_name"]),
                    str(product["category_name"]),
                ]
            ).casefold()
            if all(word in haystack for word in words):
                filtered_products.append(product)
        products = filtered_products

    filters = ""
    if can_filter:
        options = '<option value="Все поставщики">Все поставщики</option>'
        for item in suppliers:
            selected = "selected" if item == supplier else ""
            options += f'<option value="{e(item)}" {selected}>{e(item)}</option>'
        filters = f"""
        <div class="panel">
          <form method="get" id="filterForm">
            <div class="row">
              <div style="flex:1;min-width:220px"><label>Поиск<br><input name="search" value="{e(search)}" id="searchInput"></label></div>
              <div style="flex:1;min-width:220px"><label>Поставщик<br><select name="supplier" id="supplierInput">{options}</select></label></div>
              <div style="flex:1;min-width:220px"><label>Сортировка<br>
                <select name="sort" id="sortInput">
                  <option value="">Без сортировки</option>
                  <option value="asc" {"selected" if sort == "asc" else ""}>По возрастанию остатка</option>
                  <option value="desc" {"selected" if sort == "desc" else ""}>По убыванию остатка</option>
                </select>
              </label></div>
              {"<div><br><a class='btn' href='/product/new'>Добавить товар</a></div>" if can_edit else ""}
            </div>
          </form>
        </div>
        <script>
        let t;
        function autoSend() {{
          clearTimeout(t);
          t = setTimeout(() => document.getElementById('filterForm').submit(), 500);
        }}
        document.getElementById('searchInput').addEventListener('input', autoSend);
        document.getElementById('supplierInput').addEventListener('change', autoSend);
        document.getElementById('sortInput').addEventListener('change', autoSend);
        </script>
        """
    elif can_edit:
        filters = '<div class="panel"><a class="btn" href="/product/new">Добавить товар</a></div>'

    cards = ""
    for product in products:
        discount_cls = "discount high" if int(product["discount_percent"]) > 15 else "discount"
        card_cls = "card out-of-stock" if int(product["stock_quantity"]) == 0 else "card"
        price_html = f"{float(product['price']):.2f} руб."
        if int(product["discount_percent"]) > 0:
            final_price = float(product["price"]) * (100 - int(product["discount_percent"])) / 100
            price_html = (
                f'<span class="old-price">{float(product["price"]):.2f} руб.</span>'
                f'<span class="new-price">{final_price:.2f} руб.</span>'
            )
        actions = ""
        if can_edit:
            actions = f"""
            <div class="row">
              <a class="btn gray" href="/product/edit?id={product['id']}">Редактировать</a>
              <form method="post" action="/product/delete" onsubmit="return confirm('Удалить товар?')" style="margin:0">
                <input type="hidden" name="id" value="{product['id']}">
                <button type="submit">Удалить</button>
              </form>
            </div>
            """
        cards += f"""
        <div class="{card_cls}">
          <div class="grid">
            <div><img class="product" src="{product_image_url(product['image_path'])}" alt=""></div>
            <div>
              <h2 style="margin-top:0">{e(product['name'])} ({e(product['article'])})</h2>
              <div>Категория: {e(product['category_name'])}</div>
              <div>Производитель: {e(product['manufacturer_name'])}</div>
              <div>Поставщик: {e(product['supplier_name'])}</div>
              <div>Ед. изм.: {e(product['unit'])}</div>
              <div>Цена: {price_html}</div>
              <div>Количество на складе: {e(product['stock_quantity'])}</div>
              <div class="{discount_cls}">Скидка: {e(product['discount_percent'])}%</div>
              <p>{e(product['description'])}</p>
              {actions}
            </div>
          </div>
        </div>
        """

    return nav(user, "Список товаров") + filters + f'<div class="products">{cards}</div>'


def render_product_form(user: dict[str, object], product_id: int | None, message: str = "", error: bool = False) -> bytes:
    with connect() as conn:
        product = None
        if product_id is not None:
            product = conn.execute(
                """
                SELECT products.*, suppliers.name supplier_name, manufacturers.name manufacturer_name, categories.name category_name
                FROM products
                JOIN suppliers ON suppliers.id = products.supplier_id
                JOIN manufacturers ON manufacturers.id = products.manufacturer_id
                JOIN categories ON categories.id = products.category_id
                WHERE products.id = ?
                """,
                (product_id,),
            ).fetchone()
        next_id = conn.execute("SELECT COALESCE(MAX(id),0)+1 FROM products").fetchone()[0]
        categories = [row["name"] for row in conn.execute("SELECT name FROM categories ORDER BY name").fetchall()]
        manufacturers = [row["name"] for row in conn.execute("SELECT name FROM manufacturers ORDER BY name").fetchall()]

    if product_id is not None and product is None:
        return page("Товар", nav(user, "Товар") + "<div class='panel'>Товар не найден.</div>", "Товар не найден.", True)

    category_options = "".join(
        f'<option value="{e(name)}" {"selected" if product and product["category_name"] == name else ""}>{e(name)}</option>'
        for name in categories
    )
    manufacturer_options = "".join(
        f'<option value="{e(name)}" {"selected" if product and product["manufacturer_name"] == name else ""}>{e(name)}</option>'
        for name in manufacturers
    )
    content = nav(user, "Добавление товара" if product is None else "Редактирование товара") + f"""
    <div class="panel">
      <form method="post" action="/product/save" id="productForm">
        <input type="hidden" name="id" value="{e(product['id'] if product else '')}">
        <input type="hidden" name="old_image_path" value="{e(product['image_path'] if product else str(IMAGES_DIR / 'picture.png'))}">
        <input type="hidden" name="image_data" id="image_data">
        <p><label>ID<br><input value="{e(product['id'] if product else next_id)}" readonly></label></p>
        <p><label>Артикул<br><input name="article" value="{e(product['article'] if product else '')}" required></label></p>
        <p><label>Наименование товара<br><input name="name" value="{e(product['name'] if product else '')}" required></label></p>
        <p><label>Категория товара<br><select name="category_name" required>{category_options}</select></label></p>
        <p><label>Описание товара<br><textarea name="description" required>{e(product['description'] if product else '')}</textarea></label></p>
        <p><label>Производитель<br><select name="manufacturer_name" required>{manufacturer_options}</select></label></p>
        <p><label>Поставщик<br><input name="supplier_name" value="{e(product['supplier_name'] if product else '')}" required></label></p>
        <p><label>Цена<br><input name="price" value="{e(product['price'] if product else '0')}" required></label></p>
        <p><label>Единица измерения<br><input name="unit" value="{e(product['unit'] if product else 'шт.')}" required></label></p>
        <p><label>Количество на складе<br><input name="stock_quantity" value="{e(product['stock_quantity'] if product else '0')}" required></label></p>
        <p><label>Действующая скидка<br><input name="discount_percent" value="{e(product['discount_percent'] if product else '0')}" required></label></p>
        <p><label>Изображение<br><input type="file" id="image_file" accept=".png,.jpg,.jpeg"></label></p>
        <div class="row">
          <button type="submit">Сохранить</button>
          <a class="btn gray" href="/products">Отмена</a>
        </div>
      </form>
    </div>
    <script>
    const form = document.getElementById('productForm');
    form.addEventListener('submit', function(event) {{
      const file = document.getElementById('image_file').files[0];
      if (!file) return;
      event.preventDefault();
      const reader = new FileReader();
      reader.onload = function() {{
        const img = new Image();
        img.onload = function() {{
          const canvas = document.createElement('canvas');
          canvas.width = 300;
          canvas.height = 200;
          const ctx = canvas.getContext('2d');
          ctx.fillStyle = '#ffffff';
          ctx.fillRect(0, 0, 300, 200);
          const scale = Math.min(300 / img.width, 200 / img.height);
          const w = img.width * scale;
          const h = img.height * scale;
          const x = (300 - w) / 2;
          const y = (200 - h) / 2;
          ctx.drawImage(img, x, y, w, h);
          document.getElementById('image_data').value = canvas.toDataURL('image/png');
          form.submit();
        }};
        img.src = reader.result;
      }};
      reader.readAsDataURL(file);
    }});
    </script>
    """
    return page("Товар", content, message, error)


def save_image(data_url: str, article: str) -> str:
    if not data_url.strip():
        return ""
    encoded = data_url.split(",", 1)[1]
    data = base64.b64decode(encoded)
    path = IMAGES_DIR / f"{article}_{int(datetime.now().timestamp())}.png"
    path.write_bytes(data)
    return str(path)


def save_product(data: dict[str, str]) -> None:
    price = float(data["price"].replace(",", "."))
    stock = int(data["stock_quantity"])
    discount = int(data["discount_percent"])
    if price < 0 or stock < 0 or discount < 0:
        raise ValueError("Цена, количество и скидка не могут быть отрицательными.")

    new_image = save_image(data.get("image_data", ""), data["article"].strip())
    image_path = new_image or data.get("old_image_path", str(IMAGES_DIR / "picture.png"))

    with connect() as conn:
        supplier_id = get_or_create(conn, "suppliers", data["supplier_name"].strip())
        manufacturer_id = get_or_create(conn, "manufacturers", data["manufacturer_name"].strip())
        category_id = get_or_create(conn, "categories", data["category_name"].strip())

        if data.get("id", "").strip():
            old = conn.execute("SELECT image_path FROM products WHERE id = ?", (int(data["id"]),)).fetchone()
            conn.execute(
                """
                UPDATE products
                SET article=?, name=?, unit=?, price=?, supplier_id=?, manufacturer_id=?,
                    category_id=?, discount_percent=?, stock_quantity=?, description=?, image_path=?
                WHERE id=?
                """,
                (
                    data["article"].strip(),
                    data["name"].strip(),
                    data["unit"].strip(),
                    price,
                    supplier_id,
                    manufacturer_id,
                    category_id,
                    discount,
                    stock,
                    data["description"].strip(),
                    image_path,
                    int(data["id"]),
                ),
            )
            if new_image and old:
                old_path = Path(old["image_path"])
                if old_path.exists() and old_path.parent == IMAGES_DIR and old_path.name != "picture.png":
                    old_path.unlink(missing_ok=True)
        else:
            conn.execute(
                """
                INSERT INTO products (
                    article, name, unit, price, supplier_id, manufacturer_id, category_id,
                    discount_percent, stock_quantity, description, image_path
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    data["article"].strip(),
                    data["name"].strip(),
                    data["unit"].strip(),
                    price,
                    supplier_id,
                    manufacturer_id,
                    category_id,
                    discount,
                    stock,
                    data["description"].strip(),
                    image_path,
                ),
            )
        conn.commit()


def delete_product(product_id: int) -> None:
    with connect() as conn:
        used = conn.execute("SELECT 1 FROM order_items WHERE product_id = ? LIMIT 1", (product_id,)).fetchone()
        if used:
            raise ValueError("Товар присутствует в заказе, удалить его нельзя.")
        product = conn.execute("SELECT image_path FROM products WHERE id = ?", (product_id,)).fetchone()
        conn.execute("DELETE FROM products WHERE id = ?", (product_id,))
        conn.commit()
    if product:
        old_path = Path(product["image_path"])
        if old_path.exists() and old_path.parent == IMAGES_DIR and old_path.name != "picture.png":
            old_path.unlink(missing_ok=True)


def order_table_html(user: dict[str, object]) -> str:
    with connect() as conn:
        orders = conn.execute(
            """
            SELECT orders.id, orders.order_date, orders.delivery_date, orders.pickup_code,
                   pickup_points.address pickup_address, users.full_name client_name,
                   order_statuses.name status_name,
                   GROUP_CONCAT(products.article || ', ' || order_items.quantity, ', ') items
            FROM orders
            JOIN pickup_points ON pickup_points.id = orders.pickup_point_id
            LEFT JOIN users ON users.id = orders.client_id
            JOIN order_statuses ON order_statuses.id = orders.status_id
            LEFT JOIN order_items ON order_items.order_id = orders.id
            LEFT JOIN products ON products.id = order_items.product_id
            GROUP BY orders.id
            ORDER BY orders.id
            """
        ).fetchall()
    can_edit = str(user["role_name"]) == "Администратор"
    add_btn = "<a class='btn' href='/order/new'>Добавить заказ</a>" if can_edit else ""
    rows = ""
    for order in orders:
        actions = ""
        if can_edit:
            actions = f"""
            <a class="btn gray" href="/order/edit?id={order['id']}">Редактировать</a>
            <form method="post" action="/order/delete" style="display:inline" onsubmit="return confirm('Удалить заказ?')">
              <input type="hidden" name="id" value="{order['id']}">
              <button type="submit">Удалить</button>
            </form>
            """
        rows += f"""
        <div class="order-card">
          <div class="order-main">
            <div><strong>Номер заказа:</strong> {order['id']}</div>
            <div><strong>Артикул заказа:</strong> {e(order['items'] or '')}</div>
            <div><strong>Статус заказа:</strong> {e(order['status_name'])}</div>
            <div><strong>Адрес пункта выдачи:</strong> {e(order['pickup_address'])}</div>
            <div><strong>Дата заказа:</strong> {e(order['order_date'])}</div>
            <div><strong>Клиент:</strong> {e(order['client_name'] or '')}</div>
            <div><strong>Код получения:</strong> {e(order['pickup_code'])}</div>
            <div class="row">{actions}</div>
          </div>
          <div class="order-side">
            <div><strong>Дата доставки</strong></div>
            <div style="margin-top:8px">{e(order['delivery_date'])}</div>
          </div>
        </div>
        """
    return nav(user, "Заказы") + f"""
    <div class="panel">{add_btn}</div>
    <div class="orders">{rows}</div>
    """


def render_order_form(user: dict[str, object], order_id: int | None, message: str = "", error: bool = False) -> bytes:
    with connect() as conn:
        order = None
        if order_id is not None:
            order = conn.execute(
                """
                SELECT orders.*, pickup_points.address pickup_address, users.full_name client_name, order_statuses.name status_name
                FROM orders
                JOIN pickup_points ON pickup_points.id = orders.pickup_point_id
                LEFT JOIN users ON users.id = orders.client_id
                JOIN order_statuses ON order_statuses.id = orders.status_id
                WHERE orders.id = ?
                """,
                (order_id,),
            ).fetchone()
        next_id = conn.execute("SELECT COALESCE(MAX(id),0)+1 FROM orders").fetchone()[0]
        next_code = conn.execute("SELECT COALESCE(MAX(CAST(pickup_code AS INTEGER)),900)+1 FROM orders").fetchone()[0]
        statuses = [row["name"].strip() for row in conn.execute("SELECT name FROM order_statuses ORDER BY name").fetchall()]
        points = conn.execute("SELECT id, address FROM pickup_points ORDER BY id").fetchall()
        clients = conn.execute(
            """
            SELECT users.id, users.full_name
            FROM users JOIN roles ON roles.id = users.role_id
            WHERE roles.name = 'Авторизированный клиент'
            ORDER BY users.full_name
            """
        ).fetchall()
        items = ""
        if order is not None:
            parts = conn.execute(
                """
                SELECT products.article, order_items.quantity
                FROM order_items JOIN products ON products.id = order_items.product_id
                WHERE order_items.order_id = ?
                ORDER BY order_items.id
                """,
                (order_id,),
            ).fetchall()
            items = ", ".join(f"{row['article']}, {row['quantity']}" for row in parts)

    status_options = "".join(
        f'<option value="{e(name)}" {"selected" if order and order["status_name"].strip() == name else ""}>{e(name)}</option>'
        for name in statuses
    )
    point_options = "".join(
        f'<option value="{row["id"]}" {"selected" if order and int(order["pickup_point_id"]) == row["id"] else ""}>{e(row["address"])}</option>'
        for row in points
    )
    client_options = '<option value="">Не выбран</option>' + "".join(
        f'<option value="{row["id"]}" {"selected" if order and order["client_id"] == row["id"] else ""}>{e(row["full_name"])}</option>'
        for row in clients
    )
    content = nav(user, "Добавление заказа" if order is None else "Редактирование заказа") + f"""
    <div class="panel">
      <form method="post" action="/order/save">
        <input type="hidden" name="id" value="{e(order['id'] if order else '')}">
        <p><label>Номер заказа<br><input value="{e(order['id'] if order else next_id)}" readonly></label></p>
        <p><label>Артикул<br><input name="items" value="{e(items)}" required></label></p>
        <p><label>Статус заказа<br><select name="status_name" required>{status_options}</select></label></p>
        <p><label>Адрес пункта выдачи<br><select name="pickup_point_id" required>{point_options}</select></label></p>
        <p><label>Дата заказа<br><input name="order_date" value="{e(order['order_date'] if order else datetime.now().strftime('%Y-%m-%d'))}" required></label></p>
        <p><label>Дата выдачи<br><input name="delivery_date" value="{e(order['delivery_date'] if order else datetime.now().strftime('%Y-%m-%d'))}" required></label></p>
        <p><label>Клиент<br><select name="client_id">{client_options}</select></label></p>
        <p><label>Код получения<br><input name="pickup_code" value="{e(order['pickup_code'] if order else next_code)}" required></label></p>
        <div class="row">
          <button type="submit">Сохранить</button>
          <a class="btn gray" href="/orders">Отмена</a>
        </div>
      </form>
    </div>
    """
    return page("Заказ", content, message, error)


def save_order(data: dict[str, str]) -> None:
    datetime.strptime(data["order_date"], "%Y-%m-%d")
    datetime.strptime(data["delivery_date"], "%Y-%m-%d")
    items = parse_items(data["items"])
    with connect() as conn:
        status_id = get_or_create(conn, "order_statuses", data["status_name"].strip())
        order_id = int(data["id"]) if data.get("id", "").strip() else int(conn.execute("SELECT COALESCE(MAX(id),0)+1 FROM orders").fetchone()[0])
        if data.get("id", "").strip():
            conn.execute(
                """
                UPDATE orders
                SET order_date=?, delivery_date=?, pickup_point_id=?, client_id=?, pickup_code=?, status_id=?
                WHERE id=?
                """,
                (
                    data["order_date"],
                    data["delivery_date"],
                    int(data["pickup_point_id"]),
                    int(data["client_id"]) if data.get("client_id", "").strip() else None,
                    data["pickup_code"].strip(),
                    status_id,
                    order_id,
                ),
            )
            conn.execute("DELETE FROM order_items WHERE order_id = ?", (order_id,))
        else:
            conn.execute(
                """
                INSERT INTO orders (id, order_date, delivery_date, pickup_point_id, client_id, pickup_code, status_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order_id,
                    data["order_date"],
                    data["delivery_date"],
                    int(data["pickup_point_id"]),
                    int(data["client_id"]) if data.get("client_id", "").strip() else None,
                    data["pickup_code"].strip(),
                    status_id,
                ),
            )
        for article, qty in items:
            product = conn.execute("SELECT id FROM products WHERE article = ?", (article,)).fetchone()
            if not product:
                raise ValueError(f"Товар с артикулом {article} не найден.")
            conn.execute(
                "INSERT INTO order_items (order_id, product_id, quantity) VALUES (?, ?, ?)",
                (order_id, int(product["id"]), qty),
            )
        conn.commit()


def delete_order(order_id: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM orders WHERE id = ?", (order_id,))
        conn.commit()


class Server(ThreadingMixIn, WSGIServer):
    daemon_threads = True


def app(environ: dict[str, object], start_response):  # type: ignore[no-untyped-def]
    method = str(environ["REQUEST_METHOD"])
    path = str(environ.get("PATH_INFO", "/"))
    query = query_data(environ)
    user = current_user(environ)

    if path == "/":
        return redirect(start_response, "/products" if user else "/login")

    if path == "/login" and method == "GET":
        return respond(start_response, render_login())

    if path == "/login" and method == "POST":
        data = body_data(environ)
        with connect() as conn:
            found = conn.execute(
                """
                SELECT users.id, users.full_name, roles.name role_name
                FROM users JOIN roles ON roles.id = users.role_id
                WHERE users.login = ? AND users.password = ?
                """,
                (data.get("login", "").strip(), data.get("password", "").strip()),
            ).fetchone()
        if not found:
            return respond(start_response, render_login("Неверный логин или пароль.", True))
        return redirect(start_response, "/products", [("Set-Cookie", login_user(found))])

    if path == "/guest":
        guest = {"id": None, "full_name": "Гость", "role_name": "Гость"}
        return redirect(start_response, "/products", [("Set-Cookie", login_user(guest))])

    if path == "/logout":
        return redirect(start_response, "/login", [("Set-Cookie", logout_cookie())])

    if path == "/products":
        body = product_list_html(user, query)
        return respond(start_response, page("Товары", body, query.get("message", ""), bool(query.get("error"))))

    if path == "/product/new":
        if not require(user, {"Администратор"}):
            return respond(start_response, page("Ошибка", "<div class='panel'>Нет доступа.</div>", "Нет доступа.", True), "403 Forbidden")
        return respond(start_response, render_product_form(user, None))

    if path == "/product/edit":
        if not require(user, {"Администратор"}):
            return respond(start_response, page("Ошибка", "<div class='panel'>Нет доступа.</div>", "Нет доступа.", True), "403 Forbidden")
        return respond(start_response, render_product_form(user, int(query.get("id", "0") or 0)))

    if path == "/product/save" and method == "POST":
        if not require(user, {"Администратор"}):
            return respond(start_response, page("Ошибка", "<div class='panel'>Нет доступа.</div>", "Нет доступа.", True), "403 Forbidden")
        data = body_data(environ)
        try:
            save_product(data)
            return redirect(start_response, "/products?message=" + urllib.parse.quote("Товар сохранен."))
        except Exception as error:
            product_id = int(data["id"]) if data.get("id", "").strip() else None
            return respond(start_response, render_product_form(user, product_id, str(error), True))

    if path == "/product/delete" and method == "POST":
        if not require(user, {"Администратор"}):
            return respond(start_response, page("Ошибка", "<div class='panel'>Нет доступа.</div>", "Нет доступа.", True), "403 Forbidden")
        data = body_data(environ)
        try:
            delete_product(int(data["id"]))
            return redirect(start_response, "/products?message=" + urllib.parse.quote("Товар удален."))
        except Exception as error:
            return redirect(start_response, "/products?message=" + urllib.parse.quote(str(error)) + "&error=1")

    if path == "/orders":
        if not require(user, {"Менеджер", "Администратор"}):
            return respond(start_response, page("Ошибка", "<div class='panel'>Нет доступа.</div>", "Нет доступа.", True), "403 Forbidden")
        return respond(start_response, page("Заказы", order_table_html(user), query.get("message", ""), bool(query.get("error"))))

    if path == "/order/new":
        if not require(user, {"Администратор"}):
            return respond(start_response, page("Ошибка", "<div class='panel'>Нет доступа.</div>", "Нет доступа.", True), "403 Forbidden")
        return respond(start_response, render_order_form(user, None))

    if path == "/order/edit":
        if not require(user, {"Администратор"}):
            return respond(start_response, page("Ошибка", "<div class='panel'>Нет доступа.</div>", "Нет доступа.", True), "403 Forbidden")
        return respond(start_response, render_order_form(user, int(query.get("id", "0") or 0)))

    if path == "/order/save" and method == "POST":
        if not require(user, {"Администратор"}):
            return respond(start_response, page("Ошибка", "<div class='panel'>Нет доступа.</div>", "Нет доступа.", True), "403 Forbidden")
        data = body_data(environ)
        try:
            save_order(data)
            return redirect(start_response, "/orders?message=" + urllib.parse.quote("Заказ сохранен."))
        except Exception as error:
            order_id = int(data["id"]) if data.get("id", "").strip() else None
            return respond(start_response, render_order_form(user, order_id, str(error), True))

    if path == "/order/delete" and method == "POST":
        if not require(user, {"Администратор"}):
            return respond(start_response, page("Ошибка", "<div class='panel'>Нет доступа.</div>", "Нет доступа.", True), "403 Forbidden")
        data = body_data(environ)
        delete_order(int(data["id"]))
        return redirect(start_response, "/orders?message=" + urllib.parse.quote("Заказ удален."))

    if path.startswith("/media/"):
        name = urllib.parse.unquote(path.split("/media/", 1)[1])
        for folder in (IMAGES_DIR, IMPORT_DIR):
            file = folder / name
            if file.exists():
                mime = "image/png"
                if file.suffix.lower() in {".jpg", ".jpeg"}:
                    mime = "image/jpeg"
                elif file.suffix.lower() == ".ico":
                    mime = "image/x-icon"
                start_response("200 OK", [("Content-Type", mime)])
                return [file.read_bytes()]
        start_response("404 Not Found", [("Content-Type", "text/plain; charset=utf-8")])
        return [b"not found"]

    return respond(start_response, page("404", "<div class='panel'>Страница не найдена.</div>", "Страница не найдена.", True), "404 Not Found")


def run_server(host: str, port: int, open_browser: bool) -> None:
    url = f"http://{host}:{port}/login"
    print(f"Приложение запущено: {url}")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    with make_server(host, port, app, server_class=Server) as httpd:
        httpd.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--init-db", action="store_true")
    parser.add_argument("--no-server", action="store_true")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    init_db(force=args.init_db)
    if not args.no_server:
        run_server(args.host, args.port, not args.no_browser)


if __name__ == "__main__":
    main()
