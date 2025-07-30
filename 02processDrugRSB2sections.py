import psycopg2
import re
import pandas as pd
import json
import os

# --- config.jsonの読み込み ---
with open("config.json", "r", encoding="utf-8") as f:
    config = json.load(f)
db_conf = config["db"]

confirm = input("⚠️ 読み込んだRSBデータを modified_info テーブルにセクション分割して保存します。よろしいですか？ (y/n): ")
if confirm.lower() != "y":
    print("処理を中断しました。")
    exit()

conn = psycopg2.connect(**db_conf)
cursor = conn.cursor()

known_sections = [
    ("薬効、効果・効能、適応症", "efficacy"),
    ("薬効備考", "efficacy_notes"),
    ("用法及び用量", "dosage"),
    ("用法及び用量に関連する注意", "dosage_notes"),
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

# --- テーブル作成 ---
cursor.execute("DROP TABLE IF EXISTS modified_info")
create_sql = """
CREATE TABLE modified_info (
    yj_code VARCHAR(16) PRIMARY KEY,
    {}
);
""".format(",\n    ".join([f"{col} TEXT" for _, col in known_sections]))
cursor.execute(create_sql)
conn.commit()

# --- HTML処理関数 ---
def strip_html_tags(text):
    return re.sub(r"<[^>]+>", "", text)

def clean_text(text):
    text = re.sub(r"\t|\r\n|\n", " ", text)
    text = text.replace("\u3000", " ")
    return re.sub(r"\s{2,}", " ", text).strip()

def extract_sections(html):
    html = html.replace("<br>", "\n")
    raw = html
    text = strip_html_tags(html)
    result = {}

    # 【薬効】処理： <b>タグから抽出
#    print("\n--- 薬効抽出デバッグ ---")
#    print(f"[raw全文]（前300文字）: {raw[:300]}")

    efficacy_match = re.search(r"<b>【薬効】[:：]?(.*?)</b>", raw, re.DOTALL | re.IGNORECASE)
    if efficacy_match:
        efficacy = efficacy_match.group(1)
#        print(f"[マッチ成功] efficacy: {efficacy}")

        result["efficacy"] = clean_text(efficacy)

        # efficacy_notes はそれ以降を対象に
        after = raw[efficacy_match.end():]
#        print(f"[afterテキスト]（前300文字）: {after[:300]}")

        after_text = strip_html_tags(after)
#        print(f"[HTML除去後のefficacy_notes]（前300文字）: {after_text[:300]}")

        result["efficacy_notes"] = clean_text(after_text)
    else:
        print(f"[マッチ失敗] <b>【薬効】...<b> の形式が見つかりませんでした: {raw[:100]}")
        result["efficacy"] = ""
        result["efficacy_notes"] = ""

    # その他のセクション
    for j, (jp_section, en_col) in enumerate(known_sections):
        if jp_section in ["薬効、効果・効能、適応症", "薬効備考"]:
            continue
        pattern = r"(\n|^)\s*([0-9]{1,2}\.\s*)?" + re.escape(jp_section) + r"[：:\s]*"
        matches = list(re.finditer(pattern, text, re.IGNORECASE))
        if matches:
            start = matches[0].start()
            end = len(text)
            for next_j in range(j + 1, len(known_sections)):
                next_pattern = r"(\n|^)\s*([0-9]{1,2}\.\s*)?" + re.escape(known_sections[next_j][0]) + r"[：:\s]*"
                next_match = re.search(next_pattern, text[start+1:], re.IGNORECASE)
                if next_match:
                    next_pos = start + 1 + next_match.start()
                    if next_pos < end:
                        end = next_pos
            content = text[start:end]
            content = re.sub(re.escape(jp_section), "", content, flags=re.IGNORECASE)
            result[en_col] = clean_text(content)
    return result

# --- データ読み込みと処理 ---
df = pd.read_sql("SELECT yj_code, info_html FROM drug_RSB", conn)

for _, row in df.iterrows():
    yj_code = row["yj_code"]
    html = row["info_html"]
    sections = extract_sections(html or "")

    insert_cols = ["yj_code"] + list(sections.keys())
    insert_vals = [yj_code] + list(sections.values())
    placeholders = ", ".join(["%s"] * len(insert_vals))
    sql = f"INSERT INTO modified_info ({', '.join(insert_cols)}) VALUES ({placeholders}) ON CONFLICT (yj_code) DO NOTHING"
    cursor.execute(sql, insert_vals)

conn.commit()
cursor.close()
conn.close()
