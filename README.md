# Video Search with Gemini

> FMAPI Gemini と Databricks Vector Search を組み合わせた、動画シーンの意味検索アプリ。

動画をアップロードすると、シーンごとに自動分割し、音声を文字起こし、各シーンを Gemini で要約・特徴抽出。テキストクエリで「該当シーン」を確度順に表示します。

---

## 1. アプリ概要

**3 つの操作セクション**を持つ Single-page Web アプリです。

| # | セクション | 内容 |
|---|------------|------|
| 1 | **動画アップロード & シーン分割** | 動画を UC Volume に保存 → PySceneDetect でシーン分割 → ffmpeg で各シーンを mp4 / wav に切り出し |
| 2 | **Gemini 解析 & Embedding 同期** | 各シーンを FMAPI Gemini に渡し、`transcript` / `summary` / `features` を JSON 生成 → Delta テーブルに保存 → Vector Search index を TRIGGERED で sync |
| 3 | **シーン検索** | テキストクエリ → Databricks Vector Search でハイブリッド検索 → 確度スコア順に該当シーンを表示 |

**フロー制御:**

- セクション 1 完了時に **自動でセクション 2 が開始** されます（必須フロー）。
- セクション 3 の検索バーは**画面上部に固定表示**され、いつでもどこからでもクエリ可能です。

---

## 2. 使用方法

### 2.1 前提

- Databricks Workspace: `fevm-classic-stable-ytcy.cloud.databricks.com`
- Unity Catalog: `classic_stable_ytcy_catalog`
- Schema: `mulitmodal_video_search_with_gemini` (**スペル注意**: `multi` ではなく `mulit`)
- 必要な権限:
  - SQL Warehouse: `CAN_USE`
  - Vector Search Endpoint `video-search-endpoint`: `CAN_MANAGE`
  - FMAPI: `databricks-gemini-2-5-flash`, `databricks-gte-large-en` への `CAN_QUERY`
  - UC Volume `media`: `WRITE_VOLUME`

### 2.2 初期セットアップ（一度だけ）

Databricks 上で以下を実行:

1. `notebooks/00_setup_resources.py` … schema / volume / Delta テーブル 3 種を作成
2. `notebooks/01_setup_vector_search.py` … `scene_analysis_index` を TRIGGERED で作成

または CLI でスキーマ・テーブル・index を一括作成済み（本リポジトリは作成済み環境を前提）。

### 2.3 ローカル開発

```bash
cd app
pip install -r requirements.txt
export DATABRICKS_HOST=https://fevm-classic-stable-ytcy.cloud.databricks.com
export DATABRICKS_TOKEN=...
export DATABRICKS_WAREHOUSE_ID=e351c2d1b16eae95
uvicorn main:app --reload --port 8000
# http://localhost:8000 を開く
```

### 2.4 Databricks Apps へデプロイ

```bash
# 1) Bundle (workspace files + app resource) を deploy
DATABRICKS_CLI_PATH=/opt/homebrew/bin/databricks PATH=/opt/homebrew/bin:$PATH \
  databricks bundle deploy --profile fevm-classic-stable-ytcy

# 2) App compute を起動
databricks apps start video-search-with-gemini --profile fevm-classic-stable-ytcy

# 3) コードを app へ deploy
databricks apps deploy video-search-with-gemini \
  --source-code-path /Workspace/Users/<you>@databricks.com/.bundle/video_search_with_gemini/dev/files/app \
  --profile fevm-classic-stable-ytcy
```

URL: `https://video-search-with-gemini-<workspace-id>.aws.databricksapps.com`

### 2.5 操作手順

1. 画面の **Section 1** で動画ファイル (mp4/mov/mkv/webm/avi) をドラッグ&ドロップ
2. **「アップロード & シーン分割」** ボタンをクリック
   - 動画が UC Volume `media/uploads/` に保存される
   - PySceneDetect でシーンを検出（4 秒以上の連続シーンのみ）
   - 各シーンを `media/scenes/` と `media/audio/` に保存
3. Section 1 完了後、**Section 2 が自動で開始**
   - 各シーンから 4 フレームを抽出
   - フレーム + 音声 (4MB 以下) を Gemini 2.5 Flash に送信
   - `transcript` / `summary` / `features` を JSON で取得
   - `scene_analysis` テーブルへ INSERT
   - Vector Search index を TRIGGERED で sync
4. 画面上部の検索バーで自然文クエリ → ヒットしたシーンが確度順に表示
5. 結果カードをクリックすると、そのシーンの動画クリップが再生される

---

## 3. 使用技術

### 3.1 バックエンド

