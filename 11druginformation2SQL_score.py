# -*- coding: utf-8 -*-
# ルールベースで添付文書テキストをセクション分割して drug_filedata に投入
# - EUC-JP固定読み込み（タブ→スペース）
# - 各行に対し見出し語辞書でスコアリング→セクションごとの最良行をアンカーに採用
# - 同一行で「効能／用法」が併記される場合はブロック内をヒューリスティックで二分
#   -> 分割点が見つからない場合は、用法に効能本文を複製（空欄回避）
# - 改行込みオフセットでCRLFズレ回避
# - 進捗表示＆確認プロンプト、pause_every_n_files/gpu_cooling_wait対応
# - UPSERT（同一 yj_code, section_key は上書き）

import os
import re
import json
import time
import psycopg2
from tqdm import tqdm
from datetime import datetime

# ===================== 設定 =====================
with open("config.json", "r", encoding="utf-8") as f:
    config = json.load(f)

db_conf  = config["db"]
SOURCE_DIR = config.get("DI_folder") or "./drug_information"
PAUSE_EVERY = int(config.get("pause_every_n_files", 10))
MIN_HEADING_SCORE = float(config.get("min_heading_score", 5.0))  # 見出し採用の下限
HEADING_LOG_PATH = config.get("heading_log_file", "11heading_detect.log")
LOG_CANDIDATES   = bool(config.get("log_candidates", True))
LOG_MIN_SCORE    = float(config.get("log_candidates_min_score", 0.0))
LOG_MAX_LINES    = int(config.get("log_candidates_max_lines", 300))
# ===================== セクション定義 =====================
SECTION_KEYS = [
    "warning","contraindications","efficacy","efficacy_notes","dosage","dosage_notes",
    "precautions","important_notes","special_patient_notes","interactions","side_effects",
    "lab_influence","overdose","application_notes","other_notes","pharmacokinetics",
    "clinical_results","pharmacodynamics","compound_properties","handling_notes",
    "approval_conditions","packaging","main_references","contact_info"
]

# 表記ゆれ辞書（必要に応じて追記）
ALIASES = {
    "warning": ["警告"],
    "contraindications": ["禁忌","禁忌（次の患者には投与しないこと）"],
    "efficacy": ["効能又は効果","効能・効果","効能効果","効能及び効果"],
    "efficacy_notes": ["効能又は効果に関連する注意","効能又は効果に関する注意"],
    "dosage": ["用法及び用量","用法・用量","用法、用量","用量及び用法"],
    "dosage_notes": ["用法及び用量に関連する注意","用法及び用量に関する注意","用法及び用量に関連する使用上の注意"],
    "precautions": ["使用上の注意"],
    "important_notes": ["重要な基本的注意"],
    "special_patient_notes": ["特定の背景を有する患者に関する注意"],
    "interactions": ["相互作用"],
    "side_effects": ["副作用","その他の副作用","重大な副作用"],
    "lab_influence": ["臨床検査結果に及ぼす影響"],
    "overdose": ["過量投与"],
    "application_notes": ["適用上の注意"],
    "other_notes": ["その他の注意"],
    "pharmacokinetics": ["薬物動態","薬物動態パラメータ"],
    "clinical_results": ["臨床成績"],
    "pharmacodynamics": ["薬効薬理"],
    "compound_properties": ["有効成分に関する理化学的知見"],
    "handling_notes": ["取扱い上の注意"],
    "approval_conditions": ["承認条件"],
    "packaging": ["包装"],
    "main_references": ["主要文献","主要文献及び文献請求先"],
    "contact_info": ["文献請求先","文献請求先及び問い合わせ先","製造販売業者等の氏名又は名称及び所在地","主要文献及び文献請求先"],
}

# 併記見出しの時に「用法」の先頭として認識しやすい行のパターン（分割点候補）
DOSAGE_START = re.compile(
    r"(?m)^(?:[ 　]*"
    r"(?:錠|ドライシロップ|カプセル|散|内用液|坐剤|注|吸入|貼付|懸濁|シロップ)\b"
    r"|[ 　]*(?:通常|用法|投与|経口|静注|点滴|分(?:(?:\s*|[ 　]*)[0-9０-９]+)|回|mg|g|mL)\b)"
)

# 行頭の装飾・番号（*, ※, 1., 1) 等）を剥がす
LEAD_NOISE = re.compile(r"^\s*(?:[*※＊]?\s*)?(?:[0-9０-９]+(?:\.[0-9０-９]+)*[.)]?\s*)?")

# セクション優先度（同一行に複数検知したときの代表）
SECTION_PRIORITY = [
    "warning","contraindications","efficacy","dosage",
    "precautions","important_notes","special_patient_notes","interactions",
    "side_effects","lab_influence","overdose","application_notes","other_notes",
    "pharmacokinetics","clinical_results","pharmacodynamics","compound_properties",
    "handling_notes","approval_conditions","packaging","main_references","contact_info"
]

