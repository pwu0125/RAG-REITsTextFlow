# db_config.py


def get_db_announcement_config():
    """
    返回 MySQL 数据库announcement连接的配置信息。
    """
    db_announcement_config = {
        'host': '127.0.0.1',       # 数据库主机
        'port': 3306,               # 数据库端口
        'user': 'root',              # 数据库用户名
        'password': 'YOUR_MYSQL_PASSWORD',        # 数据库密码
        'database': 'announcement',         # 数据库名称
        'charset': 'utf8mb4',        # 字符集
        'init_command': "SET SESSION collation_connection = 'utf8mb4_unicode_ci'"  # 设置连接排序规则
    }
    return db_announcement_config

def get_vector_db_config():
    """
    [DEPRECATED] 返回向量数据库（Milvus）的连接配置信息。

    向量引擎已于 2026 年 7 月迁移至 FAISS（build_faiss_index.py 全量重建，
    索引文件 5_分析结果/faiss_data/reits_faiss.index）。
    本函数仅保留以兼容旧引用（如 create_vector_database.py / milvus 备份脚本），
    新代码请勿使用。
    """
    vector_db_config = {
        'host': 'localhost',  # 本地 Docker 部署的 Milvus
        'port': 19530,
        'user': 'root',
        'password': 'YOUR_MILVUS_PASSWORD'
    }
    return vector_db_config

def get_elasticsearch_config():
    """
    返回 Elasticsearch 数据库的连接配置信息。
    """
    elasticsearch_config = {
        'host': '127.0.0.1',          # Elasticsearch 服务主机
        'port': 9200,                 # Elasticsearch 服务端口
        'username': 'elastic',        # Elasticsearch 用户名
        'password': 'YOUR_ELASTICSEARCH_PASSWORD',    # Elasticsearch 密码
        'scheme': 'http'              # 明确指定连接协议为 http
    }
    return elasticsearch_config




