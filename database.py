from sqlalchemy import create_engine, inspect


def create_engine_from_url(url: str):
    return create_engine(url, future=True)


def get_schema(url: str):
    engine = create_engine_from_url(url)
    insp = inspect(engine)
    out = {}
    for table in insp.get_table_names():
        out[table] = [{"name": c["name"], "type": str(c["type"])} for c in insp.get_columns(table)]
    return out