def _shorten(s: str, n: int = 120) -> str:
    if s is None: return ""
    s = s.replace("\r", "").replace("\n", " ")
    return s if len(s) <= n else s[:n] + f"...(+{len(s)-n})"

def write_heading_log(logf, filename: str, lines: list, bucket_by_line: dict, anchors: dict):
    """
    bucket_by_line: {line_idx: {section_key: score, ...}, ...}
    anchors: {section_key: line_idx}
    """
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logf.write("====================================================================================================\n")
    logf.write(f"[{ts}] file={filename} total_lines={len(lines)} min_score={LOG_MIN_SCORE}\n")

    # 候補行ログ（スコア付き）
    if LOG_CANDIDATES and bucket_by_line:
        logf.write("[CANDIDATES] (idx: text :: key:score,...)\n")
        count = 0
        for idx in sorted(bucket_by_line.keys()):
            kv = {k:v for k,v in bucket_by_line[idx].items() if v >= LOG_MIN_SCORE}
            if not kv:
                continue
            # スコア降順で並べる
            items = sorted(kv.items(), key=lambda x: (-x[1], x[0]))
            pairs = ", ".join([f"{k}:{x:.2f}" for k,x in items])
            logf.write(f"  [{idx:>5}] {_shorten(lines[idx])} :: {pairs}\n")
            count += 1
            if count >= LOG_MAX_LINES:
                logf.write(f"  ... (truncated at {LOG_MAX_LINES} candidates)\n")
                break

    # 採用アンカー（セクション優先度順）
    logf.write("[ANCHORS]\n")
    for key in SECTION_PRIORITY:
        if key in anchors:
            li = anchors[key]
            score = bucket_by_line.get(li, {}).get(key, None)
            score_str = f"{score:.2f}" if isinstance(score, (int,float)) else "-"
            logf.write(f"  {key:<22} -> line {li:<5} score {score_str} | {_shorten(lines[li])}\n")

    # 同一行に複数キーが乗っている箇所のレポート
    rev = {}
    for k, li in anchors.items():
        rev.setdefault(li, []).append(k)
    multi = [(li, keys) for li, keys in rev.items() if len(keys) >= 2]
    if multi:
        logf.write("[MULTI-KEY-LINE]\n")
        for li, keys in sorted(multi, key=lambda x: x[0]):
            logf.write(f"  line {li:<5} keys={keys} | {_shorten(lines[li])}\n")

    logf.write("====================================================================================================\n\n")
    logf.flush()

def read_text_euc(path: str) -> str:
    with open(path, "r", encoding="euc_jp", errors="replace") as f:
        return f.read().replace("\t", " ")

def calc_line_score(line: str, idx: int, lines: list) -> dict:
    """行に対して各セクションのスコアを返す {section_key: score}"""
    raw = line.rstrip("\r\n")
    s = LEAD_NOISE.sub("", raw).strip()
    if not s:
        return {}
    scores = {}
    # 基本的な特徴量
    begins = (len(raw) - len(raw.lstrip()))  # 行頭空白
    length = len(s)
    has_period = "。" in s

    for key, variants in ALIASES.items():
        local_score = 0.0
        hit_variant = None
        for v in variants:
            if s == v:
                local_score = max(local_score, 8.0)  # 完全一致 強い
                hit_variant = v
            elif v in s:
                local_score = max(local_score, 5.0)  # 含む
                if not hit_variant:
                    hit_variant = v

        if local_score == 0.0:
            continue

        # 追加ボーナス/減点
        if begins <= 4:
            local_score += 2.0
        if length <= 20:
            local_score += 1.5
        elif length <= 35:
            local_score += 0.8
        if not has_period:
            local_score += 0.5

        # 前後が空行
        prev_empty = (idx-1 >= 0 and lines[idx-1].strip() == "")
        next_empty = (idx+1 < len(lines) and lines[idx+1].strip() == "")
        if prev_empty or next_empty:
            local_score += 0.5

        # 括弧/【】で囲われている
        if raw.strip().startswith(("【","[")) or raw.strip().endswith(("】","]")):
            local_score += 0.5

        # 合体見出しの特別扱い（主要文献・文献請求先）
        if hit_variant and ("主要文献" in hit_variant and "文献請求先" in hit_variant):
            if key == "main_references":
                local_score += 0.5
            if key == "contact_info":
                local_score += 0.5

        scores[key] = local_score

    return scores

