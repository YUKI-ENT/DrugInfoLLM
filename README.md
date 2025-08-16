# DrugInfoLLM

ローカル LLM（[Ollama](https://ollama.com/)）を使って、**薬剤添付文書**（RSBase 付属）から 
**相互作用（併用禁忌・併用注意・理由など）** を抽出・構造化し、データベースへ保存するツールです。
将来的には、このデータベースを **RAG**（Retrieval-Augmented Generation）の知識ベースとして利用し、
LLM 応答の精度向上に役立てることを目標にしています。

---

## 処理の流れ（全体像）

1. **`11druginformation2SQL_score.py`**
   EUC エンコードの平文（例: `1129009F1300.txt`）を、**効能効果／用法用量／副作用／相互作用**など
   項目単位に分割して DB(PostgreSQL) へアップロードします。

2. **`12InteractionLLM.py`**
   (1) で作成したテーブルから **相互作用** セクションを取り出し、**Ollama** で LLM 推論。
   得られた **相互作用薬名／禁忌・注意区分／理由** を **JSON** として整形し、DB へ保存します。

   ![drug_interaction](https://github.com/user-attachments/assets/bb213e43-b792-4db3-aab3-2a9e7528780c)

> このような形の相 **相互作用薬リスト** ができます。これにより、アプリやUIで相互作用薬を一覧表示でき、RAG の索引やAI学習素材としても活用できます。

---

## 開発環境（参考）

> GPU はメモリが多いほど有利です。オンプレより **GPU レンタル**の方がコスト面で有利なケースもあります。当方の環境を書いておきますが、Gemma3-4bくらいが実用限界な感じでした。

- **OS**: Ubuntu 24.04.02
- **GPU**: GeForce **RTX 5080 16GB**, **Driver 570.169**, **CUDA Runtime 12.8**
- **CPU / RAM**: Intel Core i9-9900 / 32GB
- **Python**: 3.12.3
- **LLM ランタイム**: Ollama（ローカル推論, 既定で `http://localhost:11434`）

---

## 前提条件

- NVIDIA ドライバ（例: **570.169**）が正常に動作していること
  `nvidia-smi` で確認可能
- **Ollama** が導入済みで、`ollama list` が通ること
- **Python 3.12** 系 & **venv** を利用
- **データベース**（例: PostgreSQL）に接続可能
  - （任意）RAG で使うなら **pgvector** などの拡張も検討可ですが、その場合はPostgreSQLのバージョンが新しいものでないといけないので、RSBaseとは別PCで動かしたほうがいいです。

---

## セットアップ手順（最小例）

### 1. NVIDIA ドライバ（CUDA ランタイム含む）の導入

> **💡ポイント**
> - **Ubuntu の公式パッケージ（`apt`）**で入れるのが最も安定します。手動でドライバをダウンロードしてインストールする (`.run` インストーラ) 方法は、OS アップデート時のトラブル原因となるため非推奨です。
> - **Secure Boot** はBIOS設定でOffにしておいてください

#### (a) 事前準備

既存のNVIDIAドライバがインストールされている場合は、競合を避けるためにアンインストールしておくことを推奨します。
```bash
# 既存ドライバのアンインストール（必要な場合のみ）
sudo apt purge nvidia*
sudo apt autoremove
sudo reboot
```

#### (b) 必要なパッケージの導入

ドライバの自動検出・インストールツールや、コンパイルに必要なパッケージを導入します。

```bash
# パッケージリストの更新と、基本ツールのインストール
sudo apt update
sudo apt install -y ubuntu-drivers-common build-essential dkms linux-headers-$(uname -r)
```

#### (c) 最適なドライバの自動インストール

`ubuntu-drivers` コマンドを使用すると、システムに最適なドライバを自動で検出し、インストールしてくれます。

```bash
# 推奨ドライバを確認
ubuntu-drivers devices

# 推奨ドライバを自動インストール（最も安定）
sudo ubuntu-drivers autoinstall

# もしくは、特定のバージョンを明示的に指定する場合
# ※ `ubuntu-drivers devices` の出力で確認したパッケージ名を使用
# sudo apt install -y nvidia-driver-570
```

#### (d) システムの再起動と確認

インストールを完了させるためにシステムを再起動し、ドライバが正常に動作しているか確認します。

```bash
sudo reboot
```







### 1) Python 仮想環境

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip

python3 -m venv ~/venvs/druginfo-llm
source ~/venvs/druginfo-llm/bin/activate
python -V
pip -V
python -m pip install --upgrade pip

