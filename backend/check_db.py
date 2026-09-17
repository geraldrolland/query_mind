import psycopg2

conn = psycopg2.connect('postgresql://postgres:querymind123@localhost:5433/querymind')
cur = conn.cursor()

cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='message' ORDER BY ordinal_position")
print('message columns:', [r[0] for r in cur.fetchall()])

cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='dataset' ORDER BY ordinal_position")
print('dataset columns:', [r[0] for r in cur.fetchall()])

cur.execute("SELECT * FROM alembic_version")
print('alembic_version:', cur.fetchall())

cur.execute("SELECT * FROM message LIMIT 1")
print('message data sample:', cur.fetchall())

conn.close()