# DrugInfoLLM

ローカル LLM（[Ollama](https://ollama.com/)）を使って、**薬剤添付文書**（RSBase 付属）から 
**相互作用（併用禁忌・併用注意・理由など）** を抽出・構造化し、データベースへ保存するツールです。
将来的には、このデータベースを **RAG**（Retrieval-Augmented Generation）の知識ベースとして利用し、
LLM 応答の精度向上に役立てることを目標にしています。

構造化されていない薬剤情報
![origdrug](https://github.com/user-attachments/assets/d5050f9a-044b-4ab0-a74c-59e93ef585ac)

これをスクリプトとLLMで構造化してデータベースにします
![sampleinteraction](https://github.com/user-attachments/assets/3eb47dc1-742c-4145-8e81-7041a85c99fc)

---

## 各スクリプトの概要

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

## セットアップ手順（Ubuntu24.04.02）

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

---

### 2. Ollama の導入

Ollamaは、ローカル環境でLLMモデルを実行するためのランタイムです。以下のコマンドでインストールできます。

```bash
curl -fsSL [https://ollama.com/install.sh](https://ollama.com/install.sh) | sh
```

> **💡ヒント**
> このスクリプトは、Ollamaの実行ファイルを `/usr/local/bin/` に配置し、サービスとして自動起動するように設定します。
> アップデート時もこのコマンドで上書きします。

#### (a) 外部からの接続を許可する設定

デフォルトでは、Ollamaは `http://localhost:11434` のみにバインドされており、同じPCからしかアクセスできません。ローカルネットワーク上の他のPCやデバイスから接続できるようにするには、サービス設定を変更して `0.0.0.0` にバインドする必要があります。

以下のコマンドでサービス設定ファイルを編集します。

```bash
sudo systemctl edit ollama.service
```
エディタが開いたら、以下の内容を記述して保存してください。

```ini
[Service]
Environment="OLLAMA_HOST=0.0.0.0"
```
#### (b) 変更の適用とサービス再起動

設定変更を反映し、Ollamaを再起動します。

```bash
sudo systemctl daemon-reload
sudo systemctl restart ollama
```
これにより、Ollamaが再起動され、ネットワーク上のすべてのインターフェースからの接続を受け付けるようになります。設定が反映されているかは、`sudo systemctl status ollama` コマンドで確認できます。

#### (c) インストール後の確認とモデルのダウンロード

インストールが完了したら、バージョン情報とサービスの稼働状況を確認します。

```bash
# Ollamaのバージョン確認
ollama --version

# Ollamaサービスの稼働状況確認
systemctl status ollama

# モデルのダウンロード（初回のみ）
# 本プロジェクトでは、gemma3:4b をテストモデルとして推奨します
ollama run gemma3:4b
```

これにより、Ollamaが正常に動作し、必要なモデルがダウンロードされます。

#### (d) GPU利用の確認

OllamaがGPUを使っているかどうかは、`nvidia-smi` コマンドで確認できます。モデルを推論中に `nvidia-smi` を実行し、Ollamaのプロセスがリストに表示され、GPUメモリが使用されていれば成功です。
![ollama_smi](https://github.com/user-attachments/assets/f67e3b89-c4ca-44d3-872c-4dd3a1a68aac)

### 3. Open WebUIの導入（オプション）

Open WebUIは、OllamaのAPIを介してLLMを操作するための、ChatGPTのように使いやすいウェブインターフェースです。ナレッジという機能を使うとRAGのように外部ファイル情報を参照して回答させることもできます。

Dockerでの導入方法を説明します。**OllamaがすでにホストOSで動作していること**を前提とします。
#### (a) Dockerのインストール
```bash
# Docker公式リポジトリのセットアップ
sudo apt-get update
sudo apt-get install ca-certificates curl

sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

# Dockerエンジンと必要なツールのインストール
sudo apt-get update
sudo apt-get install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# Dockerをsudoなしで実行するための設定（オプション）
sudo groupadd docker
sudo usermod -aG docker $USER
newgrp docker
```
`newgrp docker` を実行すると、グループの変更が即座に反映されます。これで、`sudo` を使わずに `docker` コマンドを実行できるようになります。
#### (b) Open WebUIの起動

Open WebUIを起動するコマンドに、自動再起動のオプションについて追記します。

以下のコマンドをターミナルで実行し、Ollamaと連携するOpen WebUIコンテナを起動します。

```bash
docker run -d --network=host --gpus=all -v open-webui:/app/backend/data -e OLLAMA_API_BASE_URL=http://localhost:11434 --name open-webui --restart always ghcr.io/open-webui/open-webui:main
```

#### (c) 動作確認とアクセス方法
以下のコマンドを実行して、Open WebUIコンテナが正常に起動しているか確認します。`STATUS`が`Up ...`となっていれば成功です。
```bash
docker ps
```
コンテナが起動したら、ブラウザで以下のURLにアクセスしてください。
`http://(ホスト名):8080`

初回アクセス時にはアカウント作成画面が表示されます。アカウントを作成すると、Open WebUIのインターフェースからOllamaにロードされたモデルを使って会話を始めることができます。

![openwebui](https://github.com/user-attachments/assets/1e94fbb8-6e18-43ff-b0f3-53ff25d553ca)


### 4 Python仮想環境の導入
Pythonの仮想環境をセットアップします。

#### (a) 仮想環境のセットアップ
```bash
# パッケージリストを更新し、venvとpipをインストール
sudo apt update
sudo apt install -y python3-venv python3-pip

# 仮想環境を作成
python3 -m venv ~/venvs/druginfo-llm

#アクティベート
source ~/venvs/druginfo-llm/bin/activate
```

### 5 PostgreSQLのインストール
RSBaseのPostgreSQLを使うこともできますが、古いバージョンのPostgreSQLではベクトル検索拡張機能のpgvectorが使えないので、今後のことを考えると別サーバーに新しいバージョンをインストールするか、RSBaseのPostgreSQLを最新にすることをお勧めします。

Ubuntu 24.04では、以下のコマンドでPostgreSQLサーバーとクライアントをインストールできます。
```bash
sudo apt update
sudo apt install -y postgresql postgresql-contrib
```
インストールが完了したら、サービスが正常に動作しているか確認します。
```bash
sudo systemctl status postgresql
```
デフォルトでは、`postgres` というユーザーが作成されますが、初期パスワードが設定されていないのでここで設定します。
```bash
# postgresユーザーに切り替える
sudo -i -u postgres

# psqlコンソールに入る
psql

# postgresユーザーのパスワードを設定するSQLコマンドを実行します。'your_new_password'を任意のパスワードに置き換えてください。
ALTER USER postgres WITH PASSWORD 'your_new_password';

# psqlコンソールを終了する
\q
```

これで本ツールを利用する環境が整いました。

---

## ツール実行 （薬剤添付文書の変換）
### 1 リポジトリクローン
リポジトリをクローンし、必要なライブラリをインストールしてツールを実行します。
```bash
# 仮想環境をアクティベート
source ~/venvs/druginfo-llm/bin/activate

# 本リポジトリをクローン
git clone https://github.com/YUKI-ENT/DrugInfoLLM.git
cd DrugInfoLLM

# 依存関係のインストール
pip install -r requirements.txt
```

### 2 データベースの準備とツール実行
11druginformation2SQL_score.py と 12InteractionLLM.py の実行手順を記載します。
#### 2-1 添付文書データの準備
RSBase付属の `drug_information.zip` を `DrugInfoLLM` フォルダにコピーし、解凍します。
```bash
unzip drug_information.zip
```
#### 2-2 config.jsonの設定
