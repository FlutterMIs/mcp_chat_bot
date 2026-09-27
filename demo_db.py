from sqlalchemy import create_engine, text

def create_demo_db(url="sqlite:///demo.db"):
    e=create_engine(url)
    with e.begin() as c:
        c.execute(text("DROP TABLE IF EXISTS sales"))
        c.execute(text("CREATE TABLE sales (id INTEGER, sale_date TEXT, city TEXT, category TEXT, amount REAL, qty REAL)"))
        rows=[(1,'2026-01-05','Delhi','Electronics',12000,2),(2,'2026-01-08','Patna','Furniture',8500,1),(3,'2026-02-10','Delhi','Electronics',15000,3),(4,'2026-03-12','Mumbai','Furniture',9200,2),(5,'2026-04-03','Patna','Electronics',18000,4),(6,'2026-05-11','Delhi','Furniture',11000,2)]
        for r in rows: c.execute(text("INSERT INTO sales VALUES (:id,:d,:city,:cat,:amount,:qty)"),dict(zip(['id','d','city','cat','amount','qty'],r)))
