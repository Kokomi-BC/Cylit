"""services/：业务编排层（规划 §6.1 ★ 唯一允许访问 repositories / providers / 向量库的层）。

各服务模块之间通过**显式导入**协作，本包不在此处聚合导入，
避免循环依赖（如 ``paper_service`` ↔ ``reader_service`` ↔ ``translation_service``）。

常用入口::

    from backend.services.paper import paper_service, reader_service
    from backend.services.translation import translation_service
    from backend.services.task.task_service import task_service, task_runner
    from backend.services.model.model_manager import model_manager
    from backend.services.knowledge import knowledge_service
    from backend.services.annotation import annotation_service
"""
