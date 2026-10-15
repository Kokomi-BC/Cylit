
/*!40101 SET @OLD_CHARACTER_SET_CLIENT=@@CHARACTER_SET_CLIENT */;
/*!40101 SET @OLD_CHARACTER_SET_RESULTS=@@CHARACTER_SET_RESULTS */;
/*!40101 SET @OLD_COLLATION_CONNECTION=@@COLLATION_CONNECTION */;
/*!50503 SET NAMES utf8mb4 */;
/*!40103 SET @OLD_TIME_ZONE=@@TIME_ZONE */;
/*!40103 SET TIME_ZONE='+00:00' */;
/*!40014 SET @OLD_UNIQUE_CHECKS=@@UNIQUE_CHECKS, UNIQUE_CHECKS=0 */;
/*!40014 SET @OLD_FOREIGN_KEY_CHECKS=@@FOREIGN_KEY_CHECKS, FOREIGN_KEY_CHECKS=0 */;
/*!40101 SET @OLD_SQL_MODE=@@SQL_MODE, SQL_MODE='NO_AUTO_VALUE_ON_ZERO' */;
/*!40111 SET @OLD_SQL_NOTES=@@SQL_NOTES, SQL_NOTES=0 */;

/*!40000 ALTER TABLE `model_config` DISABLE KEYS */;
INSERT INTO `model_config` (`id`, `name`, `provider`, `model_type`, `model_name`, `base_url`, `device`, `parameters_json`, `secret_ref`, `enabled`, `is_default`, `status`, `status_detail`, `last_tested_at`, `created_at`, `updated_at`) VALUES ('embedding-7036089f','通义千问 · RAG 检索（qwen3-vl-embedding + qwen3-vl-rerank） · embedding','dashscope_multimodal','embedding','qwen3-vl-embedding',NULL,NULL,'{\"dim\": 2560, \"batch\": 10}','dashscope.key',1,1,'ok','','2026-09-24 14:17:39','2026-09-24 14:17:34','2026-09-24 14:17:53');
INSERT INTO `model_config` (`id`, `name`, `provider`, `model_type`, `model_name`, `base_url`, `device`, `parameters_json`, `secret_ref`, `enabled`, `is_default`, `status`, `status_detail`, `last_tested_at`, `created_at`, `updated_at`) VALUES ('generation-e6715c60','DeepSeek V4.1 Flash · 思考模式（默认） · generation','deepseek','generation','deepseek-flash','https://api.deepseek.com/v1',NULL,'{\"thinking\": \"enabled\", \"reasoning_effort\": \"high\", \"max_tokens\": 393216, \"top_p\": 0.95}','deepseek.key',1,1,'ok','ok','2026-09-24 14:18:06','2026-09-22 12:20:36','2026-09-27 06:19:59');
INSERT INTO `model_config` (`id`, `name`, `provider`, `model_type`, `model_name`, `base_url`, `device`, `parameters_json`, `secret_ref`, `enabled`, `is_default`, `status`, `status_detail`, `last_tested_at`, `created_at`, `updated_at`) VALUES ('heuristic-reranker','词法启发式重排（降级）','heuristic','reranker','heuristic-overlap',NULL,NULL,'{}',NULL,1,0,'unknown',NULL,NULL,'2026-09-24 14:38:04','2026-09-24 14:38:04');
INSERT INTO `model_config` (`id`, `name`, `provider`, `model_type`, `model_name`, `base_url`, `device`, `parameters_json`, `secret_ref`, `enabled`, `is_default`, `status`, `status_detail`, `last_tested_at`, `created_at`, `updated_at`) VALUES ('mock-embedding','离线哈希向量（Mock）','mock','embedding','mock-embedding',NULL,NULL,'{\"dim\": 512}',NULL,1,0,'unknown',NULL,NULL,'2026-09-24 14:38:04','2026-09-24 14:38:04');
INSERT INTO `model_config` (`id`, `name`, `provider`, `model_type`, `model_name`, `base_url`, `device`, `parameters_json`, `secret_ref`, `enabled`, `is_default`, `status`, `status_detail`, `last_tested_at`, `created_at`, `updated_at`) VALUES ('mock-generation','离线占位生成器（Mock）','mock','generation','mock-generation',NULL,NULL,'{\"delay_ms\": 0, \"max_tokens\": 393216}',NULL,1,0,'unknown',NULL,NULL,'2026-09-24 14:38:04','2026-09-27 06:19:59');
INSERT INTO `model_config` (`id`, `name`, `provider`, `model_type`, `model_name`, `base_url`, `device`, `parameters_json`, `secret_ref`, `enabled`, `is_default`, `status`, `status_detail`, `last_tested_at`, `created_at`, `updated_at`) VALUES ('mock-vision','离线占位视觉模型（Mock）','mock','vision','mock-vision',NULL,NULL,'{\"max_tokens\": 393216}',NULL,1,0,'unknown',NULL,NULL,'2026-09-24 14:38:04','2026-09-27 06:19:59');
INSERT INTO `model_config` (`id`, `name`, `provider`, `model_type`, `model_name`, `base_url`, `device`, `parameters_json`, `secret_ref`, `enabled`, `is_default`, `status`, `status_detail`, `last_tested_at`, `created_at`, `updated_at`) VALUES ('reranker-94cd6b92','通义千问 · RAG 检索（qwen3-vl-embedding + qwen3-vl-rerank） · reranker','dashscope','reranker','qwen3-vl-rerank',NULL,NULL,'{}','dashscope.key',1,1,'ok','','2026-09-24 14:17:45','2026-09-24 14:17:34','2026-09-24 14:18:04');
INSERT INTO `model_config` (`id`, `name`, `provider`, `model_type`, `model_name`, `base_url`, `device`, `parameters_json`, `secret_ref`, `enabled`, `is_default`, `status`, `status_detail`, `last_tested_at`, `created_at`, `updated_at`) VALUES ('vision-d978f800','DeepSeek V4.1 Flash · 思考模式（默认） · vision','deepseek','vision','deepseek-v4-flash-vision-exp','https://api.deepseek.com/v1',NULL,'{\"thinking\": \"enabled\", \"max_tokens\": 393216, \"top_p\": 0.95}','deepseek.key',1,1,'unknown',NULL,NULL,'2026-09-22 12:20:36','2026-09-27 06:19:59');
/*!40000 ALTER TABLE `model_config` ENABLE KEYS */;

