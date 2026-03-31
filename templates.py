from __future__ import annotations

import html
import sqlite3
import urllib.parse
from datetime import datetime
from pathlib import Path

from config import (
    ADMIN_ONLY,
    FORBIDDEN_BODY,
    FORBIDDEN_TEXT,
    IMAGES_DIR,
    MANAGER_ROLES,
    MEDIA_PREFIX,
    PLACEHOLDER_IMAGE,
    ROLE_ADMIN,
    ROLE_GUEST,
)
from db import connect, order_form_data


def e(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


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


def product_access_html() -> bytes:
    return page("Ошибка", FORBIDDEN_BODY, FORBIDDEN_TEXT, True)


def nav(user: dict[str, object] | None, title: str) -> str:
    if not user:
        return f'<div class="top"><h1 style="margin:0">{e(title)}</h1></div>'
    orders_btn = ""
    if str(user["role_name"]) in MANAGER_ROLES:
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
    return MEDIA_PREFIX + urllib.parse.quote(Path(path).name)


def filter_products(products: list[sqlite3.Row], search: str) -> list[sqlite3.Row]:
    words = [word.casefold() for word in search.strip().split() if word]
    if not words:
        return products
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
    return filtered_products


def supplier_options(suppliers: list[str], supplier: str) -> str:
    options = '<option value="Все поставщики">Все поставщики</option>'
    for item in suppliers:
        selected = "selected" if item == supplier else ""
        options += f'<option value="{e(item)}" {selected}>{e(item)}</option>'
    return options


def product_price_html(product: sqlite3.Row) -> str:
    if int(product["discount_percent"]) <= 0:
        return f"{float(product['price']):.2f} руб."
    final_price = float(product["price"]) * (100 - int(product["discount_percent"])) / 100
    return (
        f'<span class="old-price">{float(product["price"]):.2f} руб.</span>'
        f'<span class="new-price">{final_price:.2f} руб.</span>'
    )


def product_actions_html(product: sqlite3.Row, can_edit: bool) -> str:
    if not can_edit:
        return ""
    return f"""
    <div class="row">
      <a class="btn gray" href="/product/edit?id={product['id']}">Редактировать</a>
      <form method="post" action="/product/delete" onsubmit="return confirm('Удалить товар?')" style="margin:0">
        <input type="hidden" name="id" value="{product['id']}">
        <button type="submit">Удалить</button>
      </form>
    </div>
    """


def product_card_html(product: sqlite3.Row, can_edit: bool) -> str:
    discount_cls = "discount high" if int(product["discount_percent"]) > 15 else "discount"
    card_cls = "card out-of-stock" if int(product["stock_quantity"]) == 0 else "card"
    return f"""
    <div class="{card_cls}">
      <div class="grid">
        <div><img class="product" src="{product_image_url(product['image_path'])}" alt=""></div>
        <div>
          <h2 style="margin-top:0">{e(product['name'])} ({e(product['article'])})</h2>
          <div>Категория: {e(product['category_name'])}</div>
          <div>Производитель: {e(product['manufacturer_name'])}</div>
          <div>Поставщик: {e(product['supplier_name'])}</div>
          <div>Ед. изм.: {e(product['unit'])}</div>
          <div>Цена: {product_price_html(product)}</div>
          <div>Количество на складе: {e(product['stock_quantity'])}</div>
          <div class="{discount_cls}">Скидка: {e(product['discount_percent'])}%</div>
          <p>{e(product['description'])}</p>
          {product_actions_html(product, can_edit)}
        </div>
      </div>
    </div>
    """


def product_filters_html(search: str, supplier: str, sort: str, can_edit: bool, suppliers: list[str]) -> str:
    options = supplier_options(suppliers, supplier)
    add_product_btn = "<div><br><a class='btn' href='/product/new'>Добавить товар</a></div>" if can_edit else ""
    return f"""
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
          {add_product_btn}
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


def product_list_html(user: dict[str, object] | None, query: dict[str, str]) -> str:
    role = str(user["role_name"]) if user else ROLE_GUEST
    search = query.get("search", "")
    supplier = query.get("supplier", "")
    sort = query.get("sort", "")
    can_filter = role in MANAGER_ROLES
    can_edit = role == ROLE_ADMIN
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
    if can_filter:
        products = filter_products(products, search)
    if can_filter:
        filters = product_filters_html(search, supplier, sort, can_edit, suppliers)
    elif can_edit:
        filters = '<div class="panel"><a class="btn" href="/product/new">Добавить товар</a></div>'
    else:
        filters = ""
    cards = "".join(product_card_html(product, can_edit) for product in products)
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
        <input type="hidden" name="old_image_path" value="{e(product['image_path'] if product else str(IMAGES_DIR / PLACEHOLDER_IMAGE))}">
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
    can_edit = str(user["role_name"]) == ROLE_ADMIN
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
    order, next_id, next_code, statuses, points, clients, items = order_form_data(order_id)
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
