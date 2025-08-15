# split_sections_llm.py
# EUC-JPの添付文書を、LLMで「見出し行だけ」極小バッチ分類→本文は原文スライスで抽出→SQLへUPSERT
# ・EUC固定/タブ→スペース
# ・候補は厳格→緩い抽出。LLMへは 1バッチ=少数行のみ渡す
# ・併記行は複数キー（["efficacy","dosage"]）を許容
# ・CRLF対応で絶対位置計算
# ・LLMプロンプト/応答をバッチ単位でログ
# ・GPU冷却は件数ベースのみ

import os, re, json, ast, time
from datetime import datetime
import psycopg2, requests
from tqdm import tqdm

# ===================== 設定 =====================
with open("config.json", "r", encoding="utf-8") as f:
    config = json.load(f)

db_conf  = config["db"]
source_dir = config.get("DI_folder") or config.get("source_dir") or "./drug_information"

ollama_url     = config.get("ollama_url", "http://localhost:11434/api/generate")
ollama_model   = config.get("ollama_model", "gemma3:4b")
ollama_timeout = int(config.get("ollama_timeout", 120))

pause_second        = int(config.get("gpu_cooling_wait", 15))
pause_every_n_files = int(config.get("pause_every_n_files", 10))

llm_log_path         = config.get("llm_log_file", "split_llm.log")
llm_log_prompt_max   = int(config.get("llm_log_prompt_max_chars", 4000))
llm_log_response_max = int(config.get("llm_log_response_max_chars", 4000))

llm_candidates_per_batch     = int(config.get("llm_candidates_per_batch", 12))
llm_candidate_text_max_chars = int(config.get("llm_candidate_text_max_chars", 80))

enable_regex_fallback = bool(config.get("enable_regex_fallback", False))

# ===================== セクション定義 =====================
SECTION_KEYS = [
  "warning","contraindications","efficacy","efficacy_notes","dosage","dosage_notes",
  "precautions","important_notes","special_patient_notes","interactions","side_effects",
  "lab_influence","overdose","application_notes","other_notes","pharmacokinetics",
  "clinical_results","pharmacodynamics","compound_properties","handling_notes",
  "approval_conditions","packaging","main_references","contact_info"
]

section_aliases = {
  "warning":["警告"],
  "contraindications":["禁忌","禁忌（次の患者には投与しないこと）"],
  "efficacy":["効能又は効果","効能・効果","効能効果","効能及び効果"],
  "efficacy_notes":["効能又は効果に関連する注意"],
  "dosage":["用法及び用量","用法・用量","用法、用量","用量及び用法"],
  "dosage_notes":["用法及び用量に関連する注意"],
  "precautions":["使用上の注意"],
  "important_notes":["重要な基本的注意"],
  "special_patient_notes":["特定の背景を有する患者に関する注意"],
  "interactions":["相互作用"],
  "side_effects":["副作用"],
  "lab_influence":["臨床検査結果に及ぼす影響"],
  "overdose":["過量投与"],
  "application_notes":["適用上の注意"],
  "other_notes":["その他の注意"],
  "pharmacokinetics":["薬物動態"],
  "clinical_results":["臨床成績"],
  "pharmacodynamics":["薬効薬理"],
  "compound_properties":["有効成分に関する理化学的知見"],
  "handling_notes":["取扱い上の注意"],
  "approval_conditions":["承認条件"],
  "packaging":["包装"],
  "main_references":["主要文献"],
  "contact_info":["文献請求先及び問い合わせ先","文献請求先","問い合わせ先","製造販売業者等の氏名又は名称及び所在地"]
}

# 厳格（行全体が見出し／併記も許可）
_tokens = "|".join([re.escape(p) for vs in section_aliases.values() for p in vs])
HEADLINE_STRICT = re.compile(
  r"^\s*[【\[]?\s*(?:%s)(?:\s*(?:/|／|・|、|及び|又は)\s*(?:%s))*\s*[】\]]?\s*$" % (_tokens, _tokens)
)
# 緩い（含んでいれば候補）
HEADING_CONTAINS = re.compile(r"(?:%s)" % _tokens)

