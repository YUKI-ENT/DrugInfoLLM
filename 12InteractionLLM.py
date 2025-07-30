import psycopg2
import requests
import json
from datetime import datetime
from tqdm import tqdm

# --- 設定ファイル読み込み ---
with open("config.json", "r", encoding="utf-8") as f:
    config = json.load(f)
db_conf = config["db"]

# --- ユーザー確認 ---
confirm = input(f"併用情報からデータの抽出を行います。LLMを使うのでかなりの時間がかかりますが、よろしいですか？ (y/n): ")
if confirm.lower() != 'y':
    print("中止しました。")
    exit()

# --- DB接続 ---
conn = psycopg2.connect(**db_conf)
cursor = conn.cursor()

# --- DROP確認プロンプト ---
drop_confirm = input("既存の drug_interaction テーブルを削除して作り直しますか？ (Y/n): ").strip().lower()
if drop_confirm == "y":
    print("テーブルを削除して作り直します。")
    cursor.execute("DROP TABLE IF EXISTS drug_interaction")
    cursor.execute("""
    CREATE TABLE drug_interaction (
        id SERIAL PRIMARY KEY,
        yj_code VARCHAR(16),
        agent TEXT,
        category TEXT,
        interaction_type TEXT,
        description TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    conn.commit()
    print("テーブルを作成しました。")
else:
    print("テーブル削除・再作成をスキップしました。")# ログファイル設定

log_file = open("interaction_debug.log", "w", encoding="utf-8")


# 相互作用データの取得
cursor.execute("SELECT yj_code, content FROM drug_filedata WHERE section_key = 'interactions' LIMIT 100000 OFFSET 1000")
rows = cursor.fetchall()

# Ollama呼び出し関数
def call_ollama(prompt):
    try:
        response = requests.post(
            "http://localhost:11434/api/generate",
            json={"model": "gemma3:12b", "prompt": prompt, "stream": False},
            timeout=60
        )
        data = response.json()
        raw = data.get("response", "")
        # コンソール出力（短縮表示）
        print(f"\n[Prompt] ({len(prompt)} chars)\n{prompt[:100]}...\n---")
        print(f"[Response]\n{raw}...\n---")
        return raw
    except Exception as e:
        return str(e)

# コードフェンス除去
def strip_code_fence(text):
    return text.strip().removeprefix("```json").removesuffix("```").strip()

# メイン処理
for yj_code, content in tqdm(rows, desc="LLM処理中"):
    prompt = (
        f"以下は医薬品の「相互作用」に関する記載です。\n"
        f"この文章から相互作用が記載されているすべての薬剤または薬効群名（例：マクロライド系抗生物質、CYP3A阻害薬）を抽出してください。\n\n"
        f"次の形式のJSON配列で返してください（すべての薬剤について個別に）：\n\n"
        "[\n"
        "  {\n"
        "    \"agent\": \"薬剤名または薬効群名\",\n"
        "    \"category\": \"薬効分類（不明な場合は近い表現）\",\n"
        "    \"interaction_type\": \"併用注意 または 禁忌\",\n"
        "    \"description\": \"相互作用の内容\"\n"
        "  }\n"
        "]\n\n"
        "できるだけJSON形式に誤りがないよう、厳密に出力してください。"
        f"{content[:3500]}"
    )
    # ログにプロンプトを書き込む
    log_file.write(f"\n[{yj_code}]\n--- Prompt ---\n{prompt}\n")

    response = call_ollama(prompt)
    response = strip_code_fence(response)
    
    log_file.write(f"--- Response ---\n{response}\n")

    try:
        data = json.loads(response)
        for entry in data:
            try:
                cursor.execute("""
                    INSERT INTO drug_interaction (yj_code, agent, category, interaction_type, description, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (
                    yj_code,
                    entry.get("agent", ""),
                    entry.get("category", ""),
                    entry.get("interaction_type", ""),
                    entry.get("description", ""),
                    datetime.now()
                ))
                conn.commit()
                log_file.write(f"--- SQL insert success!  ---\n")
            except Exception as insert_err:
                log_file.write(f"--- SQL INSERT Error ---\n{insert_err}\nentry={entry}\n")
    except Exception as e:
        log_file.write(f"--- JSON Parse Error ---\n{e}\n")

# 後処理
#conn.commit()
cursor.close()
conn.close()
log_file.close()
