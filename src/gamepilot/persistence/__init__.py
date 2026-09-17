"""持久化基础设施：SQLAlchemy Base、Engine/Session 工厂与 ORM 表映射。

本包目前只提供数据库骨架（配置、ORM、迁移），不含任何仓储实现；
PostgreSQL 仓储将在 TASK-002B 中加入，届时领域层与 API 层无需改动。
"""
