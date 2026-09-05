# 本番DB 読み取り専用アクセスのセットアップ

`scripts/query_db.sh`（ローカルから本番DBを直接SELECTするスクリプト）を使うための、
**VPS側の一度きりの準備手順**です。

前提: VPS = `trader@157.180.91.249`、DB = PostgreSQL の `autotrd` データベース。

---

## 1. VPS にログインする

ローカルのターミナルで、1行ずつ実行してください。

```
ssh trader@157.180.91.249
```

---

## 2. 読み取り専用ロール `autotrd_ro` を作る

まずパスワードを決めます。以下はランダム生成する例です（表示された文字列を控えてください）。

```
openssl rand -base64 24
```

次に、上で表示されたパスワードを `<ここにパスワード>` の部分に貼り付けて実行します。

```
sudo -u postgres psql -d autotrd
```

`autotrd=#` というプロンプトが出たら、以下を1行ずつ貼り付けます。

```
CREATE ROLE autotrd_ro WITH LOGIN PASSWORD '<ここにパスワード>';
```
```
GRANT CONNECT ON DATABASE autotrd TO autotrd_ro;
```
```
GRANT USAGE ON SCHEMA public TO autotrd_ro;
```
```
GRANT SELECT ON ALL TABLES IN SCHEMA public TO autotrd_ro;
```
```
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO autotrd_ro;
```
```
ALTER ROLE autotrd_ro SET default_transaction_read_only = on;
```

> 最後の行がポイントです。今後 alembic で新しいテーブルが追加されても、
> `ALTER DEFAULT PRIVILEGES` のおかげで自動的に SELECT 権限が付きます。

終わったら psql を抜けます。

```
\q
```

---

## 3. パスワードを `.pgpass` に保存する（毎回の入力を不要にする）

`<ここにパスワード>` は手順2で決めたものと同じです。

```
echo 'localhost:5432:autotrd:autotrd_ro:<ここにパスワード>' >> ~/.pgpass
```
```
chmod 600 ~/.pgpass
```

---

## 4. VPS 上で動作確認する

```
psql -h localhost -U autotrd_ro -d autotrd -c "SELECT count(*) FROM trade_log;"
```

件数が表示されれば成功です。書き込みが拒否されることも確認します。

```
psql -h localhost -U autotrd_ro -d autotrd -c "DELETE FROM trade_log WHERE id = -1;"
```

`ERROR:  permission denied for table trade_log` のようなエラーが出れば正常です。

VPS から抜けます。

```
exit
```

---

## 5. ローカルから動作確認する

ローカルのターミナルで、プロジェクトディレクトリに移動してから実行します。

```
cd ~/myClaude/004_autoTRD
```
```
bash scripts/query_db.sh --tables
```

テーブル一覧が表示されれば完了です。

---

## 使い方

```
bash scripts/query_db.sh "SELECT * FROM trade_log ORDER BY id DESC LIMIT 10"
bash scripts/query_db.sh --schema trade_log
bash scripts/query_db.sh --csv "SELECT ticker, pnl FROM trade_log" > /tmp/out.csv
bash scripts/query_db.sh -f scripts/sql/weekly.sql
```

## 安全性について

書き込みは2重にブロックされています。

1. `autotrd_ro` には `SELECT` しか権限がない
2. `default_transaction_read_only = on` をロール既定とサーバオプションの両方で強制

また `statement_timeout=30秒` を設定しているため、重いクエリが本番DBを
長時間占有することはありません。

## CSVエクスポート方式との関係

従来の `scripts/export_db_csv.sh` + `scripts/sync_db_csv.sh` はそのまま残してあります。
スナップショットを手元に保存したい場合はそちらを、
最新データをその場で確認したい場合は `query_db.sh` を使ってください。