DOSAGE_MARKER = re.compile(
  r"(?m)^[ 　]*(?:\d+|[０-９]+)\s*日|^[ 　]*(?:用法|投与|塗布|経口|経口投与|静注|点滴|噴霧|吸入|外用|内服|適用量|用量)"
)

SECTION_PRIORITY = [
  "warning","contraindications","efficacy","dosage",
  "precautions","important_notes","special_patient_notes","interactions",
  "side_effects","lab_influence","overdose","application_notes","other_notes",
  "pharmacokinetics","clinical_results","pharmacodynamics","compound_properties",
  "handling_notes","approval_conditions","packaging","main_references","contact_info"
]

# ===================== ログ =====================
def _truncate(s, n):
    if s is None: return ""
    return s if len(s) <= n else s[:n] + f"\n...[truncated {len(s)-n} chars]"

def log_llm(logf, filename, batch_idx, total_batches, cand_count, prompt, response, note=""):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logf.write("====================================================================================================\n")
    logf.write(f"[{ts}] file={filename} batch={batch_idx}/{total_batches} candidates={cand_count} {note}\n")
    logf.write("--- PROMPT ---\n")
    logf.write(_truncate(prompt, llm_log_prompt_max) + "\n")
    logf.write("--- RESPONSE ---\n")
    logf.write(_truncate(response, llm_log_response_max) + "\n")
    logf.write("====================================================================================================\n\n")
    logf.flush()

# ===================== I/O =====================
def read_text_euc(path: str) -> str:
    with open(path, "r", encoding="euc_jp", errors="replace") as f:
        return f.read().replace("\t", " ")

# ===================== LLM =====================
def _parse_json_maybe(s: str):
    try:
        return json.loads(s)
    except Exception:
        pass
    m = re.search(r"(\[[\s\S]*\])", s)
    if m:
        frag = m.group(1)
        try:
            return json.loads(frag)
        except Exception:
            try: return ast.literal_eval(frag)
            except Exception: pass
    m = re.search(r"(\{[\s\S]*\})", s)
    if m:
        frag = m.group(1)
        try:
            return json.loads(frag)
        except Exception:
            try: return ast.literal_eval(frag)
            except Exception: pass
    return None

def call_ollama_candidates(batch, filename, batch_idx, total_batches, llm_logf):
    """
    batch: [{"id": <int>, "line_index": int, "text": str}, ...]
    return: [{"id": int, "section_keys": [str,...]}]
    """
    # 極小・厳格プロンプト（本文なし／候補だけ）
    system_rules = (
        "あなたは日本語の医薬品添付文書の見出し分類器です。"
        "候補行ごとに、以下のラベル集合から該当するものを0〜複数返してください。"
        "ラベル集合: [" + ", ".join(SECTION_KEYS) + "]。"
        "出力は厳密なJSON配列のみ。各要素は {\"id\": <int>, \"section_keys\": [<label>...] }。"
        "見出しでない場合は、そのidは出力に含めない。説明文や余計なキーは禁止。"
        "同一行に『効能』と『用法』が併記されている場合は、section_keysに両方を入れる。"
    )
    prompt = system_rules + "\n\n" + json.dumps({"candidates": [{"id":c["id"], "line_index":c["line_index"], "text":c["text"]} for c in batch]}, ensure_ascii=False)

    payload = {
        "model": ollama_model,
        "prompt": prompt,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0}
    }
    raw = ""
    try:
        r = requests.post(ollama_url, json=payload, timeout=ollama_timeout)
        r.raise_for_status()
        raw = r.json().get("response","").strip()
    except Exception as e:
        raw = f"__ERROR__:{e}"

    if llm_logf:
        log_llm(llm_logf, filename, batch_idx, total_batches, len(batch), prompt, raw)

    data = _parse_json_maybe(raw)
    # ラッパー（results/headingsなど）があれば剥がす
    if isinstance(data, dict):
        for k in ("results","headings","candidates","labels"):
            if isinstance(data.get(k), list):
                data = data[k]; break
    if not isinstance(data, list):
        return []

    out = []
    for it in data:
        if not isinstance(it, dict): continue
        _id = it.get("id")
        keys = it.get("section_keys")
        if isinstance(_id, int) and isinstance(keys, list):
            # ラベル正規化
            norm = [k for k in keys if k in SECTION_KEYS]
            if norm:
                out.append({"id": _id, "section_keys": norm})
    return out

