# init_db.py

from sqlalchemy import create_engine

from db_models import Base, DB_URL_SYNC


def main():
    engine = create_engine(DB_URL_SYNC, echo=True, future=True)
    Base.metadata.create_all(engine)
    print("База данных и таблицы успешно созданы.")


if __name__ == "__main__":
    main()
