from __future__ import annotations

import base64
import importlib
import shutil
import sqlite3
import subprocess
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

from config import (
    BASE_DIR,
    BSDTAR_PATH,
    DB_PATH,
    IMAGE_SIZE,
    IMAGES_DIR,
    IMPORT_DIR,
    LOOKUP_TABLES,
    PLACEHOLDER_IMAGE,
    RESOURCES_DIR,
    SCHEMA_PATH,
    SIPS_PATH,
)


try:
    ET = importlib.import_module("defusedxml.ElementTree")
except ImportError:
    ET = importlib.import_module("xml.etree.ElementTree")


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


def col_index(ref: str) -> int:
    col = "".join(ch for ch in ref if ch.isalpha())
    result = 0
    for ch in col:
        result = result * 26 + ord(ch.upper()) - 64
    return result - 1


def shared_strings(zf: zipfile.ZipFile, ns: dict[str, str]) -> list[str]:
    if "xl/sharedStrings.xml" not in zf.namelist():
        return []
    root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
    result: list[str] = []
    for item in root.findall("main:si", ns):
        result.append("".join(node.text or "" for node in item.findall(".//main:t", ns)))
    return result


def workbook_sheet_xml(zf: zipfile.ZipFile, ns: dict[str, str]):
    workbook = ET.fromstring(zf.read("xl/workbook.xml"))
    rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    rel_map = {rel.attrib["Id"]: rel.attrib["Target"] for rel in rels}
    sheet = workbook.find("main:sheets", ns)[0]
    rid = sheet.attrib["{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"]
    return ET.fromstring(zf.read("xl/" + rel_map[rid]))


def cell_value(cell, ns: dict[str, str], shared: list[str]) -> str:
    cell_type = cell.attrib.get("t")
    if cell_type == "inlineStr":
        return "".join(node.text or "" for node in cell.findall(".//main:t", ns)).strip()
    node = cell.find("main:v", ns)
    if node is None:
        return ""
    raw = (node.text or "").strip()
    if cell_type == "s" and raw.isdigit():
        return shared[int(raw)].strip()
    return raw


def row_values(row, ns: dict[str, str], shared: list[str]) -> list[str]:
    cells: dict[int, str] = {}
    max_idx = -1
    for cell in row.findall("main:c", ns):
        idx = col_index(cell.attrib.get("r", "A1"))
        max_idx = max(max_idx, idx)
        cells[idx] = cell_value(cell, ns, shared)
    return [cells.get(i, "") for i in range(max_idx + 1)]


def xlsx_rows(path: Path) -> list[list[str]]:
    ns = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    rows: list[list[str]] = []
    with zipfile.ZipFile(path) as zf:
        shared = shared_strings(zf, ns)
        sheet_xml = workbook_sheet_xml(zf, ns)
        for row in sheet_xml.findall(".//main:sheetData/main:row", ns):
            result = row_values(row, ns, shared)
            if any(part.strip() for part in result):
                rows.append(result)
    return rows


def copy_placeholder_image() -> None:
    placeholder = IMPORT_DIR / PLACEHOLDER_IMAGE
    target = IMAGES_DIR / PLACEHOLDER_IMAGE
    if placeholder.exists() and not target.exists():
        shutil.copy2(placeholder, target)


def convert_import_images() -> None:
    for file in IMPORT_DIR.glob("*.jpg"):
        target = IMAGES_DIR / f"{file.stem}.png"
        if target.exists():
            continue
        subprocess.run(
            [SIPS_PATH, "-Z", IMAGE_SIZE, "-s", "format", "png", str(file), "--out", str(target)],
            check=True,
            capture_output=True,
            text=True,
        )


def ensure_resources() -> None:
    IMPORT_DIR.mkdir(parents=True, exist_ok=True)
    if not (IMPORT_DIR / "Tovar.xlsx").exists():
        archive = next(Path.home().joinpath("Downloads").glob("Прил_2_ОЗ_КОД*.rar"), None)
        if archive is None:
            raise FileNotFoundError("Не найден архив с ресурсами в Downloads.")
        subprocess.run([BSDTAR_PATH, "-xf", str(archive), "-C", str(RESOURCES_DIR)], check=True)
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    copy_placeholder_image()
    convert_import_images()


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_or_create(conn: sqlite3.Connection, table: str, name: str) -> int:
    if table not in LOOKUP_TABLES:
        raise ValueError(f"Недопустимая таблица: {table}")
    row = conn.execute(f"SELECT id FROM {table} WHERE name = ?", (name,)).fetchone()
    if row:
        return int(row["id"])
    return int(conn.execute(f"INSERT INTO {table} (name) VALUES (?)", (name,)).lastrowid)


def import_users(conn: sqlite3.Connection) -> None:
    for row in xlsx_rows(IMPORT_DIR / "user_import.xlsx")[1:]:
        if len(row) < 4 or not row[2]:
            continue
        role_id = get_or_create(conn, "roles", row[0].strip())
        conn.execute(
            "INSERT INTO users (full_name, login, password, role_id) VALUES (?, ?, ?, ?)",
            (row[1].strip(), row[2].strip(), row[3].strip(), role_id),
        )


def product_image_path(image_name: str) -> str:
    if not image_name.strip():
        return str(IMAGES_DIR / PLACEHOLDER_IMAGE)
    candidate = IMAGES_DIR / f"{Path(image_name).stem}.png"
    if candidate.exists():
        return str(candidate)
    return str(IMAGES_DIR / PLACEHOLDER_IMAGE)


def import_products(conn: sqlite3.Connection) -> None:
    for row in xlsx_rows(IMPORT_DIR / "Tovar.xlsx")[1:]:
        if len(row) < 11 or not row[0]:
            continue
        supplier_id = get_or_create(conn, "suppliers", row[4].strip())
        manufacturer_id = get_or_create(conn, "manufacturers", row[5].strip())
        category_id = get_or_create(conn, "categories", row[6].strip())
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
                product_image_path(row[10]),
            ),
        )


def import_pickup_points(conn: sqlite3.Connection) -> None:
    for row in xlsx_rows(IMPORT_DIR / "Пункты выдачи_import.xlsx"):
        if row and row[0].strip():
            conn.execute("INSERT INTO pickup_points (address) VALUES (?)", (row[0].strip(),))


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


def import_orders(conn: sqlite3.Connection) -> None:
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


def init_db(force: bool = False) -> None:
    ensure_resources()
    if force and DB_PATH.exists():
        DB_PATH.unlink()
    if DB_PATH.exists():
        return
    with connect() as conn:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        import_users(conn)
        import_products(conn)
        import_pickup_points(conn)
        import_orders(conn)
        conn.commit()


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
    image_path = new_image or data.get("old_image_path", str(IMAGES_DIR / PLACEHOLDER_IMAGE))
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
                if old_path.exists() and old_path.parent == IMAGES_DIR and old_path.name != PLACEHOLDER_IMAGE:
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
        if old_path.exists() and old_path.parent == IMAGES_DIR and old_path.name != PLACEHOLDER_IMAGE:
            old_path.unlink(missing_ok=True)


def order_form_data(order_id: int | None):
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
    return order, next_id, next_code, statuses, points, clients, items


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