| レイヤ | 技術 |
|--------|------|
| Web framework | **FastAPI** 0.115 + uvicorn |
| Auth | `databricks-sdk` (App 内では自動で SP token) |
| SQL | `databricks-sql-connector` (Warehouse 経由で Delta テーブル操作) |
| Vector Search | `databricks-vectorsearch` + REST API |
| シーン検出 | **PySceneDetect** (ContentDetector, threshold=30, min_scene=4s) |
| メディア処理 | **ffmpeg / ffprobe** (imageio-ffmpeg バンドル) |
| LLM | **FMAPI Gemini 2.5 Flash** (`databricks-gemini-2-5-flash`) |
| Embedding | **FMAPI GTE-large-en** (`databricks-gte-large-en`, 1024 dim) |

> **Qwen embedding** について：本来は Qwen3-Embedding を使う構想だったが、FMAPI にビルトインの Qwen embedding が存在しないため、Pay-Per-Token で即利用可能な GTE-large-en を採用。

### 3.2 フロントエンド

- 単一 HTML + Vanilla JS + CSS (フレームワーク無し)
- 検索バーは `position: sticky` で常時表示
- 結果は右側のフローティングパネル

### 3.3 データストレージ

| 種別 | パス / 名前 |
|------|-------------|
| UC Volume | `/Volumes/classic_stable_ytcy_catalog/mulitmodal_video_search_with_gemini/media/{uploads,scenes,audio}` |
| Delta テーブル | `videos`, `scenes`, `scene_analysis` |
| Vector Search Index | `classic_stable_ytcy_catalog.mulitmodal_video_search_with_gemini.scene_analysis_index` |

---

## 4. アーキテクチャ

```
┌──────────────────────────────────────────────────────────────────────────┐
│                              Browser (UI)                                │
│  Section 1: Upload  ──┐  Section 2: Analyze  ──┐   Section 3: Search    │
│                       │                        │   (sticky top bar)     │
└───────────────────────┼────────────────────────┼─────────────────────────┘
                        │ POST /api/upload       │ POST /api/analyze/{id}
                        │ POST /api/process/{id} │ POST /api/search
                        ▼                        ▼
            ┌─────────────────────────────────────────────────┐
            │   Databricks Apps  (FastAPI / uvicorn)          │
            │   - video_processing.py  (PySceneDetect+ffmpeg) │
            │   - gemini_client.py     (FMAPI Gemini)         │
            │   - db_client.py         (SQL + VS)             │
            └─┬───────────────┬─────────────────────┬─────────┘
              │               │                     │
              ▼               ▼                     ▼
     ┌────────────────┐ ┌──────────────┐ ┌──────────────────────┐
     │   UC Volume    │ │ Delta tables │ │ FMAPI / Vector Search│
     │  uploads/      │ │ - videos     │ │ - gemini-2-5-flash   │
     │  scenes/       │ │ - scenes     │ │ - gte-large-en       │
     │  audio/        │ │ - analysis   │ │ - scene_analysis_idx │
     └────────────────┘ └──────┬───────┘ └────────────▲─────────┘
                               │ TRIGGERED sync       │
                               └──────────────────────┘
```

### データフロー詳細

1. **Upload**: ブラウザ → FastAPI → `/api/2.0/fs/files` で UC Volume `uploads/` へ PUT。`videos` テーブルに 1 行 INSERT。
2. **Process**: Volume から動画を temp に DL → PySceneDetect でシーン境界検出 → ffmpeg で各シーンを mp4 / 16kHz mono wav に切り出し → Volume へアップロード → `scenes` テーブルへ N 行 INSERT。
3. **Analyze**: 各シーンを temp に DL → ffmpeg で 4 フレーム JPEG 抽出 → Gemini に `[text, image*4, audio(option)]` を multipart で送信 → JSON parse → `scene_analysis` テーブルへ INSERT → VS index に sync 要求。
4. **Search**: クエリテキストを `databricks-gte-large-en` で embedding 化 → Vector Search に top-N クエリ → 結果を score 順に返す。

---

## 5. 発生したエラーとその解決方法

開発中に遭遇した問題と対処をまとめます。

### 5.1 SQL connector の named param と positional param の混在

**症状**: `db.exec` 呼び出しで dict を渡したり tuple を渡したりすると `ParameterError` が出る。

**原因**: `databricks-sql-connector` の `Cursor.execute(operation, parameters)` は、placeholder の形式 (`?` か `%(name)s`) に応じて期待する型が変わる。

**解決**: SQL 文を全て `?` placeholder で統一し、`params` は常に `tuple` で渡すよう統一。

### 5.2 MERGE 文 + `?` placeholder で構文エラー

