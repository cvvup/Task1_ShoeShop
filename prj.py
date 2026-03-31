#!/usr/bin/env python3
from __future__ import annotations

import argparse
import secrets
import sqlite3
import urllib.parse
import webbrowser
from http import cookies
from socketserver import ThreadingMixIn
from typing import Callable
from wsgiref.simple_server import WSGIServer, make_server

from config import (
    ADMIN_ONLY,
    FORBIDDEN_STATUS,
    LOGIN_PATH,
    MANAGER_ROLES,
    MEDIA_PREFIX,
    ORDER_ITEM_MESSAGE_PREFIX,
    ORDERS_PATH,
    PRODUCT_ITEM_MESSAGE_PREFIX,
    PRODUCTS_PATH,
    ROLE_GUEST,
)
from db import connect, delete_order, delete_product, init_db, save_order, save_product
from templates import (
    page,
    product_access_html,
    product_list_html,
    render_login,
    render_order_form,
    render_product_form,
    order_table_html,
)


SESSIONS: dict[str, dict[str, object]] = {}


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
    return {key: values[-1] for key, values in parsed.items()}


def query_data(environ: dict[str, object]) -> dict[str, str]:
    parsed = urllib.parse.parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True)
    return {key: values[-1] for key, values in parsed.items()}


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


def access_denied(start_response):
    return respond(start_response, product_access_html(), FORBIDDEN_STATUS)


def parse_entity_id(data: dict[str, str]) -> int | None:
    if not data.get("id", "").strip():
        return None
    return int(data["id"])


def handle_login_get(start_response):
    return respond(start_response, render_login())


def handle_login_post(environ: dict[str, object], start_response):
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
    return redirect(start_response, PRODUCTS_PATH, [("Set-Cookie", login_user(found))])


def handle_guest_login(start_response):
    guest = {"id": None, "full_name": ROLE_GUEST, "role_name": ROLE_GUEST}
    return redirect(start_response, PRODUCTS_PATH, [("Set-Cookie", login_user(guest))])


def handle_products_page(start_response, user: dict[str, object] | None, query: dict[str, str]):
    body = product_list_html(user, query)
    return respond(start_response, page("Товары", body, query.get("message", ""), bool(query.get("error"))))


def handle_product_form(start_response, user: dict[str, object], query: dict[str, str], product_id: int | None = None):
    if not require(user, ADMIN_ONLY):
        return access_denied(start_response)
    if product_id is None:
        return respond(start_response, render_product_form(user, None))
    return respond(start_response, render_product_form(user, int(query.get("id", "0") or 0)))


def handle_product_save(environ: dict[str, object], start_response, user: dict[str, object]):
    if not require(user, ADMIN_ONLY):
        return access_denied(start_response)
    data = body_data(environ)
    try:
        save_product(data)
        return redirect(start_response, PRODUCT_ITEM_MESSAGE_PREFIX + urllib.parse.quote("Товар сохранен."))
    except (ValueError, TypeError, sqlite3.Error, OSError) as error:
        return respond(start_response, render_product_form(user, parse_entity_id(data), str(error), True))


def handle_product_delete(environ: dict[str, object], start_response, user: dict[str, object]):
    if not require(user, ADMIN_ONLY):
        return access_denied(start_response)
    data = body_data(environ)
    try:
        delete_product(int(data["id"]))
        return redirect(start_response, PRODUCT_ITEM_MESSAGE_PREFIX + urllib.parse.quote("Товар удален."))
    except (ValueError, TypeError, sqlite3.Error, OSError) as error:
        return redirect(start_response, PRODUCT_ITEM_MESSAGE_PREFIX + urllib.parse.quote(str(error)) + "&error=1")


def handle_orders_page(start_response, user: dict[str, object], query: dict[str, str]):
    if not require(user, MANAGER_ROLES):
        return access_denied(start_response)
    return respond(start_response, page("Заказы", order_table_html(user), query.get("message", ""), bool(query.get("error"))))


