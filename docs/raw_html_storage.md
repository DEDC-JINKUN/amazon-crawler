# 原始 HTML 存储边界

worker 通过 `RawHtmlStore.put(run_id, asin, body)` 保存源 HTML。当前 MVP 使用 `LocalRawHtmlStore`，目录结构和相对 key 保持不变，并采用临时文件替换保证原子写入。

本地实现先将 UTF-8 文本编码为字节再原子替换；evidence 使用同一文本字节计算 SHA-256，避免 Windows 文本换行或编码转换造成哈希漂移。

端到端回归已验证 worker 写入的 HTML 可被 `evidence_health.py` 读取并通过哈希检查；历史测试目录中的不一致文件仍只告警、不自动修正。

未来接入公司批准的 S3 兼容对象存储时，只需实现同一接口并返回对象 key；evidence 继续保存 key、哈希、采集时间和 parser 版本。当前不上传云端，也不把本地测试文件当作生产对象存储。