**症状**: `MERGE INTO ... USING (SELECT ? AS scene_id) ... WHEN MATCHED ...` が SQL parser に拒否される。

**原因**: Databricks SQL では MERGE 内の `USING (SELECT ?)` の `?` の位置が一部のドライバ版で解釈されない。

**解決**: `DELETE WHERE pk = ?` → `INSERT VALUES (...)` の 2 ステートメントに分解。冪等性は維持しつつ、`?` placeholder で確実に動作。

### 5.3 Gemini の audio 入力の互換性

**症状**: `{"type": "input_audio", ...}` を渡すと 400/422 が返ることがある。

**原因**: FMAPI Gemini エンドポイントのバージョンや route によっては OpenAI 形式の `input_audio` を受け付けない場合がある。

**解決**: 400/422 を捕捉して**音声無しでリトライ**するフォールバックを `gemini_client.py:analyze_scene` に実装。フレームベースの要約は確実に取得できるため、品質低下を最小化。

### 5.4 PySceneDetect で過剰なシーン分割

**症状**: 短いカット（0.5s 程度）が大量に作られ、後段の解析が爆発する。

**原因**: PySceneDetect のデフォルト `min_scene_len` がフレーム単位かつ短い。

**解決**: `ContentDetector(threshold=30, min_scene_len=int(4.0 * 30))` で 4 秒以下のシーンを除外。シーン未検出の動画は「全体を 1 シーン」として fallback。

### 5.5 UC Volume へのストリーミングアップロード

**症状**: 大きな動画ファイル (100MB+) を Python メモリに丸ごと載せると OOM。

**解決**: temp ファイルに書き出してから `requests.put(..., data=open(...,'rb'))` で stream upload。

### 5.6 同じ動画の再 process で primary key 重複

**症状**: 同じ動画を再 process した時に `scenes` / `scene_analysis` の primary key conflict。

**解決**: `/api/process/{video_id}` の冒頭で対象 `video_id` の既存行を `DELETE` してから入れ直す。VS sync は次回 trigger で reconcile される。

### 5.7 Databricks Apps コンテナで `libGL.so.1` 不足

**症状**: deploy 時に
```
ImportError: libGL.so.1: cannot open shared object file: No such file or directory
```
で起動失敗。

**原因**: PySceneDetect が引っ張る `opencv-python` (GUI 版) が `libGL` を要求するが、Databricks Apps の slim runtime には入っていない。

**解決**: `requirements.txt` で `scenedetect[opencv-headless]` の extras に頼らず、明示的に `opencv-python-headless>=4.10.0` を pin。これで libGL 依存が消え正常起動。

### 5.8 App 名にアンダースコア不可

**症状**: `bundle deploy` で
```
App name must contain only lowercase letters, numbers, and dashes.
```

**解決**: GitHub リポジトリ名は `video_search_with_gemini` のままで、Databricks Apps の名前のみ `video-search-with-gemini` (ハイフン) に変更。`databricks.yml` で `name:` をハイフン形式に揃える。

### 5.9 Databricks CLI v0.18 と v1.1 の併存

**症状**: `bundle deploy` 中に
```
legacy databricks CLI detected; upgrade to >= 0.100.0
```

**原因**: ローカル PATH 上、`venv/bin/databricks` (v0.18) が `/opt/homebrew/bin/databricks` (v1.1) より先に解決される。bundle deploy はサブプロセスで CLI を呼ぶため、古い CLI を呼んで失敗。

**解決**: `DATABRICKS_CLI_PATH=/opt/homebrew/bin/databricks PATH=/opt/homebrew/bin:$PATH databricks bundle deploy …` で明示。

### 5.10 Databricks Apps コンテナで `ffprobe` / `ffmpeg` が無い

**症状**: `/api/upload` 実行時に
```
FileNotFoundError: [Errno 2] No such file or directory: 'ffprobe'
```
500 エラーで停止。

**原因**: Apps の slim Python runtime は ffmpeg/ffprobe を含んでいない。

**解決**:
- duration 取得・フレーム抽出は **OpenCV (`cv2.VideoCapture`)** で代替 (ffprobe 不要)。
- 動画切り出し・音声抽出は `imageio_ffmpeg.get_ffmpeg_exe()` でバンドル ffmpeg のパスを取得し `subprocess.run([FFMPEG, ...])` で起動。
- 音声トラックが無い動画用に `anullsrc` フォールバックも追加。

### 5.11 UC Volume への大容量 PUT で SSL EOF