def handle_order_form(start_response, user: dict[str, object], query: dict[str, str], order_id: int | None = None):
    if not require(user, ADMIN_ONLY):
        return access_denied(start_response)
    if order_id is None:
        return respond(start_response, render_order_form(user, None))
    return respond(start_response, render_order_form(user, int(query.get("id", "0") or 0)))


def handle_order_save(environ: dict[str, object], start_response, user: dict[str, object]):
    if not require(user, ADMIN_ONLY):
        return access_denied(start_response)
    data = body_data(environ)
    try:
        save_order(data)
        return redirect(start_response, ORDER_ITEM_MESSAGE_PREFIX + urllib.parse.quote("Заказ сохранен."))
    except (ValueError, TypeError, sqlite3.Error) as error:
        return respond(start_response, render_order_form(user, parse_entity_id(data), str(error), True))


def handle_order_delete(environ: dict[str, object], start_response, user: dict[str, object]):
    if not require(user, ADMIN_ONLY):
        return access_denied(start_response)
    data = body_data(environ)
    delete_order(int(data["id"]))
    return redirect(start_response, ORDER_ITEM_MESSAGE_PREFIX + urllib.parse.quote("Заказ удален."))


def media_response(start_response, path: str):
    from config import IMAGES_DIR, IMPORT_DIR

    name = urllib.parse.unquote(path.split(MEDIA_PREFIX, 1)[1])
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


def exact_route_response(
    method: str,
    path: str,
    environ: dict[str, object],
    start_response,
    user: dict[str, object] | None,
    query: dict[str, str],
):
    routes: dict[tuple[str, str], Callable[[], list[bytes]]] = {
        ("GET", LOGIN_PATH): lambda: handle_login_get(start_response),
        ("POST", LOGIN_PATH): lambda: handle_login_post(environ, start_response),
        ("GET", "/guest"): lambda: handle_guest_login(start_response),
        ("GET", "/logout"): lambda: redirect(start_response, LOGIN_PATH, [("Set-Cookie", logout_cookie())]),
        ("GET", PRODUCTS_PATH): lambda: handle_products_page(start_response, user, query),
        ("GET", ORDERS_PATH): lambda: handle_orders_page(start_response, user, query),
        ("GET", "/product/new"): lambda: handle_product_form(start_response, user, query),
        ("GET", "/product/edit"): lambda: handle_product_form(start_response, user, query, 1),
        ("POST", "/product/save"): lambda: handle_product_save(environ, start_response, user),
        ("POST", "/product/delete"): lambda: handle_product_delete(environ, start_response, user),
        ("GET", "/order/new"): lambda: handle_order_form(start_response, user, query),
        ("GET", "/order/edit"): lambda: handle_order_form(start_response, user, query, 1),
        ("POST", "/order/save"): lambda: handle_order_save(environ, start_response, user),
        ("POST", "/order/delete"): lambda: handle_order_delete(environ, start_response, user),
    }
    handler = routes.get((method, path))
    return handler() if handler else None


def app(environ: dict[str, object], start_response):  # type: ignore[no-untyped-def]
    method = str(environ["REQUEST_METHOD"])
    path = str(environ.get("PATH_INFO", "/"))
    query = query_data(environ)
    user = current_user(environ)

    if path == "/":
        return redirect(start_response, PRODUCTS_PATH if user else LOGIN_PATH)

    route_response = exact_route_response(method, path, environ, start_response, user, query)
    if route_response is not None:
        return route_response

    if path.startswith(MEDIA_PREFIX):
        return media_response(start_response, path)

    return respond(start_response, page("404", "<div class='panel'>Страница не найдена.</div>", "Страница не найдена.", True), "404 Not Found")


class Server(ThreadingMixIn, WSGIServer):
    daemon_threads = True


def run_server(host: str, port: int, open_browser: bool) -> None:
    url = f"http://{host}:{port}/login"
    print(f"Приложение запущено: {url}")
    if open_browser:
        try:
            webbrowser.open(url)
        except webbrowser.Error:
            print("Не удалось открыть браузер автоматически.")
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