def choose_best_anchors(lines: list) -> dict:
    """
    各セクションについてスコア最大の行を1つ選ぶ。
    同一行に複数セクションが高得点で出ることは許容（efficacy/dosage, main_references/contact_info など）
    return: {section_key: line_index}
    """
    best = {}        # key -> (score, idx)
    bucket_by_line = {}  # idx -> {key:score}

    for idx, line in enumerate(lines):
        sc = calc_line_score(line, idx, lines)
        if not sc:
            continue
        bucket_by_line[idx] = sc
        for key, val in sc.items():
            if val < MIN_HEADING_SCORE:
                continue
            if key not in best or val > best[key][0] or (val == best[key][0] and idx < best[key][1]):
                best[key] = (val, idx)

    # 同一点（同じ行）に複数キーが載るのはそのまま許容
    anchors = {key: idx for key, (score, idx) in best.items()}
    return anchors, bucket_by_line

def make_offsets(text: str):
    """改行込みで絶対位置配列を作成"""
    raw_lines = text.splitlines(True)  # 改行保持
    lines     = [ln.rstrip("\r\n") for ln in raw_lines]
    starts, off = [], 0
    for ln in raw_lines:
        starts.append(off)
        off += len(ln)
    return lines, raw_lines, starts, len(text)

def slice_sections(text: str,
                   anchors: dict,
                   lines, raw_lines, starts, text_len: int,
                   bucket_by_line: dict,
                   heading_logf=None, filename:str=""):
    """
    anchors: {section_key: line_idx}
    bucket_by_line: {line_idx: {section_key: score, ...}}
    - 「効能」→直後に「効能／用法」併記行が来るケースを bridge。
      * 効能 = [E_line .. (併記ブロックの用法開始直前)]
      * 用法 = [併記ブロックの用法開始 .. 次アンカー直前]
      * 二分不可なら 用法 = 効能（複製）
    """
    # line順に並べる
    order = sorted([(idx, key) for key, idx in anchors.items()], key=lambda x: x[0])
    if not order:
        return {}

    # 次アンカーの絶対位置（line_idx -> abs）
    line_to_next_abs = {}
    for i, (li, key) in enumerate(order):
        next_abs = text_len if i+1 == len(order) else starts[order[i+1][0]]
        line_to_next_abs[li] = next_abs

    sections = {}
    skip_lines = set()   # ここに入れた line は通常処理をスキップ（併記を個別に処理するため）

    # ---------- BRIDGING: 「効能」→すぐ下に「効能／用法」併記 ----------
    # 併記候補となる行を抽出（同じ行で efficacy と dosage のスコアが出ている）
    combined_lines = {li for li, m in bucket_by_line.items()
                      if ("efficacy" in m) and ("dosage" in m)}
    # 効能アンカー行
    e_line = anchors.get("efficacy")
    d_line = anchors.get("dosage")

    def _log(msg):
        if heading_logf:
            heading_logf.write(f"[BRIDGE] {filename}: {msg}\n")

    if e_line is not None and d_line is not None and d_line in combined_lines and e_line < d_line:
        # 併記ブロック: d_line 〜 次アンカー直前
        block_start_abs = starts[d_line]
        block_end_abs   = line_to_next_abs.get(d_line, text_len)
        block = text[block_start_abs:block_end_abs]

        # 効能の前段: e_line 〜 d_line 直前
        pre_eff = text[starts[e_line]:starts[d_line]]

        # 併記ブロック内を二分
        m = DOSAGE_START.search(block)
        if m:
            split_at = m.start()
            eff2 = block[:split_at].rstrip()
            dos  = block[split_at:].lstrip()
            _log(f"efficacy@{e_line} + combined@{d_line} split={split_at}")
        else:
            # 二分できない → 複製
            eff2 = block
            dos  = block
            _log(f"efficacy@{e_line} + combined@{d_line} split=FAILED -> duplicate")

        eff = (pre_eff + eff2).strip()
        sections["efficacy"] = eff
        sections["dosage"]   = dos if dos.strip() else eff

        # 通常処理では e_line / d_line をスキップ（重複生成防止）
        skip_lines.add(e_line)
        skip_lines.add(d_line)

    # ---------- 通常スライス（bridgingで使ってない行だけ処理） ----------
    for i, (li, key) in enumerate(order):
        if li in skip_lines:
            continue

        start_abs = starts[li]
        end_abs   = line_to_next_abs.get(li, text_len)
        block = text[start_abs:end_abs].rstrip()

        # 同一行に efficacy & dosage がある純粋な併記（bridgingではない）もケア
        same_keys = [k for k, idx in anchors.items() if idx == li]
        if "efficacy" in same_keys and "dosage" in same_keys and key in ("efficacy","dosage"):
            m = DOSAGE_START.search(block)
            if m:
                split_at = m.start()
                sections["efficacy"] = block[:split_at].rstrip()
                sections["dosage"]   = block[split_at:].lstrip()
            else:
                sections["efficacy"] = block
                sections["dosage"]   = block
            # 同一行のもう片方もスキップ
            skip_lines.add(li)
            continue

        # 通常登録（長い方優先で上書き）
        if key not in sections or len(block) > len(sections[key]):
            sections[key] = block

    # 合体見出し（主要文献／文献請求先）の補完
    if "main_references" in anchors or "contact_info" in anchors:
        li_main = anchors.get("main_references")
        li_ct   = anchors.get("contact_info")
        if li_main is not None and li_ct is None:
            if "文献請求先" in lines[li_main] and "contact_info" not in sections:
                sections["contact_info"] = sections.get("main_references","")
        elif li_ct is not None and li_main is None:
            if "主要文献" in lines[li_ct] and "main_references" not in sections:
                sections["main_references"] = sections.get("contact_info","")

    # 用法が空なら効能を複製
    if sections.get("dosage","").strip() == "" and "efficacy" in sections:
        sections["dosage"] = sections["efficacy"]

    return sections