/*!40000 ALTER TABLE `setting` DISABLE KEYS */;
INSERT INTO `setting` (`key`, `value`, `updated_at`) VALUES ('allow_general_knowledge','true','2026-09-22 11:19:23');
INSERT INTO `setting` (`key`, `value`, `updated_at`) VALUES ('kb.chunk_use_breaks','1','2026-09-25 17:08:59');
INSERT INTO `setting` (`key`, `value`, `updated_at`) VALUES ('kb.unit_index','1','2026-09-25 18:00:13');
INSERT INTO `setting` (`key`, `value`, `updated_at`) VALUES ('mineru_engine_default','\"local\"','2026-09-28 06:38:47');
INSERT INTO `setting` (`key`, `value`, `updated_at`) VALUES ('mineru_language','\"en\"','2026-09-28 06:46:15');
INSERT INTO `setting` (`key`, `value`, `updated_at`) VALUES ('mineru_last_verify','{\"ok\": true, \"state\": \"valid\", \"http_status\": 200, \"code\": -60012, \"server_msg\": \"task not found or expire\", \"token_masked\": \"sk-K…7eJg\", \"probe\": \"b0c7d995-5aa6-492a-aa3d-b5eefca228c4\", \"latency_ms\": 177.7, \"message\": \"Token 有效（178ms，任务不存在（探测用的随机 id，正常））\", \"at\": \"2026-09-28T11:52:33Z\"}','2026-09-28 11:52:33');
INSERT INTO `setting` (`key`, `value`, `updated_at`) VALUES ('mineru_model_version','\"pipeline\"','2026-09-28 11:52:33');
INSERT INTO `setting` (`key`, `value`, `updated_at`) VALUES ('translate_concurrency','6','2026-09-22 11:19:23');
/*!40000 ALTER TABLE `setting` ENABLE KEYS */;
/*!40103 SET TIME_ZONE=@OLD_TIME_ZONE */;

/*!40101 SET SQL_MODE=@OLD_SQL_MODE */;
/*!40014 SET FOREIGN_KEY_CHECKS=@OLD_FOREIGN_KEY_CHECKS */;
/*!40014 SET UNIQUE_CHECKS=@OLD_UNIQUE_CHECKS */;
/*!40101 SET CHARACTER_SET_CLIENT=@OLD_CHARACTER_SET_CLIENT */;
/*!40101 SET CHARACTER_SET_RESULTS=@OLD_CHARACTER_SET_RESULTS */;
/*!40101 SET COLLATION_CONNECTION=@OLD_COLLATION_CONNECTION */;
/*!40111 SET SQL_NOTES=@OLD_SQL_NOTES */;

