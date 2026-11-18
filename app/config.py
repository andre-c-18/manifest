import os
from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

load_dotenv()

DB_HOST = os.getenv("DB_HOST")
DB_PORT = os.getenv("DB_PORT")
DB_NAME = os.getenv("DB_NAME")
DB_USER = os.getenv("DB_USER")
DB_PASS = os.getenv("DB_PASS")
DB_TIMEOUT = int(os.getenv("DB_TIMEOUT", "3"))
SECRET_KEY = os.getenv("SECRET_KEY")

UPDATE_INTERVAL_MINUTES = int(os.getenv("UPDATE_INTERVAL_MINUTES"))

APP_HOST = os.getenv("APP_HOST")
APP_PORT = int(os.getenv("APP_PORT"))
APP_DEBUG = os.getenv("APP_DEBUG", "True").lower() == "true"
DEFAULT_TAG = os.getenv("DEFAULT_TAG", "manifest")

ADMIN_USER = os.getenv("ADMIN_USER")
ADMIN_USER_HASH = os.getenv("ADMIN_USER_HASH")

DATABASE_URL = f"mysql+pymysql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

GOOGLE_SHEET_TAMU_CSV_URL = os.getenv("GOOGLE_SHEET_TAMU_CSV_URL")

engine = create_engine(
    DATABASE_URL,
    poolclass=NullPool,
    connect_args={"connect_timeout": DB_TIMEOUT}
)