# ===================== 抽出（極小バッチ） =====================
def build_candidates(lines):
    strict, loose = [], []
    for idx, line in enumerate(lines):
        s = line.strip()
        if not s: continue
        if HEADLINE_STRICT.match(s):
            strict.append({"line_index": idx, "text": s})
        elif len(s) <= 80 and HEADING_CONTAINS.search(s):
            loose.append({"line_index": idx, "text": s})
    base = strict if strict else loose
    # candidate text を短縮
    for c in base:
        c["text"] = c["text"][:llm_candidate_text_max_chars]
    return base

def squash_same_line_anchors(anchors):
    """
    anchors: list[(abs_body, line_idx, section_key)]
    同一line_idxで複数keyがある場合、
      - efficacy & dosage 同居 → 両方残す
      - それ以外は優先順位で1つだけ残す
    """
    from collections import defaultdict
    byline = defaultdict(list)
    for a in anchors:
        byline[a[1]].append(a)
    squashed = []
    for line_idx, group in byline.items():
        abs_body = group[0][0]
        keys = [g[2] for g in group]
        if ("efficacy" in keys) and ("dosage" in keys):
            if "efficacy" in keys: squashed.append((abs_body, line_idx, "efficacy"))
            if "dosage"   in keys: squashed.append((abs_body, line_idx, "dosage"))
        else:
            best = min(keys, key=lambda k: SECTION_PRIORITY.index(k) if k in SECTION_PRIORITY else 999)
            squashed.append((abs_body, line_idx, best))
    squashed.sort(key=lambda x: (x[0], x[2]))
    return squashed

