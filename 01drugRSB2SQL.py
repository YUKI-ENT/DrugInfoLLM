# 設定ファイル読み込み
with open("config.json", "r", encoding="utf-8") as f:
    config = json.load(f)

db_conf = config["db"]

# 管理DB用設定を drug_info 設定から派生させる（dbnameだけ変更）
admin_conf = db_conf.copy()
admin_conf["dbname"] = "postgres"

file_path = "drug_RSB.dat"

# PostgreSQL管理DBに接続して drug_info 作成
admin_conn = psycopg2.connect(**admin_conf)
admin_conn.autocommit = True
admin_cur = admin_conn.cursor()

admin_cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", ("drug_info",))
if not admin_cur.fetchone():
    print("データベース 'drug_info' を作成します。")
    admin_cur.execute("""
        CREATE DATABASE drug_info
        WITH ENCODING = 'UTF8' LC_COLLATE='C' LC_CTYPE='C' TEMPLATE=template0
    """)
else:
    print("データベース 'drug_info' はすでに存在します。")

admin_cur.close()
admin_conn.close()

# drug_info に接続
conn = psycopg2.connect(**db_conf)
cursor = conn.cursor()

# テーブル作成（なければ）
cursor.execute("""
CREATE TABLE IF NOT EXISTS drug_RSB (
    drug_name       VARCHAR(128),
    price           NUMERIC,
    manufacturer    VARCHAR(128),
    generic_name    VARCHAR(128),
    unit            VARCHAR(32),
    generic         VARCHAR(8),
    info_html       TEXT,
    concomitant     TEXT,
    yj_code         VARCHAR(16) PRIMARY KEY,
    kana_name       VARCHAR(128)
)
""")
conn.commit()

# ユーザー確認
ans = input(f"\n'{file_path}' のデータをSQLサーバーにアップロードしますか？ [Y/n]: ").strip().lower()
if ans not in ("", "y", "yes"):
    print("アップロード処理を中止しました。")
    exit()

# データ読み込み & 登録
with open(file_path, "r", encoding="euc_jp", errors="replace") as f:
    reader = csv.reader(f)
    for i, row in enumerate(reader, start=1):
        row = [col.replace("\t", " ") for col in row]
        if len(row) != 10:
            print(f"[行 {i}] 列数エラー ({len(row)}列): {row}")
            continue
        try:
            cursor.execute("""
                INSERT INTO drug_RSB (
                    drug_name, price, manufacturer, generic_name, unit,
                    generic, info_html, concomitant, yj_code, kana_name
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (yj_code) DO NOTHING
            """, row)
            conn.commit()
            print(f"[行 {i}] 成功: {row[0]}")
        except Exception as e:
            conn.rollback()
            print(f"[行 {i}] エラー: {e}\nデータ: {row}")

cursor.close()
conn.close()
