from pathlib import Path
import shutil


BASE_DIR = Path(__file__).resolve().parent
RESOURCES_DIR = BASE_DIR / "resources"
IMPORT_DIR = RESOURCES_DIR / "import"
IMAGES_DIR = RESOURCES_DIR / "product_images"
DB_PATH = BASE_DIR / "shoe_store.db"
SCHEMA_PATH = BASE_DIR / "schema.sql"

PLACEHOLDER_IMAGE = "picture.png"
MEDIA_PREFIX = "/media/"
LOGIN_PATH = "/login"
PRODUCTS_PATH = "/products"
ORDERS_PATH = "/orders"

FORBIDDEN_STATUS = "403 Forbidden"
FORBIDDEN_BODY = "<div class='panel'>Нет доступа.</div>"
FORBIDDEN_TEXT = "Нет доступа."

PRODUCT_ITEM_MESSAGE_PREFIX = "/products?message="
ORDER_ITEM_MESSAGE_PREFIX = "/orders?message="

ROLE_GUEST = "Гость"
ROLE_CLIENT = "Авторизированный клиент"
ROLE_MANAGER = "Менеджер"
ROLE_ADMIN = "Администратор"
MANAGER_ROLES = {ROLE_MANAGER, ROLE_ADMIN}
ADMIN_ONLY = {ROLE_ADMIN}

LOOKUP_TABLES = {
    "roles",
    "suppliers",
    "manufacturers",
    "categories",
    "order_statuses",
}

BSDTAR_PATH = shutil.which("bsdtar") or "/usr/bin/bsdtar"
SIPS_PATH = shutil.which("sips") or "/usr/bin/sips"
IMAGE_SIZE = "300"
