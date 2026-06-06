# Databricks notebook source
# MAGIC %md
# MAGIC # 01. Vector Search Index のセットアップ
# MAGIC
# MAGIC - Endpoint: `video-search-endpoint` (既存のものを再利用)
# MAGIC - Index: `scene_analysis_index`
# MAGIC - Embedding model: `databricks-gte-large-en` (1024 dim)
# MAGIC - Pipeline type: `TRIGGERED` (UI から手動 sync)

# COMMAND ----------

# MAGIC %pip install -q databricks-vectorsearch
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

from databricks.vector_search.client import VectorSearchClient

CATALOG = "classic_stable_ytcy_catalog"
SCHEMA = "mulitmodal_video_search_with_gemini"
SOURCE_TABLE = f"{CATALOG}.{SCHEMA}.scene_analysis"
INDEX_NAME = f"{CATALOG}.{SCHEMA}.scene_analysis_index"
ENDPOINT_NAME = "video-search-endpoint"
EMBEDDING_MODEL = "databricks-gte-large-en"

client = VectorSearchClient(disable_notice=True)

existing = [i["name"] for i in client.list_indexes(name=ENDPOINT_NAME).get("vector_indexes", [])]
print("Existing indexes:", existing)

# COMMAND ----------

if INDEX_NAME not in existing:
    index = client.create_delta_sync_index(
        endpoint_name=ENDPOINT_NAME,
        source_table_name=SOURCE_TABLE,
        index_name=INDEX_NAME,
        pipeline_type="TRIGGERED",
        primary_key="scene_id",
        embedding_source_column="embedding_text",
        embedding_model_endpoint_name=EMBEDDING_MODEL,
    )
    print("Created index:", INDEX_NAME)
else:
    print("Index already exists:", INDEX_NAME)

# COMMAND ----------

print(client.get_index(endpoint_name=ENDPOINT_NAME, index_name=INDEX_NAME).describe())
