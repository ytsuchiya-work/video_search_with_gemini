# Databricks notebook source
# MAGIC %md
# MAGIC # 00. UC リソースのセットアップ
# MAGIC
# MAGIC - Catalog: `classic_stable_ytcy_catalog`
# MAGIC - Schema: `mulitmodal_video_search_with_gemini`  (スキーマ名はユーザー指定どおり「mulit」綴り)
# MAGIC - Volume: `media` （uploads / scenes / audio の3サブディレクトリを使用）
# MAGIC - Tables: `videos`, `scenes`, `scene_analysis`

# COMMAND ----------

CATALOG = "classic_stable_ytcy_catalog"
SCHEMA = "mulitmodal_video_search_with_gemini"
VOLUME = "media"

spark.sql(f"CREATE SCHEMA IF NOT EXISTS {CATALOG}.{SCHEMA}")
spark.sql(f"CREATE VOLUME IF NOT EXISTS {CATALOG}.{SCHEMA}.{VOLUME}")

VOLUME_ROOT = f"/Volumes/{CATALOG}/{SCHEMA}/{VOLUME}"
import os
for sub in ("uploads", "scenes", "audio"):
    os.makedirs(f"{VOLUME_ROOT}/{sub}", exist_ok=True)

print("Volume root:", VOLUME_ROOT)

# COMMAND ----------

# MAGIC %md ## テーブル定義
# MAGIC
# MAGIC `scene_analysis` は Vector Search の同期元となる Delta テーブル。
# MAGIC change-data-feed を ON にして TRIGGERED モードでの同期に対応する。

# COMMAND ----------

spark.sql(f"""
CREATE TABLE IF NOT EXISTS {CATALOG}.{SCHEMA}.videos (
  video_id   STRING NOT NULL,
  filename   STRING,
  video_path STRING,
  duration   DOUBLE,
  num_scenes INT,
  status     STRING,
  uploaded_at TIMESTAMP,
  CONSTRAINT videos_pk PRIMARY KEY (video_id)
) USING DELTA
""")

spark.sql(f"""
CREATE TABLE IF NOT EXISTS {CATALOG}.{SCHEMA}.scenes (
  scene_id    STRING NOT NULL,
  video_id    STRING,
  scene_index INT,
  start_sec   DOUBLE,
  end_sec     DOUBLE,
  scene_path  STRING,
  audio_path  STRING,
  status      STRING,
  created_at  TIMESTAMP,
  CONSTRAINT scenes_pk PRIMARY KEY (scene_id)
) USING DELTA
""")

spark.sql(f"""
CREATE TABLE IF NOT EXISTS {CATALOG}.{SCHEMA}.scene_analysis (
  scene_id      STRING NOT NULL,
  video_id      STRING,
  scene_index   INT,
  start_sec     DOUBLE,
  end_sec       DOUBLE,
  scene_path    STRING,
  audio_path    STRING,
  transcript    STRING,
  summary       STRING,
  features      STRING,
  embedding_text STRING,
  created_at    TIMESTAMP,
  CONSTRAINT scene_analysis_pk PRIMARY KEY (scene_id)
) USING DELTA
TBLPROPERTIES (delta.enableChangeDataFeed = true)
""")

print("Tables created.")
display(spark.sql(f"SHOW TABLES IN {CATALOG}.{SCHEMA}"))
