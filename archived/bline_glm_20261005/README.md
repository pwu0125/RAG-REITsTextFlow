# B线(GLM-5.3-Flash 表格描述)归档 — 2026-10-05 用户裁定

## 结论
- 质量可行: 8图A/B实测数字Jaccard平均0.965(5图完全一致), 表格格式符合
- 致命缺陷: 偶发缩水(同图5测2次只输出1/3, 收入表8数字全漏) + 特定跨页合并图(page_1003-1004, 宽高比1:2.6)连续6次空返回
  (注: 并非全部跨页图失败 — 180101 跨页图实测成功1637字, 失败是图片级非类别级; 跨页图占24%=1634张) + 偶发空返回
- 积分账: API直连不免费, 夜间五折约1.23万分/全量6821张(usage实测: 单图input 8442+output 2087)
- 速度: 8并发131s/8图无429限流
- 用户裁定: 维持A线(qwen-vl-ocr/qwen-vl-max), B线放弃归档

## 文件
- step4_glm_vision_utils.py — anthropic端点调GLM-5.3-Flash视觉, 与A线同prompt同接口
- ab_test_glm_vs_qwen.py + ab_test_results.json — A/B对比测试与结果
- b_stability_probe.py — 稳定性三连测(run1全/run2,3缩水)

## 复活条件
若智谱修复跨页长图支持且缩水可控, 可从本归档恢复; model_config.py 的 glm 段保留未删(无害)。