# ===================== DB =====================
def ensure_table(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS drug_filedata (
            id_druginformation SERIAL PRIMARY KEY,
            yj_code VARCHAR(16),
            section_key VARCHAR(50),
            content TEXT,
            content_length INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (yj_code, section_key)
        )
    """)

def upsert_section(cur, yj_code, section_key, content):
    content = content or ""
    cur.execute("""
        INSERT INTO drug_filedata (yj_code, section_key, content, content_length, created_at)
        VALUES (%s, %s, %s, %s, NOW())
        ON CONFLICT (yj_code, section_key) DO UPDATE SET
            content = EXCLUDED.content,
            content_length = EXCLUDED.content_length,
            created_at = EXCLUDED.created_at
    """, (yj_code, section_key, content, len(content)))

# ===================== メイン =====================
def main():
    ans = input(f"{SOURCE_DIR} のテキストを ルールベースで分割し、DB '{db_conf['dbname']}' に登録します。続行しますか？ (y/n): ").strip().lower()
    if ans != "y":
        print("中止しました。"); return

    drop = input("既存の drug_filedata を削除して作り直しますか？ (Y/n): ").strip().lower()

    try:
        files = sorted([fn for fn in os.listdir(SOURCE_DIR) if fn.endswith(".txt")])
    except FileNotFoundError:
        print(f"フォルダが見つかりません: {SOURCE_DIR}")
        return
    if not files:
        print("対象テキストが見つかりません。"); return

    conn = psycopg2.connect(**db_conf)
    cur = conn.cursor()
    if drop == "y":
        cur.execute("DROP TABLE IF EXISTS drug_filedata")
        conn.commit()
    ensure_table(cur); conn.commit()

    inserted_total = 0

    heading_logf = open(HEADING_LOG_PATH, "a", encoding="utf-8")

    with tqdm(total=len(files), desc="項目分割→SQL", unit="file") as pbar:
        for i, filename in enumerate(files, start=1):
            yj_code = os.path.splitext(filename)[0]
            path = os.path.join(SOURCE_DIR, filename)

            try:
                text = read_text_euc(path)
            except Exception as e:
                tqdm.write(f"[{filename}] 読み込み失敗: {e}")
                pbar.update(1)
                continue

            # オフセット
            lines, raw_lines, starts, text_len = make_offsets(text)

            # スコアリング→アンカー選定
            anchors, bucket = choose_best_anchors(lines)

            try:
                write_heading_log(heading_logf, filename, lines, bucket, anchors)
            except Exception as e:
                tqdm.write(f"[{filename}] 見出しログ出力エラー: {e}")

            if not anchors:
                tqdm.write(f"[{filename}] 見出し検出なし（スコア下限 {MIN_HEADING_SCORE}）")
                pbar.update(1)
                continue

            # 切り出し
            sections = slice_sections(
                text, anchors, lines, raw_lines, starts, text_len,
                bucket_by_line=bucket, heading_logf=heading_logf, filename=filename
            )
            # 書き込み
            inserted_this = 0
            for key, content in sections.items():
                if key not in SECTION_KEYS:
                    continue
                try:
                    upsert_section(cur, yj_code, key, content)
                    conn.commit()
                    inserted_this += 1
                except Exception as e:
                    tqdm.write(f"[{filename}] SQLエラー({key}): {e}")

            inserted_total += inserted_this
            head_keys = ", ".join(list(sections.keys())[:6])
            tqdm.write(f"[{filename}] sections:{len(sections)} keys:{head_keys} | INSERT:{inserted_this}")
            pbar.set_postfix({"ins": inserted_this, "total": inserted_total})
            pbar.update(1)

    cur.close(); conn.close()
    print(f"\n完了: ファイル {len(files)} 件 / 総INSERT {inserted_total} 件")

if __name__ == "__main__":
    main()