def extract_sections_micro(text: str, filename: str, llm_logf) -> dict:
    # 改行込みで保持（CRLF対応）
    raw_lines = text.splitlines(True)              # 改行を保持
    lines = [ln.rstrip("\r\n") for ln in raw_lines]
    starts, off = [], 0
    for ln in raw_lines:
        starts.append(off)
        off += len(ln)

    # 候補抽出
    base = build_candidates(lines)
    if not base:
        return {}

    # id 付与→極小バッチに分割
    for i, c in enumerate(base, start=1):
        c["id"] = i
    batches = [ base[i:i+llm_candidates_per_batch] for i in range(0, len(base), llm_candidates_per_batch) ]

    # バッチ分類
    normalized = []
    for bi, batch in enumerate(batches, start=1):
        res = call_ollama_candidates(batch, filename, bi, len(batches), llm_logf)
        normalized.extend(res)

    # normalized: [{"id":..,"section_keys":[..]}] → アンカーへ
    id2line = { c["id"]: c["line_index"] for c in base }
    anchors = []
    for item in normalized:
        li = id2line.get(item["id"])
        if li is None: continue
        for sk in item["section_keys"]:
            abs_body = starts[li] + len(lines[li]) + (len(raw_lines[li]) - len(lines[li]))  # 行末改行分も吸収済
            # ↑ ただし上で starts は改行込みなので、行末の+1は要らない
            anchors.append((abs_body, li, sk))

    # フォールバック（任意）
    if not anchors and enable_regex_fallback:
        # 候補テキストの語から自前マッピング
        for c in base:
            s = c["text"]
            for key, variants in section_aliases.items():
                if any(v in s for v in variants):
                    abs_body = starts[c["line_index"]] + len(lines[c["line_index"]])
                    anchors.append((abs_body, c["line_index"], key))

    if not anchors:
        return {}

    anchors.sort(key=lambda x: (x[0], x[2]))
    anchors = squash_same_line_anchors(anchors)

    # スライス
    text_len = len(text)
    sections = {}
    i = 0
    while i < len(anchors):
        start_abs, line_idx, sk = anchors[i]

        # 同一行 併記（efficacy/dosage）は特別扱い（すでにsquash済みなので通常と同じでOK）
        end_abs = anchors[i+1][0] if i+1 < len(anchors) else text_len
        content = text[start_abs:end_abs].strip()
        if sk not in sections or len(content) > len(sections[sk]):
            sections[sk] = content
        i += 1

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
    ans = input(f"{source_dir} のテキストを LLM で分割し、DB '{db_conf['dbname']}' に登録します。続行しますか？ (y/n): ").strip().lower()
    if ans != "y":
        print("中止しました。"); return

    drop = input("既存の drug_filedata を削除して作り直しますか？ (Y/n): ").strip().lower()

    try:
        files = sorted([fn for fn in os.listdir(source_dir) if fn.endswith(".txt")])
    except FileNotFoundError:
        print(f"フォルダが見つかりません: {source_dir}")
        return
    if not files:
        print("対象テキストが見つかりません。"); return

    conn = psycopg2.connect(**db_conf)
    cur = conn.cursor()
    if drop == "y":
        cur.execute("DROP TABLE IF EXISTS drug_filedata")
        conn.commit()
    ensure_table(cur); conn.commit()

    llm_logf = open(llm_log_path, "a", encoding="utf-8")

    inserted_total = 0
    with tqdm(total=len(files), desc="項目分割→SQL", unit="file") as pbar:
        for i, filename in enumerate(files, start=1):
            yj_code = os.path.splitext(filename)[0]
            path = os.path.join(source_dir, filename)

            try:
                raw = read_text_euc(path)  # EUC固定・タブ→スペース
            except Exception as e:
                tqdm.write(f"[{filename}] 読み込み失敗: {e}")
                pbar.update(1)
                if i % pause_every_n_files == 0:
                    tqdm.write(f"--- {i}ファイル処理済み、冷却のため {pause_second} 秒休止 ---")
                    time.sleep(pause_second)
                continue

            t0 = time.time()
            sections = extract_sections_micro(raw, filename, llm_logf)
            elapsed = time.time() - t0

            if not sections:
                tqdm.write(f"[{filename}] 見出し検出なし | {elapsed:.1f}s")
                pbar.update(1)
                if i % pause_every_n_files == 0:
                    tqdm.write(f"--- {i}ファイル処理済み、冷却のため {pause_second} 秒休止 ---")
                    time.sleep(pause_second)
                continue

            inserted_this = 0
            for key, content in sections.items():
                if key in SECTION_KEYS:
                    try:
                        upsert_section(cur, yj_code, key, content)
                        conn.commit()
                        inserted_this += 1
                    except Exception as e:
                        tqdm.write(f"[{filename}] SQLエラー({key}): {e}")

            inserted_total += inserted_this
            head_keys = ", ".join(list(sections.keys())[:5])
            tqdm.write(f"[{filename}] sections:{len(sections)} keys:{head_keys} | INSERT:{inserted_this} | {elapsed:.1f}s")

            pbar.set_postfix({"last": f"{elapsed:.1f}s", "ins": inserted_this, "total": inserted_total})
            pbar.update(1)

            if i % pause_every_n_files == 0:
                tqdm.write(f"--- {i}ファイル処理済み、冷却のため {pause_second} 秒休止 ---")
                time.sleep(pause_second)

    llm_logf.close()
    cur.close(); conn.close()
    print(f"\n完了: ファイル {len(files)} 件 / 総INSERT {inserted_total} 件")

if __name__ == "__main__":
    main()
