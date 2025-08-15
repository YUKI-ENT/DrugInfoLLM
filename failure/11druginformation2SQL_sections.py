import os
import re
import psycopg2
import json
from tqdm import tqdm

# セクション定義（2017年以降の新書式）
known_sections = [
    ("警告", "warning"),
    ("禁忌", "contraindications"),
    ("効能又は効果", "efficacy"),
    ("効能又は効果に関連する注意", "efficacy_notes"),
    ("用法及び用量", "dosage"),
    ("用法及び用量に関連する注意", "dosage_notes"),
    ("使用上の注意", "precautions"),
    ("重要な基本的注意", "important_notes"),
    ("特定の背景を有する患者に関する注意", "special_patient_notes"),
    ("相互作用", "interactions"),
    ("副作用", "side_effects"),
    ("臨床検査結果に及ぼす影響", "lab_influence"),
    ("過量投与", "overdose"),
    ("適用上の注意", "application_notes"),
    ("その他の注意", "other_notes"),
    ("薬物動態", "pharmacokinetics"),
    ("臨床成績", "clinical_results"),
    ("薬効薬理", "pharmacodynamics"),
    ("有効成分に関する理化学的知見", "compound_properties"),
    ("取扱い上の注意", "handling_notes"),
    ("承認条件", "approval_conditions"),
    ("包装", "packaging"),
    ("主要文献", "main_references"),
    ("文献請求先及び問い合わせ先", "contact_info")
]

# --- 設定ファイル読み込み ---
with open("config.json", "r", encoding="utf-8") as f:
    config = json.load(f)
db_conf = config["db"]

# --- 処理ファイルのパスを指定 ---
source_dir = "./drug_information"

# --- ユーザー確認 ---
confirm = input(f"{source_dir} のデータを SQL サーバー '{db_conf['dbname']}' にアップロードしますが、よろしいですか？ (y/n): ")
if confirm.lower() != 'y':
    print("中止しました。")
    exit()

# --- DB接続 ---
conn = psycopg2.connect(**db_conf)
cursor = conn.cursor()

# テーブル作成
cursor.execute("DROP TABLE IF EXISTS drug_filedata")
cursor.execute("""
    CREATE TABLE drug_filedata (
        id_druginformation SERIAL PRIMARY KEY,
        yj_code VARCHAR(16),
        section_key VARCHAR(50),
        content TEXT,
        content_length INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (yj_code, section_key)
    )
""")
conn.commit()

# セクション分割関数（行単位でスコア判定）
def extract_sections(text):
    lines = text.splitlines()
    section_candidates = {}

    for idx, line in enumerate(lines):
        for jp_title, en_key in known_sections:
            if jp_title in line:
                score = 0
                pos = line.find(jp_title)

                if pos <= 5:
                    score += 3
                if len(line.strip()) <= 12:
                    score += 2
                if sum(1 for other_title, _ in known_sections if other_title != jp_title and other_title in line) == 0:
                    score += 1
                if (idx > 0 and lines[idx - 1].strip() == "") or (idx + 1 < len(lines) and lines[idx + 1].strip() == ""):
                    score += 1

                if en_key not in section_candidates or section_candidates[en_key]['score'] < score:
                    section_candidates[en_key] = {"line_index": idx, "score": score}

    sorted_sections = sorted(
        [(v["line_index"], k) for k, v in section_candidates.items()],
        key=lambda x: x[0]
    )

    sections = {}
    for i, (start_idx, en_key) in enumerate(sorted_sections):
        end_idx = sorted_sections[i + 1][0] if i + 1 < len(sorted_sections) else len(lines)
        content = "\n".join(lines[start_idx:end_idx]).strip()
        sections[en_key] = content

    return sections

# エラーログファイル
errorlog_file = open("drug_information_error.log", "w", encoding="utf-8")

# メイン処理
for filename in tqdm(os.listdir(source_dir), desc="処理中"):
    if not filename.endswith(".txt"):
        continue

    yj_code = filename.replace(".txt", "")
    try:
        with open(os.path.join(source_dir, filename), "r", encoding="euc_jp", errors="replace") as f:
            text = f.read().replace("\t", " ")

        sections = extract_sections(text)
        for en_key, section_text in sections.items():
            content_length = len(section_text)
            cursor.execute(
                """
                INSERT INTO drug_filedata (yj_code, section_key, content, content_length, created_at)
                VALUES (%s, %s, %s, %s, NOW())
                ON CONFLICT (yj_code, section_key) DO NOTHING
                """,
                (yj_code, en_key, section_text, content_length)
            )
    except Exception as file_err:
        errorlog_file.write(f"[{filename}] File error: {file_err}\n")

# 終了処理
conn.commit()
cursor.close()
conn.close()
errorlog_file.close()
