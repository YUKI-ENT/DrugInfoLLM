import psycopg2
import requests
import json
import re, ast
import time
from datetime import datetime
from tqdm import tqdm

# --- 設定ファイル読み込み ---
with open("config.json", "r", encoding="utf-8") as f:
    config = json.load(f)
db_conf = config["db"]
ollama_url = config.get("ollama_url", "http://localhost:11434/api/generate")
ollama_model = config.get("ollama_model", "gemma3:12b")
ollama_timeout = config.get("ollama_timeout", 60)
chunk_length = config.get("chunk_length", 3000)
chunk_overlap = config.get("chunk_overlap", 500)
pause_second = config.get("gpu_cooling_wait", 30) #GPU加熱対策。10件処理するごとにこの秒数処理を中断する

# --- ユーザー確認 ---
confirm = input(f"併用情報からデータの抽出を行います。LLMを使うのでかなりの時間がかかりますが、よろしいですか？ (y/n): ")
if confirm.lower() != 'y':
    print("中止しました。")
    exit()

# --- DB接続 ---
conn = psycopg2.connect(**db_conf)
cursor = conn.cursor()

# --- DROP確認プロンプト ---
start_id = 0
progress_file = "progress_interaction.json"
drop_confirm = input("既存の drug_interaction テーブルを削除して作り直しますか？ (Y/n): ").strip().lower()
if drop_confirm == "y":
    print("テーブルを削除して作り直します。")
    cursor.execute("DROP TABLE IF EXISTS drug_interaction")
    cursor.execute("""
    CREATE TABLE drug_interaction (
        id SERIAL PRIMARY KEY,
        id_druginformation INTEGER,
        yj_code VARCHAR(16),
        agent TEXT,
        category TEXT,
        interaction_type TEXT,
        description TEXT,
        AImodel VARCHAR(64),
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    conn.commit()
    print("テーブルを作成しました。")
else:
    print("テーブル削除・再作成をスキップしました。")# ログファイル設定
    # --- 処理再開ポイントの読み込み ---
    process_confirm = input("前回の中断部位から再開しますか？ n:先頭から(Y/n): ").strip().lower()
    if process_confirm != "n":
        try:
            with open(progress_file, "r", encoding="utf-8") as pf:
                saved = json.load(pf)
                start_id = saved.get("last_id", 0)
                print(f"前回の処理位置（id_druginformation={start_id}）から再開します。")
        except FileNotFoundError:
            print("進捗ファイルが見つかりません。最初から処理を開始します。")
    else:
        print("最初から処理を開始します。")

log_file = open("interaction_debug.log", "a", encoding="utf-8")

# 相互作用データの取得
# cursor.execute("SELECT yj_code, content FROM drug_filedata WHERE section_key = 'interactions' LIMIT 100000 OFFSET 1000")
cursor.execute("""
    SELECT id_druginformation, yj_code, content 
    FROM drug_filedata 
    WHERE section_key = 'interactions' AND id_druginformation > %s
    ORDER BY id_druginformation
""", (start_id,))
rows = cursor.fetchall()

# Ollama呼び出し関数
def call_ollama(prompt):
    try:
        response = requests.post(
            ollama_url,
            json={"model": ollama_model, "prompt": prompt, "stream": False},
            timeout=ollama_timeout
        )
        data = response.json()
        raw = data.get("response", "")
        
        return raw
    except Exception as e:
        return str(e)

# コードフェンス除去
# def strip_code_fence(text):
#     return text.strip().removeprefix("```json").removesuffix("```").strip()
def strip_code_fence(text, log_file):
    """
    応答テキストから JSON 配列または Python 構文配列を抽出・パースし、log_file にデバッグ出力。
    """
    # 応答本文ログ出力
    log_file.write("--- strip_code_fence(): 応答本文 ---\n")
    log_file.write(text + "\n")
    log_file.write("-----------------------------------\n")

   # すでにPythonのlist/dictとして渡された場合はそのまま返す
    if isinstance(text, (list, dict)):
        return text

    # 文字列から [] を抽出
    array_matches = re.findall(r"\[[\s\S]*?\]", text)
    for candidate in array_matches:
        try:
            log_file.write(f"✓ JSONとして成功\n")
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass
        try:
            return ast.literal_eval(candidate)
        except Exception:
            continue

    return None

def split_text_safely(text, max_len=3000, overlap=500):
    """
    長文を max_len 文字以内のチャンクに改行単位で安全に分割。
    各チャンクは overlap 文字だけ前のチャンクの末尾と重複させる。

    :param text: 分割対象の文字列
    :param max_len: 各チャンクの最大文字数（デフォルト: 3000）
    :param overlap: チャンク間のオーバーラップ文字数（デフォルト: 500）
    :return: 分割されたチャンクのリスト
    """
    paragraphs = text.split("\n")
    chunks = []
    current_chunk = ""

    for para in paragraphs:
        # 改行 + 1 文字分を見越して余裕を見る
        if len(current_chunk) + len(para) + 1 <= max_len:
            current_chunk += para + "\n"
        else:
            # チャンク完成
            chunks.append(current_chunk.strip())

            # overlap分を残す（改行込みで確保）
            tail = current_chunk[-overlap:] if len(current_chunk) > overlap else current_chunk
            current_chunk = tail + para + "\n"

    if current_chunk.strip():
        chunks.append(current_chunk.strip())

    return chunks

# メイン処理
for idx, (id_druginformation, yj_code, content) in enumerate(tqdm(rows, desc="LLM処理中"), 1):
    chunks = split_text_safely(content, max_len=chunk_length, overlap=chunk_overlap)
    for part_idx, chunk in enumerate(chunks):
        prompt = (
            f"以下は医薬品の「相互作用」に関する記載です（分割{part_idx+1}/{len(chunks)}）。\n"
            f"この文章から、相互作用が記載されているすべての薬剤名（一般名または商品名）と薬効群名を抽出してください。\n\n"
            f"特に、薬効群（例：カテコールアミン製剤、キサンチン系薬剤など）の中に個別薬剤（例：アドレナリン、テオフィリン等）が列挙されている場合は、\n"
            f"カッコ書き内のすべての薬剤名（例：キニジン、パロキセチンなど）をそれぞれ展開し、1つずつ独立した項目として出力してください。\n"
            f"例えば以下のような文章：\n"
            f"「CYP2D6阻害作用を有する薬剤（キニジン、パロキセチン等）」\n"
            f"という記載があれば、以下のように展開して出力してください：\n\n"
            "[\n"
            "  {\n"
            "    \"agent\": \"キニジン\",\n"
            "    \"category\": \"CYP2D6阻害剤\",\n"
            "    \"interaction_type\": \"併用注意\",\n"
            "    \"description\": \"本剤の作用が増強するおそれがあるので、本剤を減量するなど考慮すること。\"\n"
            "  },\n"
            "  {\n"
            "    \"agent\": \"パロキセチン\",\n"
            "    \"category\": \"CYP2D6阻害剤\",\n"
            "    \"interaction_type\": \"併用注意\",\n"
            "    \"description\": \"本剤の作用が増強するおそれがあるので、本剤を減量するなど考慮すること。\"\n"
            "  }\n"
            "]\n\n"
            "このように、括弧内に薬剤名が並んでいる場合は、それぞれを別の JSON オブジェクトとして出力してください。\n\n"
            "以下の形式の JSON 配列で返してください（すべての薬剤について1つずつ）：\n\n"
            "[\n"
            "  {\n"
            "    \"agent\": \"薬剤名または薬効群名（できる限り個別薬剤名）\",\n"
            "    \"category\": \"薬効分類（不明な場合は近い表現）\",\n"
            "    \"interaction_type\": \"併用注意 または 禁忌\",\n"
            "    \"description\": \"相互作用の内容（薬効群名に対する説明を共通で使ってよい）\"\n"
            "  }\n"
            "]\n\n"
            "絶対にフィールド名を変更しないでください。返答は、厳密な JSON 形式（ダブルクォートで囲まれた文字列）でのみ出力してください。\n"
            "シングルクォートや <think> のような説明文は含めないでください。\n\n"
            "以下が添付文書です：\n"
            f"{chunk}"
        )

        # ログにプロンプトを書き込む
        log_file.write("======================================================================================\n")
        log_file.write(f"\n[{datetime.now()}]\n[{yj_code}] (chunk {part_idx+1})\n--- Prompt(model:{ollama_model}) ---\n{prompt}\n")
        # コンソール出力（短縮表示）
        # print(f"\n[{yj_code}] (chunk {part_idx+1})\n--- Prompt(model:{ollama_model}) ---\n{prompt[:150]}\n")
        print(f"[{yj_code}] (chunk {part_idx+1}/{len(chunks)}) ollama({ollama_model})問い合わせ中...")
        
        start = time.time()

        response = call_ollama(prompt)
        response = strip_code_fence(response, log_file)

        elapsed = time.time() - start

        log_file.write(f"--- Response (model: {ollama_model})---\n{response}\n")
        log_file.write(f"[{datetime.now()}] response time: {elapsed:.2f} sec\n")
        print(f"[Response](model: {ollama_model})\n{response}...\n---")
        print(f"[{datetime.now()}] response time: {elapsed:.2f} sec\n")

        try:
            inserted_number = 0
            data = response if isinstance(response, list) else json.loads(response)
            for entry in data:
                try:
                    cursor.execute("""
                        INSERT INTO drug_interaction (id_druginformation, yj_code, agent, category, interaction_type, description, created_at, AImodel)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """, (
                        id_druginformation,
                        yj_code,
                        entry.get("agent", ""),
                        entry.get("category", ""),
                        entry.get("interaction_type", ""),
                        entry.get("description", ""),
                        datetime.now(),
                        ollama_model
                    ))
                    conn.commit()
                    log_file.write(f"--- SQL insert success!  ---\n")
                    inserted_number += 1
                except Exception as insert_err:
                    log_file.write(f"--- SQL INSERT Error ---\n{insert_err}\nentry={entry}\n")
                    print(f"--- SQL INSERT Error ---\n{insert_err}\nentry={entry}\n")
        except Exception as e:
            log_file.write(f"--- JSON Parse Error (chunk {part_idx+1}) ---\n{e}\n{response}\n")
            print(f"--- JSON Parse Error (chunk {part_idx+1}) ---\n{e}\n{response}\n")
        if inserted_number > 0:
            print(f"SQL{inserted_number}件送信成功\n")    
        
    # 🔄 進捗保存
    with open(progress_file, "w", encoding="utf-8") as pf:
        json.dump({"last_id": id_druginformation}, pf)

    # 10件ごとに休止
    if idx % 10 == 0:
        print(f"\n--- {idx}件処理済み、{pause_second}秒休止 ---\n")
        log_file.write(f"\n--- {idx}件処理済み、{pause_second}秒休止 ---\n")
        time.sleep(pause_second)

# 後処理
#conn.commit()
cursor.close()
conn.close()
log_file.close()