**症状**: 32MB 程度の動画を `requests.put("/api/2.0/fs/files/...", data=f)` でアップロードすると
```
requests.exceptions.SSLError: ... EOF occurred in violation of protocol
```
で接続が切れる。

**原因**: 生の REST `PUT` で大きなボディを送ると、proxy / load balancer 側で接続が切られる場合がある。
特に Databricks Apps の OBO トークン経由のリクエストでは Content-Length / chunked 等の扱いが不安定。

**解決**: Databricks SDK の `WorkspaceClient.files.upload(file_path, contents=..., overwrite=True)` に置換。
SDK 側で multipart / chunked / retry を適切にハンドルしてくれる。ダウンロードも `w.files.download()` を使用。

### 5.12 App Service Principal の UC 権限不足

**症状**: アップロード時に
```
PermissionDenied: User does not have USE CATALOG on Catalog 'classic_stable_ytcy_catalog'
```

**原因**: Databricks Apps の SP (`app-<id> <name>`) は default で UC アクセス権を持たない。`app.yaml` の `resources` ブロックは SQL warehouse / serving endpoint / vector search endpoint には対応するが、catalog / schema / volume のグラントは別途必要。

**解決**: SP に直接 GRANT する。
```sql
GRANT USE CATALOG ON CATALOG classic_stable_ytcy_catalog TO `<sp_client_id>`;
GRANT USE SCHEMA  ON SCHEMA  classic_stable_ytcy_catalog.mulitmodal_video_search_with_gemini TO `<sp_client_id>`;
GRANT ALL PRIVILEGES ON SCHEMA classic_stable_ytcy_catalog.mulitmodal_video_search_with_gemini TO `<sp_client_id>`;
GRANT ALL PRIVILEGES ON VOLUME classic_stable_ytcy_catalog.mulitmodal_video_search_with_gemini.media TO `<sp_client_id>`;
GRANT SELECT, MODIFY ON TABLE …videos    TO `<sp_client_id>`;
GRANT SELECT, MODIFY ON TABLE …scenes    TO `<sp_client_id>`;
GRANT SELECT, MODIFY ON TABLE …scene_analysis TO `<sp_client_id>`;
```
SP の `client_id` は `databricks apps get <app_name>` の `service_principal_client_id` から取得。

### 5.13 Gemini の nested JSON レスポンス

**症状**: Gemini が偶発的に `{"summary": "{\"summary\": \"…\", \"features\": […]}"}` のように summary の中に JSON 文字列を入れ子で返すことがある。結果として `scene_analysis.summary` カラムに生の JSON 文字列が混入。

**解決**: `gemini_client.py:_parse_json` で、`summary` が `{` で始まる文字列なら 1 段だけ `json.loads` してアンラップ。プロンプト側にも「入れ子禁止」を明記。

### 5.14 Databricks Apps のリソース権限

**症状**: deploy 時に SP が SQL warehouse / VS endpoint / serving endpoint へアクセスできずに 403。

**解決**: `app.yaml` の `resources` ブロックに `sql_warehouse` / `vector_search_endpoint` / `serving_endpoint` を全て列挙し、必要権限 (CAN_USE / CAN_MANAGE / CAN_QUERY) を付与。

---

## 6. ディレクトリ構成

```
video_search_with_gemini/
├── app/
│   ├── main.py                # FastAPI エントリ
│   ├── video_processing.py    # PySceneDetect + ffmpeg
│   ├── gemini_client.py       # FMAPI Gemini ラッパー
│   ├── db_client.py           # SQL warehouse + VS
│   ├── requirements.txt
│   ├── app.yaml               # Databricks Apps 設定
│   └── static/
│       ├── index.html
│       ├── style.css
│       └── app.js
├── notebooks/
│   ├── 00_setup_resources.py
│   └── 01_setup_vector_search.py
├── databricks.yml             # Asset Bundle
├── README.md
└── LICENSE
```

---

## 7. 環境変数

`app.yaml` で設定。ローカル開発時は環境変数で同等のものを設定。

| 変数 | デフォルト |
|------|-----------|
| `CATALOG` | `classic_stable_ytcy_catalog` |
| `SCHEMA` | `mulitmodal_video_search_with_gemini` |
| `VOLUME` | `media` |
| `DATABRICKS_WAREHOUSE_ID` | `e351c2d1b16eae95` |
| `VS_ENDPOINT_NAME` | `video-search-endpoint` |
| `VS_INDEX_NAME` | `…scene_analysis_index` |
| `GEMINI_ENDPOINT` | `databricks-gemini-2-5-flash` |
| `EMBEDDING_ENDPOINT` | `databricks-gte-large-en` |

---

## 8. ライセンス

Apache 2.0 (LICENSE 参照)
