-- 解释结果记下实际生成它的模型（版本元组的 model_id）。
-- NULL 表示不知道：本 migration 之前完成的请求，或执行器没有报告模型。
ALTER TABLE explanation_requests ADD COLUMN model_id TEXT;
